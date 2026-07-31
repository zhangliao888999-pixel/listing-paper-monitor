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
        res = _rpc_at(HELIUS, "getSignaturesForAddress", [addr, p]) if HELIUS else None
        if res is None:
            res = rpc("getSignaturesForAddress", [addr, p])
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
    return {"sig": sig_rec["sig"], "ts": sig_rec["ts"], "signer": signer,
            "fee": (meta.get("fee") or 0) / 1e9, "sol_bal": sol_bal,
            "sol_delta": sol_delta, "flow": dict(flow), "held": held}


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


def analyze(pool, txs, expected=None):
    """核心分析。txs 为按时间正序的 parse_tx 结果。返回 (metrics, wallet_rows)。

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

    # ---- 计价币: 看哪个已知计价币在签名钱包的流水里出现最多 ----
    hits = defaultdict(int)
    for t in txs:
        for (owner, mint) in t["flow"]:
            if owner in signers and mint in QUOTES:
                hits[mint] += 1
    quote = max(QUOTES, key=lambda m: hits.get(m, 0))
    qsym, qpx = QUOTES[quote]
    base = None
    bh = defaultdict(int)
    for t in txs:
        for (owner, mint) in t["flow"]:
            if mint not in QUOTES:
                bh[mint] += 1
    if bh:
        base = max(bh, key=lambda m: bh[m])

    # ---- 每个签名钱包的资金流 ----
    W = defaultdict(lambda: {"n_tx": 0, "n_buy": 0, "n_sell": 0, "in_usd": 0.0,
                             "out_usd": 0.0, "gas_usd": 0.0, "tok_held": 0.0,
                             "first_ts": None, "last_ts": None})
    sol_first, sol_last = {}, {}
    for t in txs:
        s = t["signer"]
        if not s:
            continue
        w = W[s]
        w["n_tx"] += 1
        w["gas_usd"] += t["fee"] * SOL_USD
        w["first_ts"] = w["first_ts"] or t["ts"]
        w["last_ts"] = t["ts"]
        if s in t["sol_bal"]:
            sol_first.setdefault(s, t["sol_bal"][s])
            sol_last[s] = t["sol_bal"][s]
        q = t["flow"].get((s, quote), 0.0)
        if quote == WSOL and abs(q) < 1e-9:
            # 原生SOL的swap会在同一笔里wrap再close,postTokenBalances的WSOL
            # 前后都是0,流水抓不到。必须回退到lamport增量。
            # 注意用**每笔的增量**累加,不能用余额首末差: 只出现在一笔交易里的
            # 钱包,我记录的首末都是同一个交易后余额,相减恒为0——SOL计价的池子
            # 因此全部算成"狗庄成本$0",这个bug让前8个快照全是废数据。
            q = t["sol_delta"].get(s, 0.0) + t["fee"]   # 加回手续费,只看交易本身
        if q < 0:
            w["in_usd"] += -q * qpx; w["n_buy"] += 1
        elif q > 0:
            w["out_usd"] += q * qpx; w["n_sell"] += 1
        for (owner, mint), v in t["held"].items():
            if mint == base and owner in W:
                W[owner]["tok_held"] = v
    # gas 和持币量在上面的主循环里已经能一次算完。原来这里是
    #   for s in W: sum(t["fee"] for t in txs if t["signer"]==s)
    # 即 O(钱包数 x 交易数),100个钱包配3000笔交易就是30万次,而 analyze()
    # 每3分钟对每个观察对象重算一遍 —— VPS的CPU被这个吃到81%。

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
    # 从簇内钱包收过币、自己却没花钱买过的,也算同伙(USOH那12个砸盘钱包)
    for s in W:
        if s in cluster:
            continue
        if W[s]["in_usd"] < 1.0 and W[s]["out_usd"] > 50.0:
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
        """这笔交易里,钱包s的计价币净变化(负=买入付钱, 正=卖出收钱)。

        SOL计价的池子必须回退到lamport增量,原因见上面的注释。三处地方
        (钱包账、储备曲线、寄生指标)口径必须完全一致,所以抽成一个函数。
        """
        q = t["flow"].get((s, quote), 0.0)
        if quote == WSOL and abs(q) < 1e-9:
            q = t["sol_delta"].get(s, 0.0) + t["fee"]
        return q

    # ---- 储备曲线: 资金守恒,钱包净失去的就是池子净得到的 ----
    cum, curve = 0.0, []
    for t in txs:
        s = t["signer"]
        if not s:
            continue
        cum += -qflow(t, s) * qpx
        curve.append((t["ts"], cum))
    peak_res = max((v for _, v in curve), default=0.0)
    end_res = curve[-1][1] if curve else 0.0

    # ---- 守恒自检 ----
    # 池子的净流入不可能为负: 谁也不能从池子里取出比投进去更多的钱。出现负值
    # 只有一个解释——我们没拿到完整历史(节点裁剪、或者池子由联合曲线迁移而来,
    # 起始流动性在本池签名之前)。这种样本的"狗庄成本"必然偏低,不能用。
    net_in = sum(w["in_usd"] for w in W.values()) - sum(w["out_usd"] for w in W.values())
    if net_in < -max(peak_res, 1.0) * 0.05:
        return None, []

    # ---- 砸盘检测: 60秒窗口内最大的计价币流出 ----
    sells = [(t["ts"], qflow(t, t["signer"]) * qpx)
             for t in txs if t["signer"] and qflow(t, t["signer"]) > 0]
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
    op_buys = [(t["ts"], -qflow(t, t["signer"]) * qpx)
               for t in txs if t["signer"] in cluster and qflow(t, t["signer"]) < 0]
    fish_buys = [(t["ts"], -qflow(t, t["signer"]) * qpx)
                 for t in txs if t["signer"] and t["signer"] not in cluster
                 and qflow(t, t["signer"]) < 0]
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
        updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return m, rows
