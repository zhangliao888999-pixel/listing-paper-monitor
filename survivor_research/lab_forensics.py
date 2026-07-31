# -*- coding: utf-8 -*-
"""狗庄研究实验室 - 链上取证引擎。2026-07-31建。

把一个池子的全部链上交易还原成"谁投了多少钱、谁拿走了多少钱、谁是一伙的"。

之前手工分析USOH和DISNEY踩过的坑,全部固化在这里:

  1. **计价币不一定是SOL**。DISNEY是USDC计价,只抓WSOL会算出"狗庄只花了$79"
     ——那其实全是gas。所以不预设,把所有mint的流水都收下来再判断。

  2. **只有签名钱包是真实交易者**。按代币持有人汇总会把池子PDA、路由账户、
     聚合器中转账户都算成"钱包",出现 -7,490 USDC 这种鬼条目。PDA不会签名。

  3. **gas要按原生SOL余额首末差算,不能用meta.fee**。Jito小费是普通转账不是
     手续费,DISNEY操盘方的meta.fee只有0.0024 SOL,实际烧了1.0552 SOL。

  4. **狗庄用多钱包倒手**。USOH的创建者买了600 SOL从不卖,把币转给12个钱包
     去砸盘,单看钱包会得出"内部人亏了$46K"的荒谬结论。所以要用代币转账把
     同伙并成一个簇(并查集),整簇算总账。

  5. **储备曲线不用找金库账户**。资金守恒: 钱包净失去的就是池子净得到的,
     直接对所有钱包的计价币流水取累计负值即可,比认PDA可靠得多。
"""
import threading
import time
from collections import defaultdict

import requests

# 公共节点各自限流都很狠,靠轮换分散压力。实测: 单节点在并发下
# getTransaction 直接返回空,而且**不报错**,交易就被静默丢掉了
# (曾出现310笔只解析成功88笔却照样输出资金账)。
#
# 有 Helius key 时优先走它(免费档100万次/月、100请求/秒),公共节点只做兜底。
# key 放在 .helius_key,已加入 .gitignore,不进版本库。
_PUBLIC = ["https://api.mainnet-beta.solana.com",
           "https://solana-rpc.publicnode.com",
           "https://rpc.ankr.com/solana",
           "https://solana.drpc.org"]


def _load_helius():
    from pathlib import Path
    kf = Path(__file__).parent / ".helius_key"
    if kf.exists():
        k = kf.read_text(encoding="utf-8").strip()
        if k:
            return f"https://mainnet.helius-rpc.com/?api-key={k}"
    return None


HELIUS = _load_helius()
HAS_HELIUS = HELIUS is not None
RPCS = ([HELIUS] if HELIUS else []) + _PUBLIC
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTES = {WSOL: ("SOL", 75.0), USDC: ("USDC", 1.0), USDT: ("USDT", 1.0)}
SOL_USD = 75.0
MAX_FETCH = 4000          # 超过这个交易量的池子单独跑,不拖慢批量流水线
DUMP_WINDOW = 60          # 砸盘检测窗口(秒)

_lock = threading.Lock()
_rr = [0]

# 每个线程一个带连接池的 Session。
# 这是VPS上CPU 100%的真凶: 原来每次RPC都是裸 requests.post(),不复用连接,
# 每个请求都要完整做一次TLS握手。握手在C层执行且释放GIL,所以10个线程能把
# 4.5个核吃满 —— 纯Python代码受GIL限制根本做不到这一点,多核满载本身就是
# "开销在C层"的证据。加上keep-alive后握手只发生一次。
_tls = threading.local()


def _session():
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        ad = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8,
                                           max_retries=0)
        s.mount("https://", ad)
        s.headers.update({"Content-Type": "application/json",
                          "Connection": "keep-alive"})
        _tls.s = s
    return s

# Helius免费档: 100万credit/月、约10请求/秒。实测加线程到32吞吐也停在7笔/秒,
# 说明限速在服务端。这里记账,免得像上次CoinGecko那样撞上额度才发现。
_USAGE_F = __import__("pathlib").Path(__file__).parent / ".helius_usage.json"
_usage = [0, 0.0]        # [本进程调用数, 上次落盘时间]


def _tick_usage():
    import json
    from datetime import datetime
    _usage[0] += 1
    if time.time() - _usage[1] < 60:
        return
    _usage[1] = time.time()
    try:
        d = json.loads(_USAGE_F.read_text()) if _USAGE_F.exists() else {}
    except (ValueError, OSError):
        d = {}
    mon = datetime.now().strftime("%Y-%m")
    d[mon] = d.get(mon, 0) + _usage[0]
    _usage[0] = 0
    try:
        _USAGE_F.write_text(json.dumps(d))
    except OSError:
        pass


def usage_report():
    import json
    from datetime import datetime
    try:
        d = json.loads(_USAGE_F.read_text())
    except (ValueError, OSError):
        return "无记录"
    mon = datetime.now().strftime("%Y-%m")
    n = d.get(mon, 0) + _usage[0]
    return f"{n:,}/1,000,000 ({n/10000:.1f}%)"


def _rpc_at(url, method, params, tries=4):
    """打指定节点,不轮换。给必须保证历史完整性的调用用。"""
    for k in range(tries):
        if url == HELIUS:
            _tick_usage()
        try:
            r = _session().post(url, json={"jsonrpc": "2.0", "id": 1,
                                           "method": method, "params": params}, timeout=30)
            if r.status_code == 200:
                # 只解析一次。原来 `"result" in r.json()` 和 `r.json()["result"]`
                # 各解一次,大响应(getTransaction 的JSON常有几十KB)白白翻倍。
                j = r.json()
                if "result" in j:
                    return j["result"]
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.5 * (k + 1))
    return None


def rpc(method, params, tries=5):
    """有Helius时前两次都打Helius,失败才降级到公共节点。

    之前是所有端点等权轮换,结果40%的请求落到慢的公共节点上,把Helius的
    速度优势整个拉平了(实测只有7.6笔/秒,而Helius单独能跑到几十)。
    """
    for k in range(tries):
        if HELIUS and k < 2:
            url = HELIUS
            _tick_usage()
        else:
            with _lock:
                url = RPCS[_rr[0] % len(RPCS)]; _rr[0] += 1
        try:
            r = _session().post(url, json={"jsonrpc": "2.0", "id": 1,
                                           "method": method, "params": params}, timeout=30)
            if r.status_code == 200:
                j = r.json()
                if "result" in j:
                    return j["result"]
            elif r.status_code in (429, 503):
                time.sleep(0.8 * (k + 1)); continue
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.4 * (k + 1))
    return None


def get_signatures(addr, cap=None):
    """拉全部签名。**必须钉死在Helius上,不能轮换到公共节点。**

    实测同一个池子在不同节点上拿到的签名数不同(67 vs 173): 公共节点会裁剪
    历史,只保留最近一段。轮换的话每次拉到的历史深度都不一样,算出来的
    "狗庄成本"就会随机偏低,甚至出现"净流入为负"这种物理上不可能的结果。
    """
    sigs, before = [], None
    while True:
        p = {"limit": 1000}
        if before:
            p["before"] = before
        # 分页失败必须重试到底。原来一失败就 break,**静默截断**且不报错 ——
        # 同一个池子三次拉到 36,014 / 20,913 / 1,992 笔,历史深度每次都不同,
        # 算出来的狗庄成本自然也每次不同。这种错比拿不到数据更危险。
        res = None
        for attempt in range(6):
            res = _rpc_at(HELIUS, "getSignaturesForAddress", [addr, p]) if HELIUS else None
            if res is None:
                res = rpc("getSignaturesForAddress", [addr, p])
            if res is not None:
                break
            time.sleep(2.0 * (attempt + 1))
        if res is None:
            raise RuntimeError(
                "签名分页失败: %s 已拿到%d笔就断了,历史不完整" % (addr[:12], len(sigs)))
        if not res:
            break
        sigs += [{"sig": s["signature"], "ts": s.get("blockTime"),
                  "err": s.get("err") is not None} for s in res]
        if len(res) < 1000 or (cap and len(sigs) > cap):
            break
        before = res[-1]["signature"]
        time.sleep(0.1)
    sigs.reverse()
    return sigs


def account_keys(res):
    """还原完整账户列表。

    这是个必须踩过一次才知道的坑: 版本化交易(v0)用地址查找表,
    message.accountKeys 只有静态地址,而 pre/postTokenBalances 里的
    accountIndex 是按 **静态 + 查找表writable + 查找表readonly** 的完整
    顺序编号的。只用accountKeys会整体错位,金库认不出来、余额算到别人头上。
    """
    msg = (res.get("transaction") or {}).get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in (msg.get("accountKeys") or [])]
    loaded = (res.get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])


def parse_tx(sig_rec):
    """一笔交易 -> 签名人 / 原生SOL余额 / 各持有人各代币的净变化。"""
    res = rpc("getTransaction", [sig_rec["sig"],
                                 {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}])
    if not res:
        return None
    meta = res.get("meta") or {}
    if meta.get("err"):
        return None
    keys = account_keys(res)
    signer = keys[0] if keys else None
    pre_l, post_l = meta.get("preBalances") or [], meta.get("postBalances") or []
    sol_bal, sol_delta = {}, {}
    for i, k in enumerate(keys):
        if i < len(pre_l) and i < len(post_l):
            sol_bal[k] = post_l[i] / 1e9
            d = (post_l[i] - pre_l[i]) / 1e9
            if abs(d) > 1e-9:
                sol_delta[k] = d
    prev = {b.get("accountIndex"): float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            for b in (meta.get("preTokenBalances") or [])}
    flow, held = defaultdict(float), {}
    for b in (meta.get("postTokenBalances") or []):
        owner, mint, idx = b.get("owner"), b.get("mint"), b.get("accountIndex")
        v = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        if owner:
            held[(owner, mint)] = v
            d = v - prev.get(idx, 0.0)
            if abs(d) > 1e-9:
                flow[(owner, mint)] += d
    # 这笔是不是"池子诞生"事件。用来判历史是否完整,以及把建池注资从买卖里
    # 剔出去。只存一个布尔值,不存指令名,省内存。
    #
    # 关键词必须精确。第一版写了 "Instruction: Initialize",它会匹配到
    # **InitializeAccount3** —— 每个第一次买这个币的人都要创建代币账户,
    # 于是所有首次买入全被当成"建池注资"跳过,USOH 的狗庄成本从 $45,364
    # 变成 $0,钱包数从128掉到42。同理不能用宽泛的 "Instruction: Create"
    # (会匹配 CreateIdempotent 这类ATA指令)。
    logs = meta.get("logMessages") or []
    init = any(any(k in l for k in ("MigrateV2", "InitializeVirtualPool",
                                    "Instruction: CreatePool", "Instruction: Migrate",
                                    "Instruction: Initialize2", "Instruction: InitializePool"))
               for l in logs)
    return {"sig": sig_rec["sig"], "ts": sig_rec["ts"], "signer": signer,
            "fee": (meta.get("fee") or 0) / 1e9, "sol_bal": sol_bal,
            "sol_delta": sol_delta, "flow": dict(flow), "held": held,
            "init": init}


class Union:
    """并查集: 把靠代币转账连在一起的钱包并成一伙。"""

    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


MIN_COVERAGE = 0.90       # 交易覆盖率低于这个,数据不可信
MAX_CUSTODIAL_SHARE = 0.35   # 托管平台占资金流量超过这个,归属不可信


def analyze(pool, txs, expected=None, role_lookup=None, use_mover=True):
    """核心分析。txs 为按时间正序的 parse_tx 结果。返回 (metrics, wallet_rows)。

    role_lookup: 可选的角色判定函数(见 lab_registry.role_of)。传了就会把
    协议费/创建者费/托管平台账户从"狗庄 vs 鱼"的统计里剔除,并给出归属
    可信度。不传则退化成老行为,方便回归对比。

    expected: 本应拉到多少笔。公共RPC被打满时 getTransaction 会静默返回空,
    丢掉的交易不会报错,资金账就会少算——曾经出现过310笔只解析成功88笔,
    却照样输出"狗庄成本$34"这种看着很确定的错数字。所以覆盖率不够就拒绝
    出结果,宁可没有数据也不要错数据。
    """
    txs = [t for t in txs if t and t.get("ts")]
    if len(txs) < 5:
        return None, []
    if expected and len(txs) < expected * MIN_COVERAGE:
        return None, []
    txs.sort(key=lambda x: x["ts"])
    signers = {t["signer"] for t in txs if t["signer"]}

    # ---- 计价币判定 ----
    # 原来只数"签名钱包"的计价币流水,踩了个大坑: SOL的swap在同一笔内wrap再
    # close,签名人的WSOL流水前后都是0,WSOL永远是0票;而只要有**一笔**订单
    # 路由经过USDC,USDC就以1票胜出 —— 于是整个SOL池子的交易全部隐形,算出
    # "狗庄成本$0.00"($GATE那个池子就是这么错的)。
    #
    # 改成数所有持有人(池子金库也算): SOL池子的金库持有WSOL,每笔swap都会
    # 动,票数自然高。再加一道门槛: 胜出者必须出现在足够多的交易里,否则
    # 认定为原生SOL计价——这是Solana上绝大多数池子的情况。
    hits = defaultdict(int)
    for t in txs:
        seen_mints = {mint for (_, mint) in t["flow"] if mint in QUOTES}
        for mint in seen_mints:
            hits[mint] += 1
    quote = max(QUOTES, key=lambda m: hits.get(m, 0))
    if hits.get(quote, 0) < len(txs) * 0.25:
        quote = WSOL
    qsym, qpx = QUOTES[quote]
    base = None
    bh = defaultdict(int)
    for t in txs:
        for (owner, mint) in t["flow"]:
            if mint not in QUOTES:
                bh[mint] += 1
    if bh:
        base = max(bh, key=lambda m: bh[m])

    # 池子金库的识别: 它是每一笔交易的对手方,所以出现频率接近100%;真实
    # 交易者只占很小比例。不能只排除"池子地址本身"——金库的持有人往往是
    # 另一个PDA(DISNEY的金库持有人是 FhVo3mqL8PW5,出现在265/662笔里,
    # 第一版没排除它,结果它被当成"鱼",凭空多出$7,797)。
    # 金库要在**两个命名空间**里各排一遍,这是踩过的坑:
    # flow 的 key 是"持有人"地址,sol_delta 的 key 是"账户"地址,两者不是
    # 一回事。第一版只用 flow 建集合,lamport 那条路径里金库的代币账户从来
    # 没被排除,于是被当成 money_mover —— USOH 的钱包数从128掉到57、
    # 狗庄成本从 $45,364 变成 $0。
    _q_hits, _sol_hits = defaultdict(int), defaultdict(int)
    for t in txs:
        for (owner, mint) in t["flow"]:
            if mint == quote:
                _q_hits[owner] += 1
        gas0 = max(t["fee"] * 2, 3e-5)
        for acc, d in t["sol_delta"].items():
            if abs(d) > gas0:
                _sol_hits[acc] += 1
    _n = max(len(txs), 1)
    _vaults = ({pool}
               | {a for a, n in _q_hits.items() if n > _n * 0.6}
               | {a for a, n in _sol_hits.items() if n > _n * 0.6})

    def money_mover(t):
        """这笔交易里真正出钱/收钱的账户。

        设计原则: **默认相信签名人,只在它明显没出钱时才另找。**

        为什么不能一律"取资金变动最大的账户": 试过,连错四次。那样会在正常
        池子里选中金库或中转账户 —— USOH 的钱包数从128掉到57、狗庄成本从
        $45,364 变成 $0。金库在 flow(持有人地址)和 sol_delta(账户地址)
        两个命名空间里都要排,即便排干净了,这个判据本身还是太激进。

        但签名人口径也确实会被绕过: $GATE 的操盘方用"热钱包签名 + 另一个
        账户出资",签名钱包每笔只掉 0.000021 SOL 的gas,真正的 0.0145 SOL
        走 BwWK17cbHxwW,算出来的狗庄成本是 $0。

        所以只处理这一种情况: 签名人的资金变动只有gas量级 -> 说明它是代付
        gas的角色 -> 在非金库账户里找变动最大的那个。其余一概按签名人算。
        """
        s0 = t["signer"]
        if not use_mover or not s0:
            return s0
        own = t["flow"].get((s0, quote), 0.0)
        if quote == WSOL and abs(own) < 1e-9:
            own = t["sol_delta"].get(s0, 0.0) + t["fee"]
        gas = max(t["fee"] * 3, 1e-4)
        if abs(own) > gas:
            return s0                      # 签名人确实出了钱
        best, bv = None, 0.0
        for (owner, mint), v in t["flow"].items():
            if mint == quote and owner not in _vaults and abs(v) > abs(bv):
                best, bv = owner, v
        if best:
            return best
        for acc, d in t["sol_delta"].items():
            if acc in _vaults or acc == s0 or abs(d) <= gas:
                continue
            if abs(d) > abs(bv):
                best, bv = acc, d
        return best or s0

    # ---- 每个"真正动钱的账户"的资金流 ----
    W = defaultdict(lambda: {"n_tx": 0, "n_buy": 0, "n_sell": 0, "in_usd": 0.0,
                             "out_usd": 0.0, "gas_usd": 0.0, "tok_held": 0.0,
                             "first_ts": None, "last_ts": None,
                             "tok_in": 0.0, "tok_out": 0.0})
    sol_first, sol_last = {}, {}
    seed_usd = 0.0
    for t in txs:
        s = money_mover(t) if use_mover else t["signer"]
        if not s:
            continue
        if t.get("init"):
            # 建池/迁移那一笔里的资金是**注入流动性**,不是买卖。DISNEY 的
            # CFXpPrPLhN8J 在建池那笔里出了 $7,510,按交易记账会变成一条
            # 凭空多出来的"鱼",而这个币真实的外部买入是 $0。
            q0 = t["flow"].get((s, quote), 0.0)
            seed_usd += abs(q0) * qpx
            continue
        w = W[s]
        w["n_tx"] += 1
        # gas 记在签名人头上(它才是付gas的),但资金记在出钱账户头上
        if t["signer"] == s:
            w["gas_usd"] += t["fee"] * SOL_USD
        w["first_ts"] = w["first_ts"] or t["ts"]
        w["last_ts"] = t["ts"]
        if s in t["sol_bal"]:
            sol_first.setdefault(s, t["sol_bal"][s])
            sol_last[s] = t["sol_bal"][s]
        q = t["flow"].get((s, quote), 0.0)
        if quote == WSOL and abs(q) < 1e-9:
            # 出钱账户不一定付gas,所以这里不能无条件加回手续费
            # 原生SOL的swap会在同一笔里wrap再close,postTokenBalances的WSOL
            # 前后都是0,流水抓不到。必须回退到lamport增量。
            # 注意用**每笔的增量**累加,不能用余额首末差: 只出现在一笔交易里的
            # 钱包,我记录的首末都是同一个交易后余额,相减恒为0——SOL计价的池子
            # 因此全部算成"狗庄成本$0",这个bug让前8个快照全是废数据。
            q = t["sol_delta"].get(s, 0.0)
            if t["signer"] == s:
                q += t["fee"]
        if q < 0:
            w["in_usd"] += -q * qpx; w["n_buy"] += 1
        elif q > 0:
            w["out_usd"] += q * qpx; w["n_sell"] += 1
        # 记录标的币的收发数量。卖出的币远多于买进的,差额只能来自钱包间转账
        # —— 那是识别狗庄同伙的关键线索(见下面的簇识别)。
        if base:
            tq = t["flow"].get((s, base), 0.0)
            if tq > 0:
                w["tok_in"] += tq
            elif tq < 0:
                w["tok_out"] += -tq
        for (owner, mint), v in t["held"].items():
            if mint == base and owner in W:
                W[owner]["tok_held"] = v
    # gas 和持币量在上面的主循环里已经能一次算完。原来这里是
    #   for s in W: sum(t["fee"] for t in txs if t["signer"]==s)
    # 即 O(钱包数 x 交易数),100个钱包配3000笔交易就是30万次,而 analyze()
    # 每3分钟对每个观察对象重算一遍 —— VPS的CPU被这个吃到81%。

    # ---- 角色过滤 ----
    # 池子里出现的账户不全是交易者。pump.fun的协议费账户(每笔抽0.95%)、
    # 每个币自己的创建者费账户(0.30%)、以及托管平台的资金池,都会有大额
    # 资金进出但跟"狗庄 vs 鱼"的博弈无关。
    # $GATE 那个池子里资金流量最大的两个账户都是托管平台(余额46,751 SOL、
    # 11个签名人共用、同时操作16个币),把它们算成交易者,算出来的是平台
    # 代表一堆用户的进出总和,跟任何一个操盘方都没关系。
    roles, cust_vol, tot_vol = {}, 0.0, 0.0
    if role_lookup:
        n_tx_all = len(txs)
        for a, w in list(W.items()):
            vol = w["in_usd"] + w["out_usd"]
            tot_vol += vol
            try:
                role, _ = role_lookup(a, pool, w["in_usd"], w["out_usd"],
                                      w["n_tx"], n_tx_all)
            except Exception:
                role = "trader"
            roles[a] = role
            if role == "custodial":
                cust_vol += vol
            if role != "trader":
                W.pop(a, None)
        if not W:
            return None, []

    # ---- 同伙识别: 代币转账(标的币动了但计价币没动)把钱包连起来 ----
    # 这里踩过一个会让整个模型失真的坑: 判断"计价币有没有动"原本只看
    # flow 里有没有 quote 的条目。但SOL计价的池子里,原生SOL的swap会在同一笔
    # 内 wrap 再 close,flow 里根本没有WSOL记录 —— 于是**每一笔买卖都被当成
    # 了转账**,把交易双方并进同一个簇,最后全池子的人都成了"狗庄同伙",
    # 鱼永远是$0(146笔59个钱包的池子算出鱼$0就是这么来的)。
    # 所以SOL计价时必须回退到看 signer 的 lamport 变化: 真转账只掉gas。
    uf = Union()
    for t in txs:
        if not base:
            break
        movers = [(o, v) for (o, m), v in t["flow"].items() if m == base]
        if len(movers) < 2:
            continue
        qmoved = any(m == quote for (_, m) in t["flow"])
        if not qmoved and quote == WSOL and t["signer"]:
            moved_sol = abs(t["sol_delta"].get(t["signer"], 0.0) + t["fee"])
            qmoved = moved_sol > 0.0005      # 超过gas量级就是买卖不是转账
        if qmoved:
            continue                     # 有计价币变动的是买卖,不是转账
        senders = [o for o, v in movers if v < 0]
        receivers = [o for o, v in movers if v > 0]
        for a in senders:
            for b in receivers:
                uf.union(a, b)

    creator = txs[0]["signer"]
    # 操盘方簇 = 建池者所在的簇 + 交易笔数最多的钱包所在的簇
    busiest = max(W, key=lambda k: W[k]["n_tx"]) if W else creator
    op_roots = {uf.find(creator), uf.find(busiest)}
    cluster = {s for s in W if uf.find(s) in op_roots}
    # USOH 的12个砸盘钱包是靠**钱包间转账**拿到币的,而转账不经过池子、
    # 不在池子的签名历史里,并查集看不到这个连接 —— 结果它们被当成"鱼",
    # 狗庄取出算成 $87 而实际是 $66,474。
    # 但可以从代币数量反推: 在本池只买进少量币却卖出大量币,差额必然来自
    # 外部转入。这12个钱包每个买入约$200、卖出约$3,600,比例18倍。
    for s in W:
        if s in cluster:
            continue
        w = W[s]
        if w["in_usd"] < 1.0 and w["out_usd"] > 50.0:
            cluster.add(s); continue
        if (w["tok_out"] > w["tok_in"] * 2.0 and w["tok_out"] > 0
                and w["out_usd"] > max(w["in_usd"] * 3.0, 20.0)):
            cluster.add(s)

    rows = []
    for s, w in W.items():
        role = "creator" if s == creator else ("operator" if s == busiest else
                                               ("cluster" if s in cluster else "fish"))
        rows.append(dict(addr=s, role=role, **w))

    op_cost = sum(w["in_usd"] for s, w in W.items() if s in cluster)
    op_out = sum(w["out_usd"] for s, w in W.items() if s in cluster)
    op_gas = sum(w["gas_usd"] for s, w in W.items() if s in cluster)
    op_tok = sum(w["tok_held"] for s, w in W.items() if s in cluster)
    fish = {s: w for s, w in W.items() if s not in cluster}
    fish_in = sum(w["in_usd"] for w in fish.values())
    fish_out = sum(w["out_usd"] for w in fish.values())

    def qflow(t, s):
        """注意: s 必须是 money_mover 认定的账户,口径要和上面的主循环一致。"""
        """这笔交易里,钱包s的计价币净变化(负=买入付钱, 正=卖出收钱)。

        SOL计价的池子必须回退到lamport增量,原因见上面的注释。三处地方
        (钱包账、储备曲线、寄生指标)口径必须完全一致,所以抽成一个函数。
        """
        q = t["flow"].get((s, quote), 0.0)
        if quote == WSOL and abs(q) < 1e-9:
            # 出钱账户不一定付gas,所以这里不能无条件加回手续费
            q = t["sol_delta"].get(s, 0.0) + t["fee"]
        return q

    # ---- 储备曲线: 资金守恒,钱包净失去的就是池子净得到的 ----
    cum, curve = 0.0, []
    for t in txs:
        s = money_mover(t) if use_mover else t["signer"]
        if not s:
            continue
        cum += -qflow(t, s) * qpx
        curve.append((t["ts"], cum))
    peak_res = max((v for _, v in curve), default=0.0)
    end_res = curve[-1][1] if curve else 0.0

    # ---- 历史完整性检查 ----
    # 这里原来放的是"净流入不能为负"的守恒闸门,**那个设计是错的**,而且错得
    # 很隐蔽: 它把毕业迁移来的池子全部误杀了,而那恰恰是最有价值的样本
    # (USOH开盘就有672 SOL,其中594是狗庄在联合曲线阶段打进去的)。
    #
    # 根本问题: 迁移池子天生带着一笔**不在本池签名历史里**的初始储备,所以
    # "取出 > 投入"对它来说是正常的。而真正的历史缺失表现完全一样。这个判据
    # 从原理上就区分不了两者,不是调阈值能救的。
    #
    # 改成直接检测: 我们有没有拿到"池子诞生"那一笔。拿到了,历史就是完整的,
    # 至于初始储备多少,单独记下来当指标用(它本身就是狗庄的铺底金额)。
    gross_in = sum(w["in_usd"] for w in W.values())
    gross_out = sum(w["out_usd"] for w in W.values())
    has_birth = any(t.get("init") for t in txs[:5])
    # 初始储备 = 开盘就存在、却没人存进来的那部分 = 取出超过投入的部分
    # 初始储备优先用建池那笔实际注入的金额;没抓到就退回"取出超过投入"的估计
    init_reserve = seed_usd if seed_usd > 0 else max(gross_out - gross_in, 0.0)
    # 不因为历史不完整就丢弃数据。USOH 有9,623笔,抓取上限2600时诞生事件必然
    # 不在窗口里 —— 直接拒绝等于把所有大池子排除掉,那才是更大的偏差。
    # 改成打质量标签,让下游按需过滤: 要精确算狗庄成本时只用 full 的样本,
    # 看形态和结局时 truncated 的一样能用。
    if has_birth:
        quality = "full"
    elif init_reserve > max(gross_in, 1.0) * 0.15:
        quality = "truncated"      # 缺开头,狗庄成本会偏低
    else:
        quality = "partial"        # 没见到诞生事件但资金基本配平

    # ---- 砸盘检测: 60秒窗口内最大的计价币流出 ----
    _mv = {id(t): (money_mover(t) if use_mover else t["signer"]) for t in txs}
    sells = [(t["ts"], qflow(t, _mv[id(t)]) * qpx)
             for t in txs if _mv[id(t)] and qflow(t, _mv[id(t)]) > 0]
    # 找60秒窗口内流出最大的那一段。原来是对每笔卖出再扫一遍后面所有卖出,
    # O(卖出笔数^2); 卖出上千笔的池子光这一步就要上百万次运算。改成双指针
    # 滑动窗口,一趟扫完。
    dump_usd, dump_t0, dump_t1 = 0.0, None, None
    lo = 0
    run = 0.0
    for hi in range(len(sells)):
        run += sells[hi][1]
        while sells[hi][0] - sells[lo][0] > DUMP_WINDOW:
            run -= sells[lo][1]
            lo += 1
        if run > dump_usd:
            dump_usd = run
            dump_t0 = sells[lo][0]
            dump_t1 = sells[hi][0]

    # ---- 第一条鱼 / 鱼到砸盘的间隔 ----
    fish_ts = [w["first_ts"] for s, w in fish.items() if w["in_usd"] > 1.0]
    t0 = txs[0]["ts"]
    t_first_fish = (min(fish_ts) - t0) / 60 if fish_ts else None
    t_fish_to_dump = ((dump_t0 - min(fish_ts)) / 60
                      if (fish_ts and dump_t0 and dump_t0 >= min(fish_ts)) else None)

    life = (txs[-1]["ts"] - t0) / 60
    idle = (time.time() - txs[-1]["ts"]) / 60
    tx_by_w = sorted((w["n_tx"] for w in W.values()), reverse=True)
    tot_tx = sum(tx_by_w) or 1
    top_share = tx_by_w[0] / tot_tx if tx_by_w else 0
    hhi = sum((n / tot_tx) ** 2 for n in tx_by_w)

    if len(fish) == 0 or fish_in < 10:
        outcome = "no_fish"
    elif dump_usd > max(peak_res, 1) * 0.3:
        outcome = "caught"
    elif idle > 60:
        outcome = "abandoned"
    else:
        outcome = "running"

    # ---- 寄生策略要的指标 ----
    # 核心问题: 多大的外部买单会触发他收网? 用户实盘$5就被砸,但那是刚出生的
    # 币; DISNEY这种已经沉没$2,244的盘,砸一个$5的买家等于自己认赔离场,不合算。
    # 所以猜测阈值跟沉没成本挂钩,这里把两边都量出来让数据说话。
    op_buys = [(t["ts"], -qflow(t, _mv[id(t)]) * qpx)
               for t in txs if _mv[id(t)] in cluster and qflow(t, _mv[id(t)]) < 0]
    fish_buys = [(t["ts"], -qflow(t, _mv[id(t)]) * qpx)
                 for t in txs if _mv[id(t)] and _mv[id(t)] not in cluster
                 and qflow(t, _mv[id(t)]) < 0]
    trigger_buy = op_cost_at_dump = fish_in_at_dump = trig_ratio = None
    if dump_t0:
        pre = [v for ts, v in fish_buys if 0 <= dump_t0 - ts <= 300]
        trigger_buy = round(max(pre), 2) if pre else 0.0
        op_cost_at_dump = round(sum(v for ts, v in op_buys if ts <= dump_t0), 2)
        fish_in_at_dump = round(sum(v for ts, v in fish_buys if ts <= dump_t0), 2)
        if op_cost_at_dump > 0:
            trig_ratio = round(trigger_buy / op_cost_at_dump, 4)
    # 被忽略的最大外部买单 = 安全仓位的经验上限
    ignored = [v for ts, v in fish_buys if not dump_t0 or ts < dump_t0 - 300]
    ratchet = round(sum(v for _, v in op_buys) / max(life, 1), 3)

    m = dict(
        trigger_buy_usd=trigger_buy, op_cost_at_dump=op_cost_at_dump,
        trigger_ratio=trig_ratio, fish_in_at_dump=fish_in_at_dump,
        ratchet_usd_min=ratchet,
        max_fish_ignored=round(max(ignored), 2) if ignored else 0.0,
        quote_sym=qsym, n_tx=len(txs), n_wallet=len(W), life_min=round(life, 1),
        idle_min=round(idle, 1), top_share=round(top_share, 4), hhi=round(hhi, 4),
        op_addr=busiest, op_cost_usd=round(op_cost, 2), op_out_usd=round(op_out, 2),
        op_gas_usd=round(op_gas, 2), op_pnl_usd=round(op_out - op_cost - op_gas, 2),
        op_tok_held=round(op_tok, 2), fish_n=len(fish), fish_in_usd=round(fish_in, 2),
        fish_out_usd=round(fish_out, 2), peak_res_usd=round(peak_res, 2),
        end_res_usd=round(end_res, 2), drained_usd=round(peak_res - end_res, 2),
        t_first_fish=round(t_first_fish, 2) if t_first_fish is not None else None,
        t_fish_to_dump=round(t_fish_to_dump, 2) if t_fish_to_dump is not None else None,
        dump_sec=round(dump_t1 - dump_t0, 1) if dump_t0 else None,
        max_drawdown=None, outcome=outcome,
        has_birth=int(has_birth),
        data_quality=quality,
        init_reserve_usd=round(init_reserve, 2),
        custodial_share=round(cust_vol / tot_vol, 4) if tot_vol else 0.0,
        attribution_ok=int(not tot_vol or cust_vol / tot_vol <= MAX_CUSTODIAL_SHARE),
        updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return m, rows
