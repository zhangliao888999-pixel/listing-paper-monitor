# -*- coding: utf-8 -*-
""""蹲机器人对倒币,真买家一到就跑"执行器 —— 跟live_botscalp/live_runner.py同一套
安全边界,照抄过来,不是新发明:

  - 私钥只从环境变量 WALLET_PRIVATE_KEY 读进内存,从不写日志/落盘/发网络请求携带。
    必须由用户自己在自己的机器上设置并启动 —— 我(Claude)不会持有你的私钥,不会
    在我这边替你真实下单。你部署、你启动、你负责。
  - 默认 DRY_RUN(只打印"本来会做什么",不广播真实交易)。要真正下单,必须同时
    设置 LIVE_TRADING=1 和 CONFIRM_LIVE_SNIPE=YES 两个环境变量(双重确认)。
  - 入场/出场前都重新拉一次实时报价,不用缓存价格下单。
  - 每一笔尝试/成交都记进 snipe_orders.jsonl,方便事后审计。

策略本身(用户明确要求的思路,不设固定止盈止损):
  1. 开局买入一笔很小金额(POS_SIZE_USD,默认$5) —— 小金额本身就是风险控制,亏光了
     也就是几美元学费,不是想靠这一笔仓位赚大钱。
  2. 持续监控最新成交,一旦出现"不在已知操盘方钱包名单里、金额超过阈值"的买入,
     判定为疑似真买家入场,不管当前盈亏多少,立刻全部卖出。这是唯一的主动退出
     触发条件——用户明确说了"赚多赚少无所谓,能跑就是赢"。
  3. 独立安全网: 流动性一旦跌破EXIT_LIQ_THRESHOLD(默认$5000,比整晚其他脚本用的
     门槛更低,因为这里买入金额本来就很小),不管有没有等到真买家信号,立刻卖出——
     这条跟"不设止盈止损"不矛盾,是防"根本等不到信号、流动性却先没了"这种更糟的
     结局,复用整晚验证过的LIQ_LOW逻辑。
  4. 已知操盘方钱包名单的识别方法,复用分析TNOS时验证过的思路: GMGN头部交易者
     里带bundler/transfer_in/creator/dev_team标签的,大概率是同一伙人控制的钱包群。

局限性(用户已明确认可这个风险,让我照实现,不要过度设计):
  - 不保证能抢在操盘方砸盘之前跑掉——如果操盘方自己的机器人在同一区块内就对
    真买家信号做出反应并砸盘,这里再快的轮询也来不及,是链上确认速度的物理限制,
    不是代码能优化掉的。
  - 已知操盘方钱包名单只在启动时拉一次快照,操盘方后续换新钱包不会被识别,
    可能被误判成"真买家"信号提前跑掉(误报,不是漏报,是相对安全的误差方向)。

用法(在你自己的机器/VPS上,设置好WALLET_PRIVATE_KEY等环境变量后):
  python snipe_exit.py <池子地址或GeckoTerminal链接>
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path

import requests

try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
except ImportError:
    print("缺依赖: pip install solders requests")
    sys.exit(1)

from git_lock import git_lock, resolve_stuck_merge, run_git, GIT_PULL_CMD

HERE = Path(__file__).parent
JOURNAL_F = HERE / "journal.jsonl"  # 2026-07-29新增: 详细交易台账,所有币共用一份,
                                     # 每笔完整的买卖记一整条(币的详细情况+进场时间点+
                                     # 崩盘特征+仓位),不是简单的买/卖两条散记录
LOG_F = None       # main()里按池子地址派生前缀后赋值,避免自动部署多个币同时跑
ORDERS_LOG_F = None  # 时互相覆盖同一份文件
# 2026-07-30新增: lifecycle_logger.py之前拿pump_lifecycle.json里的live_deployed
# 字段判断"真实仓位名额占没占用",但那个字段代表的是"这个池子曾经部署过实盘"，
# 从来不会清零,而且旁边那个status字段是池子本身死没死(流动性/回撤),跟"我们
# 这笔实盘交易平没平"是两回事——真实买家把我们打出来之后,池子往往还活得好
# 好的,status永远不会变成dead,导致MAX_CONCURRENT_LIVE=1这个名额从第一笔
# 交易之后就被永久占死,后面所有候选都会被跳过,而且不会有任何报错提示,
# 表现就是"只成交了一笔,之后再也没有了"。改用这个独立的标记文件: 建仓成功
# 后创建,finish_trade()时删除,判断"名额占没占用"直接看文件在不在,不跟池子
# 自己的生死状态混在一起。
# 2026-07-31改: 单文件标记支持不了并发>1(N个进程抢同一个文件只有1个能成功),
# 改成目录+每个持仓一个以mint命名的标记文件,跟pregrad_scalp_exit.py共用。
LIVE_POSITIONS_DIR = HERE / ".live_positions"


def live_marker_path(mint):
    safe = re.sub(r"[^A-Za-z0-9]", "", str(mint))[:44]
    return LIVE_POSITIONS_DIR / f"{safe}.json"

SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://lite-api.jup.ag/swap/v1/swap"
GT_BASE = "https://api.geckoterminal.com/api/v2"

GT_S = requests.Session()
GT_S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                     "Accept": "application/json;version=20230302"})
GMGN_S = requests.Session()
GMGN_S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                       "Referer": "https://gmgn.ai/", "Accept": "application/json"})

POS_SIZE_USD = float(os.environ.get("POS_SIZE_USD", "5.0"))
MIN_REAL_BUYER_USD = float(os.environ.get("MIN_REAL_BUYER_USD", "50.0"))
EXIT_LIQ_THRESHOLD = float(os.environ.get("EXIT_LIQ_THRESHOLD", "5000.0"))
POLL_SEC = float(os.environ.get("POLL_SEC", "8"))
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "40"))
SLIPPAGE_BPS = int(os.environ.get("SLIPPAGE_BPS", "300"))
# 2026-07-30新增: 这条腿(mcap_scanner触发的进场,由这个脚本负责退出)之前从来
# 没记过strategy_version,没法像pregrad_ramp那样做干净的调参前后对比——发现
# mcap_scanner那42笔"均值+32.69%"看着亮眼,实际几乎全靠2笔在$1400-2000流动性
# 薄池子里的极端值(957%/454%)撑起来,胜率反而比pregrad_ramp还低,真实资金
# 大概率吃不到这种滑点。从v1开始记,以后调参才有干净的基线可比。
STRATEGY_VERSION = 1

NOW = int(time.time())
NOW_STR = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        pass
    with LOG_F.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def audit(record):
    record["ts"] = int(time.time())
    with ORDERS_LOG_F.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_journal(record):
    """2026-07-29新增: 详细交易台账,一笔完整买卖(从建仓到清仓)写一整条,不是
    audit()那种零散的买/卖动作记录。用户明确要求要详细:币的具体情况(名字/mint/
    锁仓/流动性/MCAP/操盘方钱包结构)、进场时间点(相对这个币自己创建了多久)、
    仓位大小、退出原因、崩盘特征(如果后来死了的话)、是否反复进入同一个币。
    所有币共用一份文件,追加写入,方便以后横向统计对比。"""
    record["written_at"] = int(time.time())
    with JOURNAL_F.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def git_push_journal():
    """2026-07-29新增: 用户要求交易一完成就立刻公开可见,不用等
    lifecycle_runner_loop那10分钟一轮的循环才push。只提交journal.jsonl这一个
    文件(不是整个watch_bot/,避免跟同时在跑的其他脚本互相踩踏太多文件),失败
    重试3次(多个snipe_exit.py实例可能同时在推,跟lifecycle_runner_loop的
    git_sync同一套retry逻辑)。"""
    repo_root = HERE.parent
    def run(cmd):
        return run_git(cmd, repo_root)
    # 2026-07-30新增: VPS并发调到6之后好几个进程同时commit+push互相撞车,单靠
    # 重试次数扛不住,加文件锁让这几个脚本的git操作排队,一次只有一个在做。
    with git_lock() as got_lock:
        if not got_lock:
            log("拿不到git锁(30秒超时,可能有很多进程在排队),这次先不推,交给下一轮补推")
            return
        if resolve_stuck_merge(repo_root, log=log):
            log("清理了上一轮遗留的未解决合并冲突")
        run(["git", "add", "watch_bot/journal.jsonl"])
        commit = run(["git", "commit", "-m", f"trade completed: {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"])
        if commit.returncode != 0 and "nothing to commit" in (commit.stdout + commit.stderr):
            return
        # 2026-07-30再修: 本机screener_local和云端GitHub Actions也在并发push
        # 同一仓库,锁挡不住跨主机的fetch-first冲突,3次重试偶尔追不上,加到
        # 6次+间隔拉长到5秒。
        for _ in range(6):
            push = run(["git", "push"])
            if push.returncode == 0:
                log("交易记录已立刻推送到GitHub,页面刷新即可见")
                return
            # 2026-07-30修复: --rebase遇到journal.jsonl只追加型冲突会卡住需要人工
            # --abort,改用普通merge能自动合并"两边各自追加新行"这种冲突。
            run(GIT_PULL_CMD)
            time.sleep(5)
        log("交易记录推送失败(重试6次),会在下一轮lifecycle循环时补推")


def count_prior_entries(mint):
    """查一下journal里这个mint之前进过几次仓,用来标记"是否反复进入"。"""
    if not JOURNAL_F.exists():
        return 0
    n = 0
    with JOURNAL_F.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("mint") == mint:
                n += 1
    return n


def is_live_mode():
    return os.environ.get("LIVE_TRADING") == "1" and os.environ.get("CONFIRM_LIVE_SNIPE") == "YES"


def get_wallet():
    pk = os.environ.get("WALLET_PRIVATE_KEY")
    if not pk:
        log("FATAL: 环境变量 WALLET_PRIVATE_KEY 未设置，本机不持有私钥无法下单")
        sys.exit(1)
    try:
        return Keypair.from_base58_string(pk)
    except Exception:
        log("FATAL: WALLET_PRIVATE_KEY 格式不对(需要base58编码的私钥字符串)")
        sys.exit(1)


def gt_get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = GT_S.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(3 * (i + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1.5 * (i + 1))
    return None


def extract_addr(arg):
    m = re.search(r"/pools/([A-Za-z0-9]+)", arg)
    return m.group(1) if m else arg


def check_pool_and_mint(addr):
    d = gt_get(f"{GT_BASE}/networks/solana/pools/{addr}", {"include": "base_token"})
    if not d:
        return None, None
    attrs = d["data"]["attributes"]
    mint = None
    for inc in d.get("included", []):
        if inc.get("type") == "token":
            mint = inc["attributes"].get("address")
    return attrs, mint


def get_pool_snapshot(addr):
    d = gt_get(f"{GT_BASE}/networks/solana/pools/{addr}")
    if not d:
        return None
    a = d.get("data", {}).get("attributes", {})
    try:
        price = float(a.get("base_token_price_usd"))
    except (TypeError, ValueError):
        price = None
    try:
        liq = float(a.get("reserve_in_usd"))
    except (TypeError, ValueError):
        liq = None
    return {"price": price, "liq": liq}


def load_known_insiders(mint):
    r = GMGN_S.get(f"https://gmgn.ai/vas/api/v1/token_traders/sol/{mint}", params={"limit": 40}, timeout=15)
    try:
        d = r.json()
    except Exception:
        d = None
    rows = (d or {}).get("data", {}).get("list", []) if d else []
    insiders = set()
    for row in rows:
        tags = row.get("maker_token_tags") or []
        if any(t in tags for t in ("bundler", "transfer_in", "creator", "dev_team")):
            insiders.add(row["address"])
    return insiders


def get_fresh_sol_price_usd():
    d = gt_get(f"{GT_BASE}/networks/solana/tokens/{SOL_MINT}")
    try:
        return float(d["data"]["attributes"]["price_usd"])
    except (KeyError, TypeError, ValueError):
        return None


def jupiter_quote(input_mint, output_mint, amount_lamports, slippage_bps):
    try:
        r = requests.get(JUPITER_QUOTE_URL, params={
            "inputMint": input_mint, "outputMint": output_mint,
            "amount": amount_lamports, "slippageBps": slippage_bps,
        }, timeout=15)
        return r.json() if r.status_code == 200 else None
    except requests.RequestException:
        return None


def jupiter_swap_tx(quote, user_pubkey):
    try:
        r = requests.post(JUPITER_SWAP_URL, json={
            "quoteResponse": quote, "userPublicKey": user_pubkey, "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": "auto",
        }, timeout=15)
        return r.json().get("swapTransaction") if r.status_code == 200 else None
    except requests.RequestException:
        return None


def rpc_call(method, params):
    try:
        r = requests.post("https://api.mainnet-beta.solana.com",
                          json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"error": str(e)}


def sign_and_send(wallet, swap_tx_b64):
    try:
        raw = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(raw)
        tx = VersionedTransaction(tx.message, [wallet])
        sig_b64 = base64.b64encode(bytes(tx)).decode()
    except Exception as e:
        return None, str(e)
    resp = rpc_call("sendTransaction", [sig_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
    if "error" in resp:
        return None, resp["error"]
    return resp["result"], None


def confirm_tx(sig, tries=15):
    for _ in range(tries):
        resp = rpc_call("getSignatureStatuses", [[sig]])
        try:
            status = resp["result"]["value"][0]
        except (KeyError, IndexError, TypeError):
            status = None
        if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
            return status.get("err") is None
        time.sleep(2)
    return False


def do_buy(wallet, mint, pos_size_usd):
    sol_price = get_fresh_sol_price_usd()
    if not sol_price:
        log("SKIP 买入: 拿不到实时SOL价格,不猜")
        return None
    amount_lamports = int(pos_size_usd / sol_price * 1e9)
    quote = jupiter_quote(SOL_MINT, mint, amount_lamports, SLIPPAGE_BPS)
    if not quote:
        log("FAIL 买入: Jupiter拿不到报价(可能还在bonding curve阶段,建议先去pump.fun官网确认能买)")
        return None
    if not is_live_mode():
        log(f"[DRY-RUN] 本来会买入 ${pos_size_usd:.2f} 等值的代币")
        audit({"action": "BUY", "mint": mint, "pos_size_usd": pos_size_usd, "status": "dry_run"})
        return {"qty_raw": int(quote.get("outAmount", 0)), "entry_usd": pos_size_usd, "dry_run": True}
    swap_tx = jupiter_swap_tx(quote, str(wallet.pubkey()))
    if not swap_tx:
        log("FAIL 买入: 构造交易失败")
        audit({"action": "BUY", "mint": mint, "status": "build_failed"})
        return None
    sig, err = sign_and_send(wallet, swap_tx)
    if err:
        log(f"FAIL 买入: 广播失败 {err}")
        audit({"action": "BUY", "mint": mint, "status": "send_failed", "error": err})
        return None
    ok = confirm_tx(sig)
    log(f"买入{'成功' if ok else '未确认(可能失败,自己上链查一下)'} tx={sig}")
    audit({"action": "BUY", "mint": mint, "pos_size_usd": pos_size_usd, "tx": sig, "status": "confirmed" if ok else "unconfirmed"})
    if not ok:
        return None
    # 2026-07-30新增: pos_size_usd是"打算花多少钱",不是"实际到账多少代币对应的
    # 真实成本"——这两个数字在真实下单时应该相等(花了多少钱就是多少钱),但记
    # 下来方便跟卖出时的真实到账USD直接算差值,不用再去猜测。
    return {"qty_raw": int(quote.get("outAmount", 0)), "entry_usd": pos_size_usd, "dry_run": False, "buy_tx": sig}


def get_token_balance_raw(owner_pubkey_str, mint):
    """查钱包里这个币的真实余额(raw数量)。买入报价的outAmount和实际到账会因
    滑点有偏差,卖出必须用真实余额,不然构造出的交易会因为'卖的比持有的多'
    在链上模拟阶段直接被拒。"""
    resp = rpc_call("getTokenAccountsByOwner", [owner_pubkey_str, {"mint": mint}, {"encoding": "jsonParsed"}])
    try:
        accounts = resp["result"]["value"]
        if not accounts:
            return None
        return int(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def do_sell(wallet, mint, qty_raw, reason, dry_run):
    """2026-07-30修复: 之前这个函数什么都不返回,导致finish_trade()记账时只能用
    GT快照价格算pnl_pct。现在返回真实成交结果。

    2026-07-31再修(头两笔实盘的实测教训,两笔卖出全军覆没):
    1. 卖出数量原来用买入报价的outAmount,和实际到账数量有滑点偏差,链上模拟
       直接拒绝——改为卖出前查钱包真实余额。
    2. 原来拿一次报价失败就放弃(Chiikawa那笔: 信号到广播之间价格动了,3%滑点
       容忍被击穿,0x1788拒绝,币卡在钱包里)——改成每次重试都重新拿新报价,
       滑点容忍逐档放宽3%->10%->30%: 这个策略的本质是'能跑就是赢',退出时
       价格差点无所谓,卡在里面出不来才是最大的损失。"""
    if dry_run:
        log(f"[DRY-RUN] 本来会因为[{reason}]全部卖出")
        audit({"action": "SELL", "mint": mint, "reason": reason, "status": "dry_run"})
        return {"dry_run": True, "confirmed": True}

    real_qty = get_token_balance_raw(str(wallet.pubkey()), mint)
    if real_qty:
        if real_qty != qty_raw:
            log(f"卖出数量校正: 报价预估{qty_raw} -> 钱包真实{real_qty}")
        qty_raw = real_qty
    elif real_qty == 0:
        log(f"FAIL 卖出[{reason}]: 钱包里这个币余额为0(买入可能实际没成交?)")
        return {"dry_run": False, "confirmed": False, "error": "zero_balance"}

    last_err = None
    sig = None
    for slippage_bps in (SLIPPAGE_BPS, 1000, 3000):
        quote = jupiter_quote(mint, SOL_MINT, qty_raw, slippage_bps)
        if not quote:
            last_err = "quote_failed"
            log(f"卖出[{reason}]拿不到报价(滑点{slippage_bps/100:.0f}%),重试")
            time.sleep(1.5)
            continue
        swap_tx = jupiter_swap_tx(quote, str(wallet.pubkey()))
        if not swap_tx:
            last_err = "build_failed"
            log(f"卖出[{reason}]构造交易失败(滑点{slippage_bps/100:.0f}%),重试")
            time.sleep(1.5)
            continue
        sig, err = sign_and_send(wallet, swap_tx)
        if err:
            last_err = str(err)
            log(f"卖出[{reason}]广播失败(滑点{slippage_bps/100:.0f}%): {str(err)[:150]},换更高滑点重试")
            time.sleep(1.5)
            continue
        ok = confirm_tx(sig)
        if ok:
            out_sol_lamports = int(quote.get("outAmount", 0))
            sol_price = get_fresh_sol_price_usd()
            exit_usd_actual = (out_sol_lamports / 1e9 * sol_price) if sol_price else None
            extra = f" (约${exit_usd_actual:.2f})" if exit_usd_actual is not None else ""
            log(f"卖出[{reason}]成功(滑点容忍{slippage_bps/100:.0f}%) tx={sig} 换回约{out_sol_lamports/1e9:.4f} SOL{extra}")
            audit({"action": "SELL", "mint": mint, "reason": reason, "tx": sig, "out_lamports": out_sol_lamports,
                  "status": "confirmed", "exit_usd_actual": exit_usd_actual})
            return {"dry_run": False, "confirmed": True, "tx": sig, "out_lamports": out_sol_lamports,
                    "exit_usd_actual": exit_usd_actual}
        last_err = "unconfirmed"
        log(f"卖出[{reason}]交易未确认(滑点{slippage_bps/100:.0f}%) tx={sig},换更高滑点重试")
        time.sleep(1.5)

    log(f"FAIL 卖出[{reason}]: 三档滑点全部失败,最后错误: {str(last_err)[:150]}")
    audit({"action": "SELL", "mint": mint, "reason": reason, "status": "all_retries_failed", "error": str(last_err)[:200]})
    return {"dry_run": False, "confirmed": False, "error": last_err, "tx": sig}


def make_prefix(addr):
    return re.sub(r"[^A-Za-z0-9]", "", addr)[:8]


def finish_trade(entry_info, exit_reason, exit_price, sell_result=None):
    """2026-07-29新增: 一笔交易结束时,把完整的台账记下来——币的详细情况(名字/mint/
    地址/锁仓/流动性/MCAP)、进场时间点(相对这个币自己创建了多久,不是相对我们脚本
    启动的时间)、仓位大小、持仓时长、盈亏、退出原因、这个mint之前进过几次仓(是否
    反复进入)。写进journal.jsonl,所有币共用一份,方便以后横向统计对比。

    2026-07-30再补: pnl_pct是GT快照价格算出来的"理想化零冲击价"盈亏,纸盘和
    实盘一直共用这一套——今天要开始拿真钱测试,如果还只记这个,统计出来的
    还是纸盘那套数字,滑点/优先费完全看不出来,白测了。真实成交时(dry_run=
    False且sell_result里有确认成功的真实到账USD),额外算一版entry_usd_actual/
    exit_usd_actual/pnl_pct_actual,两套数字并排存着,以后统计"真实滑点有多大"
    直接拿pnl_pct_actual减pnl_pct就行,不用再去猜。"""
    entry_price = entry_info["entry_price"]
    pnl_pct = (exit_price / entry_price - 1) * 100 if (entry_price and exit_price) else None
    hold_sec = time.time() - entry_info["entry_ts"]

    entry_usd_actual = entry_info.get("entry_usd_actual")
    exit_usd_actual = sell_result.get("exit_usd_actual") if sell_result else None
    sell_confirmed = sell_result.get("confirmed") if sell_result else None
    pnl_pct_actual = None
    if entry_usd_actual and exit_usd_actual is not None:
        pnl_pct_actual = (exit_usd_actual / entry_usd_actual - 1) * 100
        # 2026-07-31新增(pregrad腿第一笔实盘算出-203%的教训): 现货全仓最多
        # 亏光本金,低于-100%说明记账口径有误,留空不写脏数据进台账。
        if pnl_pct_actual < -100:
            log(f"*** 真实盈亏算出{pnl_pct_actual:.1f}%,现货不可能低于-100%,判定记账口径有误,留空待查 ***")
            pnl_pct_actual = None

    record = {
        "name": entry_info["name"], "mint": entry_info["mint"], "addr": entry_info["addr"],
        "entry_ts": entry_info["entry_ts"], "entry_ts_str": dt.datetime.fromtimestamp(entry_info["entry_ts"]).strftime("%Y-%m-%d %H:%M:%S"),
        "coin_age_min_at_entry": entry_info["coin_age_min_at_entry"],
        "entry_price": entry_price, "entry_liq_usd": entry_info["entry_liq_usd"],
        "entry_locked_liq_pct": entry_info["entry_locked_liq_pct"], "entry_fdv_usd": entry_info["entry_fdv_usd"],
        "n_insiders": entry_info["n_insiders"], "found_via": entry_info.get("found_via"),
        "pos_size_usd": entry_info["pos_size_usd"], "dry_run": entry_info["dry_run"],
        "exit_reason": exit_reason, "exit_price": exit_price, "pnl_pct": pnl_pct,
        "hold_sec": round(hold_sec, 1),
        "prior_entries_on_this_mint": entry_info["prior_entries"],
        "deploy_env": os.environ.get("DEPLOY_ENV", "local"),
        "strategy_version": STRATEGY_VERSION,
        "buy_tx": entry_info.get("buy_tx"),
        "sell_tx": sell_result.get("tx") if sell_result else None,
        "sell_confirmed": sell_confirmed,
        "entry_usd_actual": entry_usd_actual,
        "exit_usd_actual": exit_usd_actual,
        "pnl_pct_actual": pnl_pct_actual,
    }
    write_journal(record)
    if not entry_info["dry_run"]:
        try:
            live_marker_path(entry_info["mint"]).unlink()
        except FileNotFoundError:
            pass
    pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "未知"
    actual_str = f"  真实成交pnl={pnl_pct_actual:+.2f}%" if pnl_pct_actual is not None else ""
    log(f"台账记录完毕: {exit_reason} pnl={pnl_str}{actual_str} 持仓{hold_sec:.0f}秒 "
       f"(这个币之前进过{entry_info['prior_entries']}次仓)")
    git_push_journal()


def lookup_found_via(addr):
    """如果这个池子是被lifecycle_logger自动发现+部署的,从pump_lifecycle.json里
    查一下当初是靠起点MCAP/早期涨幅/后期涨幅哪道信号发现的,记进台账方便以后
    对比"哪种发现方式选出来的币质量更好"。查不到就返回None,不影响交易流程。"""
    try:
        db = json.loads((HERE / "pump_lifecycle.json").read_text(encoding="utf-8"))
        return db.get(addr, {}).get("found_via")
    except Exception:
        return None


def main():
    global LOG_F, ORDERS_LOG_F
    if len(sys.argv) < 2:
        print("用法: python snipe_exit.py <池子地址或GeckoTerminal链接>")
        return
    addr = extract_addr(sys.argv[1])
    prefix = make_prefix(addr)
    LOG_F = HERE / f"{prefix}_snipe_exit.log"
    ORDERS_LOG_F = HERE / f"{prefix}_snipe_orders.jsonl"

    attrs, mint = check_pool_and_mint(addr)
    if not attrs or not mint:
        log("查不到这个池子,地址可能不对")
        return

    log(f"=== {attrs.get('name')} ({addr[:10]}...) ===")
    log(f"模式: {'实盘(真金白银)' if is_live_mode() else 'DRY-RUN(只打印,不下单)'}  仓位=${POS_SIZE_USD:.2f}  "
       f"真买家阈值=${MIN_REAL_BUYER_USD:.0f}  流动性安全网=${EXIT_LIQ_THRESHOLD:,.0f}")

    insiders = load_known_insiders(mint)
    log(f"已知操盘方钱包(bundler/transfer_in/creator/dev_team标签): {len(insiders)}个")

    prior_entries = count_prior_entries(mint)
    if prior_entries:
        log(f"注意: 这个币之前已经进过{prior_entries}次仓了,这次是反复进入")

    try:
        created = dt.datetime.fromisoformat(attrs["pool_created_at"].replace("Z", "+00:00"))
        coin_age_min = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 60
    except (KeyError, ValueError, TypeError):
        coin_age_min = None
    try:
        entry_price = float(attrs.get("base_token_price_usd") or 0)
    except (TypeError, ValueError):
        entry_price = None

    marker_fd = None
    if is_live_mode():
        # 2026-07-31修复(头两笔实盘实测教训): Coupe那笔买进了流动性只剩$0.74的
        # 死池子——纸盘时代这种是无害的0%记录,实盘就是$5买进去出不来($4.6打水漂)。
        # 实盘入场前必须有流动性下限,门槛跟pregrad v5验证过的$10K保持一致。
        try:
            entry_liq = float(attrs.get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            entry_liq = 0
        min_liq = float(os.environ.get("LIVE_MIN_ENTRY_LIQ_USD", "10000"))
        if entry_liq < min_liq:
            log(f"实盘入场流动性检查不过: ${entry_liq:,.0f} < ${min_liq:,.0f},放弃这个候选(纸盘数据证明这种池子买进去就出不来)")
            return

        # 2026-07-31修复: 头两笔实盘同一秒建仓,名额被并发竞速绕过(原来标记是
        # 买入确认后才写,两个进程都赶在对方写之前各买了一笔)。改成买入前用
        # O_CREAT|O_EXCL原子抢占,抢不到就放弃,买入失败时释放。
        # 再改: 单文件->目录+每币一个标记,支持并发>1;抢到后复查总数防竞速超限。
        LIVE_POSITIONS_DIR.mkdir(exist_ok=True)
        max_live = int(os.environ.get("MAX_CONCURRENT_LIVE", "1"))
        marker_path = live_marker_path(mint)
        try:
            marker_fd = os.open(str(marker_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            log(f"这个币已经有实盘仓位在跑({marker_path.name}),放弃重复建仓")
            return
        n_now = len(list(LIVE_POSITIONS_DIR.glob("*.json")))
        if n_now > max_live:
            log(f"抢到标记后复查发现并发已达{n_now}>{max_live},退回名额放弃这个候选")
            os.close(marker_fd)
            try:
                marker_path.unlink()
            except FileNotFoundError:
                pass
            return

    wallet = get_wallet() if is_live_mode() else None
    entry_ts = time.time()
    pos = do_buy(wallet, mint, POS_SIZE_USD)
    if not pos:
        log("买入没成功,监控结束")
        if marker_fd is not None:
            os.close(marker_fd)
            try:
                live_marker_path(mint).unlink()
            except FileNotFoundError:
                pass
        return

    if not pos["dry_run"] and marker_fd is not None:
        # 2026-07-30新增: 监控窗口实时显示"正在交易的币"需要的信息写进标记文件。
        # 标记文件本身在买入前已经原子抢占,这里只是补充内容。
        try:
            os.write(marker_fd, json.dumps({
                "addr": addr, "mint": mint, "name": attrs.get("name"),
                "opened_at": entry_ts, "entry_price": entry_price,
                "pos_size_usd": POS_SIZE_USD,
            }, ensure_ascii=False).encode("utf-8"))
            os.close(marker_fd)
        except OSError:
            pass

    entry_info = {
        "name": attrs.get("name"), "mint": mint, "addr": addr, "entry_ts": entry_ts,
        "coin_age_min_at_entry": coin_age_min, "entry_price": entry_price,
        "entry_liq_usd": attrs.get("reserve_in_usd"), "entry_locked_liq_pct": attrs.get("locked_liquidity_percentage"),
        "entry_fdv_usd": attrs.get("fdv_usd"), "n_insiders": len(insiders), "found_via": lookup_found_via(addr),
        "pos_size_usd": POS_SIZE_USD, "dry_run": pos["dry_run"], "prior_entries": prior_entries,
        "entry_usd_actual": pos["entry_usd"] if not pos["dry_run"] else None,
        "buy_tx": pos.get("buy_tx"),
    }

    log(f"持仓建立: qty_raw={pos['qty_raw']} entry=${pos['entry_usd']:.2f} entry_price={entry_price}  开始监控退出信号...")

    seen_tx = set()
    first_pass = True
    deadline = time.time() + MAX_MINUTES * 60
    while time.time() < deadline:
        snap = get_pool_snapshot(addr)
        if snap and snap["liq"] is not None and snap["liq"] < EXIT_LIQ_THRESHOLD:
            log(f"*** 流动性安全网触发: 只剩${snap['liq']:,.0f},不等真买家信号了,立刻卖出 ***")
            sell_result = do_sell(wallet, mint, pos["qty_raw"], "LIQ_LOW", pos["dry_run"])
            finish_trade(entry_info, "LIQ_LOW", snap.get("price"), sell_result)
            return

        d = gt_get(f"{GT_BASE}/networks/solana/pools/{addr}/trades", {"trade_volume_in_usd_greater_than": 0})
        rows = (d or {}).get("data", [])
        rows.sort(key=lambda r: r["attributes"]["block_timestamp"])
        new_rows = [r for r in rows if r["attributes"]["tx_hash"] not in seen_tx]
        for row in new_rows:
            a = row["attributes"]
            seen_tx.add(a["tx_hash"])
            if first_pass:
                continue
            w = a.get("tx_from_address")
            usd = float(a.get("volume_in_usd") or 0)
            # 2026-07-29新增(用户明确要求): 已知操盘方钱包一旦出现卖出,立刻跑,
            # 不等流动性暴跌或者外部买家信号——这是比另外两个信号更早的一层。
            # TNOS那次实测: 一笔比背景噪音大5-10倍的操盘方卖出,21秒后价格就崩了
            # 98%,用户判断"21秒够机器人反应",这里直接测试这个假设是否成立——
            # 如果跑得比操盘方晚,pnl会是大幅负数,直接记录进台账,不用猜。
            # 2026-07-29修复: 第一次实测(TNOS2)就暴露问题——不设金额门槛,任何
            # 一笔操盘方卖出都触发,结果被一笔$17.17的正常背景噪音提前清仓,
            # pnl几乎为0,不是真的死钱,是白白错过了后续还在健康上涨的仓位。
            # 跟check_wallet_dumping同一个教训: 操盘方偶尔卖1-2笔小单是正常获利
            # 了结,得设个门槛过滤掉背景噪音,只对明显异常放量的卖出反应。
            if a["kind"] == "sell" and w in insiders and usd >= MIN_REAL_BUYER_USD:
                log(f"*** 已知操盘方钱包卖出信号: {w[:10]}...卖出${usd:,.2f},立刻卖出,不等更多确认 ***")
                exit_snap = get_pool_snapshot(addr)
                sell_result = do_sell(wallet, mint, pos["qty_raw"], "INSIDER_SELL_DETECTED", pos["dry_run"])
                finish_trade(entry_info, "INSIDER_SELL_DETECTED", exit_snap.get("price") if exit_snap else None, sell_result)
                return
            if a["kind"] == "buy" and w and w not in insiders and usd >= MIN_REAL_BUYER_USD:
                log(f"*** 疑似真买家信号: 钱包{w[:10]}...买入${usd:,.2f},立刻卖出,不等更多确认 ***")
                exit_snap = get_pool_snapshot(addr)
                sell_result = do_sell(wallet, mint, pos["qty_raw"], "REAL_BUYER_DETECTED", pos["dry_run"])
                finish_trade(entry_info, "REAL_BUYER_DETECTED", exit_snap.get("price") if exit_snap else None, sell_result)
                return
        first_pass = False
        time.sleep(POLL_SEC)

    log(f"=== {MAX_MINUTES:.0f}分钟窗口结束,没等到真买家信号,仍持有(需要自己决定要不要手动处理) ===")
    if not pos["dry_run"]:
        log("*** 这是真实仓位,脚本不会替你卖出——上面这行不是提示,是真的还拿着真钱买的代币,自己去钱包/Jupiter看着办 ***")
    timeout_snap = get_pool_snapshot(addr)
    # sell_result不传(保持None): 这里没有真实卖出发生,pnl_pct_actual必须留空,
    # 不能假装这笔交易已经"实现"了,那样会污染真实盈亏的统计。
    finish_trade(entry_info, "TIMEOUT_STILL_HOLDING", timeout_snap.get("price") if timeout_snap else None)


if __name__ == "__main__":
    main()
