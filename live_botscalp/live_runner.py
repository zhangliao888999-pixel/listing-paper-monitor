# -*- coding: utf-8 -*-
"""策略D 实盘执行器 —— 薅机器人羊毛，Solana真实链上swap版本。

安全边界(照抄Polymarket那套已经跑通的模式，不是新发明)：
  - 这份代码从来不会把私钥发送到任何地方，也不会打印/写日志包含私钥。私钥只从
    环境变量 WALLET_PRIVATE_KEY 读进内存，用完即弃。**必须由用户自己在自己的机器/
    服务器上设置这个环境变量并启动本脚本** —— 我(Claude)不会持有你的私钥，
    不会在我这边的会话/环境里运行这份代码去真实下单。你部署、你启动、你负责。
  - 默认 DRY_RUN=1(只打印"本来会做什么"，不广播真实交易)。要真正下单，
    必须同时设置 LIVE_TRADING=1 和 CONFIRM_LIVE_BOTSCALP=YES 两个环境变量
    (双重确认，防止手滑)。
  - 单笔金额上限/每日累计金额上限/每日亏损熔断三道闸门，任何一个触发就停止开新仓。
  - 入场/出场前都会重新拉一次实时报价再执行(Polymarket那边吃过"用了几秒钟前的
    过期报价下单，结果价格已经跌穿"的亏，这里直接照搬这个教训——绝不用缓存价格
    直接下单)。
  - 每一笔尝试/成交都记进 live_orders.jsonl，方便事后审计。

依赖: pip install -r requirements.txt (solders + requests)
执行方式(在你自己的机器上): 见 README_CN.md
"""
import base64
import json
import os
import statistics
import sys
import time
import datetime as dt
from pathlib import Path

import requests

try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    from solders.commitment_config import CommitmentLevel
    from solders.rpc.requests import SendVersionedTransaction, GetSignatureStatuses
    from solders.rpc.config import RpcSendTransactionConfig
except ImportError:
    print("缺依赖: pip install -r requirements.txt")
    sys.exit(1)

HERE = Path(__file__).parent
CONFIG_F = HERE / "config.live.json"
STATE_F = HERE / "live_state.json"
ORDERS_LOG_F = HERE / "live_orders.jsonl"
LOG_F = HERE / "live_runner.log"
# 只在VPS本地生成,从不git add/push——这是真实钱包的持仓和盈亏,推到公开仓库
# 等于把实盘账户信息暴露给所有人,跟纸盘那个公开看盘页面必须分开处理
DASHBOARD_F = HERE / "DASHBOARD_LIVE.md"
DASHBOARD_HTML_F = HERE / "DASHBOARD_LIVE.html"

SOL_MINT = "So11111111111111111111111111111111111111112"
# 注意: 老的 quote-api.jup.ag(v6) 已经失效(DNS都解析不到了),Jupiter把免费公共接口
# 迁移到了 lite-api.jup.ag(付费/认证走api.jup.ag)。这里用免费版，实盘量大了考虑升级。
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://lite-api.jup.ag/swap/v1/swap"
GT_BASE = "https://api.geckoterminal.com/api/v2"

GT_S = requests.Session()
GT_S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                     "Accept": "application/json;version=20230302"})

NOW = int(time.time())
NOW_STR = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


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


def check_scalping(addr):
    """跟check_coin.py里的同名函数逻辑完全一样,直接内联复制过来，让live_botscalp/
    这个文件夹可以整个单独打包部署，不需要依赖上一级目录的check_coin.py。
    检查最近的逐笔成交里,是不是被同一个钱包反复买卖刷量(做市/套利机器人的常见特征)。
    关键不是这个钱包占了总成交的多大比例(取样窗口跨度长的话占比会被稀释拉低)，
    而是它自己前后两笔交易之间隔了多久——真人不可能几秒钟到几十秒钟就买卖反手一次，
    连续做几分钟。"""
    d = gt_get(f"{GT_BASE}/networks/solana/pools/{addr}/trades")
    rows = (d or {}).get("data", [])
    if len(rows) < 10:
        return {"n_trades": len(rows), "verdict": "成交笔数不够,跳过刷量检查", "flag": False}

    by_wallet = {}
    for row in rows:
        a = row["attributes"]
        w = a.get("tx_from_address")
        if w:
            by_wallet.setdefault(w, []).append(a)

    def parse_ts(a):
        return dt.datetime.fromisoformat(a["block_timestamp"].replace("Z", "+00:00")).timestamp()

    best = None
    for w, trades in by_wallet.items():
        if len(trades) < 8:
            continue
        kinds = {t["kind"] for t in trades}
        if not ("buy" in kinds and "sell" in kinds):
            continue
        ts = sorted(parse_ts(t) for t in trades)
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        median_gap = statistics.median(gaps)
        if best is None or median_gap < best["median_gap_s"]:
            avg_usd = statistics.mean(float(t["volume_in_usd"]) for t in trades)
            best = {"wallet": w, "n_trades": len(trades), "median_gap_s": median_gap, "avg_trade_usd": avg_usd}

    flag = best is not None and best["median_gap_s"] < 60
    result = {"n_trades": len(rows), "n_wallets": len(by_wallet), "flag": flag}
    if best:
        result.update({"suspect_wallet": best["wallet"], "suspect_wallet_trades": best["n_trades"],
                       "suspect_wallet_median_gap_s": round(best["median_gap_s"], 1),
                       "suspect_wallet_avg_trade_usd": round(best["avg_trade_usd"], 2)})
    return result


def log(msg):
    # 用调用这一刻的真实时间,不用NOW_STR(那是整个进程启动时刻算的,一轮扫描里
    # 好几十个候选跑下来实际经过了几秒到几十秒,如果都打同一个时间戳,实时tail
    # 日志的时候就看不出"现在具体卡在哪一步"了
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        pass
    with LOG_F.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    default = {
        "rpcUrl": "https://api.mainnet-beta.solana.com",
        # 仓位模式两选一:
        #   "fixed"     - 每笔固定posSizeUsd金额，用于$1这种最小化验证阶段
        #   "pct_of_bot"- 跟纸盘bot_scalp_monitor.py同一套逻辑:目标机器人钱包场均单笔
        #                 交易金额的pctOfBot(默认10%)，下限minPosUsd上限maxPosUsd
        # 验证阶段确认没bug之后，把sizingMode改成pct_of_bot就是跟纸盘一样的动态仓位，
        # 不需要改代码。
        "sizingMode": "fixed",
        "posSizeUsd": 1.0,           # fixed模式下每笔的金额;先用$1验证整条链路能不能跑通
        "pctOfBot": 0.10, "minPosUsd": 5.0, "maxPosUsd": 50.0,   # pct_of_bot模式的参数,对齐纸盘
        "tp": 0.05, "sl": -0.03, "maxHoldMin": 30,
        "slippageBps": 200,          # 2%,链上真实滑点比模拟盘假设的更真实,新币薄流动性给够余量
        "maxPositions": 3,           # 实盘先保守,远小于模拟盘的MAX_POS=20
        "dailyMaxUsd": 50.0,         # 每日累计开仓金额上限
        "dailyLossKillUsd": -20.0,   # 每日已实现亏损到这个数就停止开新仓(不影响已开仓位的止损平仓)
        # 2026-07-27新增: breadcat闪崩事故后加的两道"进得去也要出得来"检查。
        # 根因是同一个:breadcat崩盘后一查,池子真实流动性只剩$7.94,但24小时成交量
        # 高达$1565万——成交量/流动性比值离谱到200万倍,典型"看着很活跃、兜不住"的池子。
        # "闪崩后清零"和"想卖卖不掉(CXMT/Grok那种)"表面现象不同,但都是同一个根因
        # (真实深度不够)导致的,买入前直接量深度比事后猜特征更直接。
        "minLiquidityUsd": 15000.0,   # 流动性低于这个数,不管多活跃都不进场
        "maxPriceImpactPct": 5.0,     # 买这一笔仓位对价格的冲击超过这个百分比,说明池子薄到连
                                       # 我们这个小仓位都扛不住,大概率也卖不出去,不进场
        # 2026-07-27新增: 用户拿SPY/SOL这个真实案例点出的"拉高出货"风险,三条新防线:
        "minLockedLiqPct": 50.0,      # 流动性锁仓比例低于这个数,LP随时能被抽干,不进场
        "maxBuySellRatio": 30.0,      # 过去1小时买家人数/卖家人数超过这个比例,像是"广撒网
                                       # 吸引散户接盘,少数人偷偷出货",不进场
        "maxRecentPumpPct": 100.0,    # 过去1小时已经涨了这么多,大概率已经涨过头,追进去等于
                                       # 替别人接盘,不进场
        # 2026-07-27新增: 用户明确指出"流动性比3%止损重要得多——流动性没了是100%全亏,
        # 3%止损根本不算什么"。买候选慢(一轮要查10-15个池子的尽调),但盯着手上2-3个
        # 持仓的流动性很快,不用等下一次计划任务触发(最快1分钟)才复查一次——脚本内部
        # 在扫描完新候选后,进入一段高频复查窗口,持续盯着已有持仓直到没仓位或者到时间。
        "exitCheckIntervalSec": 15,   # 持仓期间每隔几秒重新查一次流动性/价格
        "exitCheckWindowSec": 150,    # 这段高频复查窗口最长跑多久(留量给计划任务下一次触发)
    }
    if CONFIG_F.exists():
        default.update(json.loads(CONFIG_F.read_text(encoding="utf-8")))
    return default


def decide_pos_size_usd(cfg, addr):
    """按sizingMode算这一笔要用多少钱。pct_of_bot模式需要重新查一次这个池子的
    刷量机器人场均单笔金额(跟check_coin.py/check_scalping同一套检测,只用GeckoTerminal，
    不占GMGN配额)——如果这时候已经检测不到机器人了(市场状况变了)，就跳过这笔，
    不用旧数据硬凑仓位。"""
    if cfg["sizingMode"] == "fixed":
        return cfg["posSizeUsd"]
    result = check_scalping(addr)
    avg_bot_usd = result.get("suspect_wallet_avg_trade_usd")
    if not result.get("flag") or not avg_bot_usd:
        return None
    return max(cfg["minPosUsd"], min(cfg["maxPosUsd"], avg_bot_usd * cfg["pctOfBot"]))


def get_wallet():
    """私钥只从环境变量读，从不写日志、从不落盘、从不发网络请求携带。"""
    pk = os.environ.get("WALLET_PRIVATE_KEY")
    if not pk:
        log("FATAL: 环境变量 WALLET_PRIVATE_KEY 未设置，本机不持有私钥无法下单")
        sys.exit(1)
    try:
        return Keypair.from_base58_string(pk)
    except Exception:
        log("FATAL: WALLET_PRIVATE_KEY 格式不对(需要base58编码的私钥字符串)")
        sys.exit(1)


def is_live_mode():
    return os.environ.get("LIVE_TRADING") == "1" and os.environ.get("CONFIRM_LIVE_BOTSCALP") == "YES"


def rpc_call(cfg, method, params):
    """所有网络调用都不能让异常直接往外冒——这是要无人值守跑很多轮的脚本，一次
    DNS抖动/超时就把整个进程崩掉的话，持仓监控(止盈止损)也会跟着停摆，风险更大。"""
    try:
        r = requests.post(cfg["rpcUrl"], json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"error": str(e)}


def get_fresh_price_usd(mint_or_pool_addr, is_pool_addr=True):
    """执行前必须重新拉一次实时价格,绝不用几秒/几十秒前缓存的信号价格下单
    (Polymarket live那边的教训：stale book导致按已经跌穿的过期价格进场，亏了钱才查出来)。
    注意: pools接口的价格字段叫base_token_price_usd,tokens接口叫price_usd,两个
    endpoint字段名不一样,一开始写错导致SOL价格一直悄悄拿不到、静默retreat到硬编码
    fallback——那个fallback现在也删了，拿不到就是拿不到，不能悄悄用一个可能过期很久的假数字。"""
    if is_pool_addr:
        url = f"{GT_BASE}/networks/solana/pools/{mint_or_pool_addr}"
        field = "base_token_price_usd"
    else:
        url = f"{GT_BASE}/networks/solana/tokens/{mint_or_pool_addr}"
        field = "price_usd"
    # 2026-07-27修复: 这里原来是自己裸调requests.get,不走下面gt_get()那套429退避重试,
    # 也没用带User-Agent的GT_S会话——1分钟一轮扫描+管理持仓,请求频率一高就被限流,
    # 一撞到429就直接放弃,导致整轮所有候选/持仓齐刷刷"拿不到实时报价"。改成复用gt_get()。
    d = gt_get(url)
    if not d:
        return None
    try:
        return float(d["data"]["attributes"][field])
    except (KeyError, ValueError, TypeError):
        return None


def get_pool_diligence(addr):
    """2026-07-27新增: 只在进场前调用一次(不是每轮exit check都调,避免加重限流)。
    用户拿SPY/SOL这个真实池子举例点出的风险: 池子创建2-3小时内涨335%,过去1小时
    3011个钱包在买、只有38个在卖(典型"广撒网吸引散户接盘,少数人偷偷出货"信号)，
    最要命的是locked_liquidity_percentage(流动性锁仓比例)几乎是0——流动性提供方
    随时能把池子一次性抽干，这跟"流动性慢慢枯竭"是完全不同的风险，是主动rug机制。
    这几个字段本来就在同一个pools接口响应里，跟查价格是同一次请求，不用额外调用。"""
    d = gt_get(f"{GT_BASE}/networks/solana/pools/{addr}")
    if not d:
        return None
    a = d.get("data", {}).get("attributes", {})
    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    h1 = a.get("transactions", {}).get("h1", {}) or {}
    return {
        "price": _f(a.get("base_token_price_usd")),
        "liq": _f(a.get("reserve_in_usd")),
        "locked_liq_pct": _f(a.get("locked_liquidity_percentage")),
        "buyers_h1": h1.get("buyers"),
        "sellers_h1": h1.get("sellers"),
        "price_change_h1_pct": _f((a.get("price_change_percentage") or {}).get("h1")),
    }


def jupiter_quote(input_mint, output_mint, amount_lamports, slippage_bps):
    try:
        r = requests.get(JUPITER_QUOTE_URL, params={
            "inputMint": input_mint, "outputMint": output_mint,
            "amount": amount_lamports, "slippageBps": slippage_bps,
        }, timeout=15)
        if r.status_code != 200:
            return None
        return r.json()
    except requests.RequestException:
        return None


def jupiter_swap_tx(quote, user_pubkey):
    try:
        r = requests.post(JUPITER_SWAP_URL, json={
            "quoteResponse": quote, "userPublicKey": user_pubkey, "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": "auto",
        }, timeout=15)
        if r.status_code != 200:
            return None
        return r.json().get("swapTransaction")
    except requests.RequestException:
        return None


def sign_and_send(cfg, wallet, swap_tx_b64):
    try:
        raw = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(raw)
        tx = VersionedTransaction(tx.message, [wallet])
        sig_b64 = base64.b64encode(bytes(tx)).decode()
    except Exception as e:
        return None, str(e)
    resp = rpc_call(cfg, "sendTransaction", [sig_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
    if "error" in resp:
        return None, resp["error"]
    return resp["result"], None


def confirm_tx(cfg, sig, tries=15):
    for _ in range(tries):
        resp = rpc_call(cfg, "getSignatureStatuses", [[sig]])
        try:
            status = resp["result"]["value"][0]
        except (KeyError, IndexError, TypeError):
            status = None
        if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
            return status.get("err") is None
        time.sleep(2)
    return False


def audit(record):
    record["ts"] = NOW
    with ORDERS_LOG_F.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_state():
    if STATE_F.exists():
        return json.loads(STATE_F.read_text(encoding="utf-8"))
    return {"positions": {}, "closed": [], "realized_pnl_usd": 0.0, "spent_today_usd": 0.0, "day": NOW_STR[:10]}


def save_state(state):
    STATE_F.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


CANDIDATES_URLS = [
    "https://raw.githubusercontent.com/zhangliao888999-pixel/listing-paper-monitor/master/screener_candidates.json",
    "https://raw.githubusercontent.com/zhangliao888999-pixel/listing-paper-monitor/master/screener_candidates_local.json",
]


# 2026-07-27确认: GeckoTerminal对这个池子的报价持续异常(同一小时内先后读到
# 0.00006/0.32/0.045/0.12/0.16这种互相对不上的"价格")，导致4笔$1测试仓位
# 都在错误的止盈止损信号下平仓、实际净亏约-$3.66。不是一次性坏点，是这个
# 池子的数据源本身不可信，直接拉黑不再进场。
BLOCKED_POOL_ADDRS = {
    "FLUMAEUTHQ3X8xzAQdGA45BXS94yNjkmDZHx9WTR3fCA",  # CXMT / SOL
}


def load_bot_candidates():
    """跟bot_scalp_monitor.py同一个信号源: 复用screener已经在跑的候选扫描。
    直接从GitHub在线拉取(跟看盘页面同一套公开raw文件)，不依赖本地文件/仓库clone结构——
    这样这个live_botscalp文件夹可以单独打包、单独部署，不需要连着paper/仓库其它文件一起搬。
    (代价: raw.githubusercontent.com有几分钟到几十分钟不等的CDN缓存延迟，候选数据不是
    绝对实时，但反正每次下单前也会重新拉一次实时报价再确认，不影响安全性)"""
    cands, seen = [], set()
    for url in CANDIDATES_URLS:
        try:
            r = requests.get(url, params={"_": int(time.time())}, timeout=15)
            if r.status_code != 200:
                continue
            d = r.json()
        except (requests.RequestException, ValueError):
            continue
        for c in d.get("candidates", []):
            if c.get("scalping_flag") and c["addr"] not in seen and c["addr"] not in BLOCKED_POOL_ADDRS:
                seen.add(c["addr"])
                cands.append(c)
    return cands


def try_enter(cfg, wallet, state, c):
    if c["addr"] in state["positions"] or len(state["positions"]) >= cfg["maxPositions"]:
        return False
    if state["realized_pnl_usd"] <= cfg["dailyLossKillUsd"]:
        log(f"SKIP {c['name']}: 今日已实现亏损${state['realized_pnl_usd']:.2f}已触发熔断,停止开新仓")
        return False
    liq = c.get("liq")
    if liq is not None and liq < cfg["minLiquidityUsd"]:
        log(f"SKIP {c['name']}: 流动性只有${liq:,.0f},低于${cfg['minLiquidityUsd']:,.0f}门槛,进得去也可能出不来")
        return False

    pos_size_usd = decide_pos_size_usd(cfg, c["addr"])
    if pos_size_usd is None:
        log(f"SKIP {c['name']}: pct_of_bot模式下现在查不到机器人在刷(市场状况变了),不用旧数据凑仓位")
        return False
    if state["spent_today_usd"] + pos_size_usd > cfg["dailyMaxUsd"]:
        log(f"SKIP {c['name']}: 今日累计开仓将超过上限${cfg['dailyMaxUsd']}")
        return False

    dil = get_pool_diligence(c["addr"])
    if dil is None or dil["price"] is None:
        log(f"SKIP {c['name']}: 拿不到实时报价")
        return False
    fresh_price = dil["price"]

    # 2026-07-27曾经改成三条全部只记录不拦截(发现锁仓比例一上来就100%拦截所有候选)，
    # 但2026-07-28锁仓比例这条改回硬拦截了(见下面),只有买卖比例/涨幅这两条继续记录观察。
    buyers, sellers = dil["buyers_h1"], dil["sellers_h1"]
    buy_sell_ratio = None
    if buyers and sellers is not None:
        buy_sell_ratio = float("inf") if (sellers == 0 and buyers > 20) else buyers / max(sellers, 1)
    # 2026-07-28改回硬拦截: 用户当晚亲身踩坑印证——锁仓比例低意味着主力随时能一笔交易
    # 瞬间抽干流动性,这个动作在链上一个区块内就完成,不管止盈止损检查多快(15秒/1秒都
    # 一样)都来不及反应,是物理时间差不是代码速度问题。检查再快只能防"慢慢流失"，防不住
    # "瞬间抽干"，唯一有效的办法是压根不进没锁仓的池子。买卖比例/涨幅这两条留作记录观察
    # (那两个是"过程类"信号,反应速度还有意义),只有这条改回硬拦截。
    if dil["locked_liq_pct"] is not None and dil["locked_liq_pct"] < cfg["minLockedLiqPct"]:
        log(f"SKIP {c['name']}: 流动性锁仓比例只有{dil['locked_liq_pct']:.1f}%,主力随时能一笔交易瞬间抽干,不进场")
        return False
    if buy_sell_ratio is not None and buy_sell_ratio > cfg["maxBuySellRatio"]:
        log(f"NOTE(未拦截) {c['name']}: 过去1小时买家{buyers}人/卖家{sellers}人,比例{buy_sell_ratio:.0f}:1")
    if dil["price_change_h1_pct"] is not None and dil["price_change_h1_pct"] > cfg["maxRecentPumpPct"]:
        log(f"NOTE(未拦截) {c['name']}: 过去1小时已经涨了{dil['price_change_h1_pct']:.0f}%")

    mint = c.get("mint")
    if not mint:
        log(f"SKIP {c['name']}: 没有mint地址")
        return False

    # 用实时SOL价格换算lamports金额；拿不到就跳过这一轮,不用过期/瞎猜的价格算仓位大小
    # (吃过亏: 一开始这里写死了个$150兜底，SOL真实价格才$75，等于每次仓位都算大了将近2倍)
    sol_price_usd = get_fresh_price_usd(SOL_MINT, is_pool_addr=False)
    if not sol_price_usd:
        log(f"SKIP {c['name']}: 拿不到实时SOL价格,不猜")
        return False
    amount_lamports = int(pos_size_usd / sol_price_usd * 1e9)

    quote = jupiter_quote(SOL_MINT, mint, amount_lamports, cfg["slippageBps"])
    if not quote:
        log(f"SKIP {c['name']}: Jupiter拿不到报价(可能流动性不足以支撑这笔金额)")
        return False
    try:
        price_impact = float(quote.get("priceImpactPct", 0)) * 100
    except (TypeError, ValueError):
        price_impact = 0
    if price_impact > cfg["maxPriceImpactPct"]:
        log(f"SKIP {c['name']}: 买入这笔仓位本身冲击价格达{price_impact:.1f}%(超过{cfg['maxPriceImpactPct']}%门槛),池子太薄")
        return False

    record = {"action": "BUY", "name": c["name"], "addr": c["addr"], "mint": mint,
             "fresh_price_usd": fresh_price, "pos_size_usd": pos_size_usd,
             "sizing_mode": cfg["sizingMode"], "live_mode": is_live_mode()}

    if not is_live_mode():
        log(f"[DRY-RUN] 本来会 BUY {c['name']} ({c['addr'][:8]}...) 报价内={fresh_price:.10g} "
           f"仓位=${pos_size_usd:.2f}({cfg['sizingMode']})")
        audit({**record, "status": "dry_run"})
        return False  # dry-run不记为真实持仓

    swap_tx = jupiter_swap_tx(quote, str(wallet.pubkey()))
    if not swap_tx:
        log(f"FAIL {c['name']}: Jupiter构造交易失败")
        audit({**record, "status": "build_failed"})
        return False

    sig, err = sign_and_send(cfg, wallet, swap_tx)
    if err:
        log(f"FAIL {c['name']}: 广播失败 {err}")
        audit({**record, "status": "send_failed", "error": str(err)})
        return False

    ok = confirm_tx(cfg, sig)
    if not ok:
        log(f"FAIL {c['name']}: 交易未确认成功 sig={sig}")
        audit({**record, "status": "unconfirmed", "sig": sig})
        return False

    out_amount = int(quote["outAmount"])
    state["positions"][c["addr"]] = {
        "name": c["name"], "mint": mint, "entry_price_usd": fresh_price,
        "qty_raw": out_amount, "usd": pos_size_usd, "t_entry": NOW, "buy_sig": sig,
        # 入场时的尽调快照,只记录不拦截,明天回看这些值分布跟实际盈亏的关系再定门槛。
        # ratio存None统一代表"数据缺失或买家人数远超卖家(sellers=0)导致比例无穷大",
        # 靠下面两个原始人数字段区分是哪种情况,JSON不支持Infinity所以不能直接存
        "entry_locked_liq_pct": dil["locked_liq_pct"],
        "entry_buyers_h1": buyers, "entry_sellers_h1": sellers,
        "entry_buy_sell_ratio_h1": None if buy_sell_ratio == float("inf") else buy_sell_ratio,
        "entry_price_change_h1_pct": dil["price_change_h1_pct"],
    }
    state["spent_today_usd"] += pos_size_usd
    log(f"BUY {c['name']} ({c['addr'][:8]}...) 成交 sig={sig} 价格={fresh_price:.10g} 仓位=${pos_size_usd:.2f}")
    audit({**record, "status": "filled", "sig": sig})
    return True


def try_exit(cfg, wallet, state, addr, pos):
    dil = get_pool_diligence(addr)
    fresh_price = dil["price"] if dil else None
    if fresh_price is None:
        # 2026-07-27修复: 这里原来是静默return,拿不到报价就完全不出现在日志里——
        # 排查Tepe那次时发现持仓已经开了几轮,日志里却对这个仓位只字未提,
        # 完全看不出是"没查到价格"还是别的原因。现在必须至少留一行痕迹。
        log(f"SKIP EXIT CHECK {pos['name']}: 拿不到实时报价,这轮跳过止盈止损判断")
        return

    # 2026-07-27新增(用户明确要求): 流动性快要不足,不管别的条件,必须马上卖出——
    # 这个检查独立在最前面、不受下面abs(ret)>50那道"疑似坏数据跳过"哨兵影响,因为
    # 流动性告急是另一个独立信号,不依赖(可能出错的)价格比例判断,该跑就必须跑,
    # 等真的跌到DEAD_LIQ级别再处理就晚了(CXMT/Grok/breadcat都是流动性没了才发现)。
    liq_now = dil["liq"]
    reason = None
    if liq_now is not None and liq_now < cfg["minLiquidityUsd"]:
        reason = "LIQ_LOW"
        log(f"{pos['name']} 流动性只剩${liq_now:,.0f}(低于${cfg['minLiquidityUsd']:,.0f}门槛),不管止盈止损,立刻卖出")

    ret = fresh_price / pos["entry_price_usd"] - 1
    if reason is None:
        # 报价源(GeckoTerminal)偶尔会给出离谱的坏数据(实测出现过池子price_usd差了240万倍的情况，
        # 跟之前screener那边碰到的"流动性显示14亿美元"是同一类报价异常)。这么夸张的比例基本可以
        # 确定是数据错误而不是真实行情，不能拿它去触发止盈止损决策——先跳过这一轮，等下一轮报价
        # 恢复正常再判断，比"按错误数据强行卖出"安全。(这条哨兵只挡止盈止损判断,不挡上面的
        # 流动性紧急卖出,因为流动性告急不看价格比例,该跑就跑)
        if abs(ret) > 50:
            log(f"SKIP EXIT CHECK {pos['name']}: fresh_price={fresh_price:.10g}相对入场价异常(ret={ret*100:.0f}%),"
               f"疑似报价数据错误,这轮不判断止盈止损")
            return
        hold_min = (NOW - pos["t_entry"]) / 60
        if ret >= cfg["tp"]:
            reason = "TP"
        elif ret <= cfg["sl"]:
            reason = "SL"
        elif hold_min >= cfg["maxHoldMin"]:
            reason = "TIME"
    if not reason:
        return

    record = {"action": "SELL", "name": pos["name"], "addr": addr, "reason": reason,
             "fresh_price_usd": fresh_price, "ret": ret, "live_mode": is_live_mode()}

    if not is_live_mode():
        log(f"[DRY-RUN] 本来会 SELL {pos['name']} [{reason}] ret={ret*100:+.1f}%")
        audit({**record, "status": "dry_run"})
        return

    quote = jupiter_quote(pos["mint"], SOL_MINT, pos["qty_raw"], cfg["slippageBps"])
    if not quote:
        log(f"FAIL SELL {pos['name']}: Jupiter拿不到报价")
        audit({**record, "status": "quote_failed"})
        return
    swap_tx = jupiter_swap_tx(quote, str(wallet.pubkey()))
    if not swap_tx:
        log(f"FAIL SELL {pos['name']}: 构造交易失败")
        audit({**record, "status": "build_failed"})
        return
    sig, err = sign_and_send(cfg, wallet, swap_tx)
    if err:
        log(f"FAIL SELL {pos['name']}: 广播失败 {err}")
        audit({**record, "status": "send_failed", "error": str(err)})
        return
    ok = confirm_tx(cfg, sig)
    if not ok:
        log(f"FAIL SELL {pos['name']}: 未确认 sig={sig}")
        audit({**record, "status": "unconfirmed", "sig": sig})
        return

    # 用Jupiter这笔卖出报价的实际outAmount(能拿到多少SOL)乘实时SOL价格算真实到手金额，
    # 不再用fresh_price的比例去估算——那个比例来自第三方报价接口，偶尔会离谱出错(见上面
    # ret>50的哨兵注释)，而Jupiter报价对应的是这笔交易实际会成交的数量，更可靠。
    sol_out = int(quote["outAmount"]) / 1e9
    sol_price_now = get_fresh_price_usd(SOL_MINT, is_pool_addr=False)
    if sol_price_now:
        proceeds_usd = sol_out * sol_price_now
    else:
        proceeds_usd = pos["usd"] * (1 + ret)  # 兜底:SOL价格也拿不到时才退回近似值
    pnl = proceeds_usd - pos["usd"]
    state["realized_pnl_usd"] += pnl
    state["closed"].append({**pos, "addr": addr, "exit_price_usd": fresh_price, "reason": reason,
                            "pnl_usd": round(pnl, 4), "sell_sig": sig, "t_exit": NOW})
    del state["positions"][addr]
    log(f"SELL {pos['name']} [{reason}] sig={sig} ret={ret*100:+.1f}% pnl=${pnl:+.4f}")
    audit({**record, "status": "filled", "sig": sig, "pnl_usd": round(pnl, 4)})


def write_dashboard(cfg, state):
    closed = state["closed"]
    wins = [c for c in closed if c["pnl_usd"] > 0]
    lines = [
        "# 策略D 实盘 (真实钱包) —— 本地专用,不会推送到任何公开地方",
        "",
        f"更新: {NOW_STR}  |  模式: {cfg['sizingMode']} "
        f"({'固定$%.2f' % cfg['posSizeUsd'] if cfg['sizingMode']=='fixed' else '机器人单笔的%.0f%%(区间$%.0f-$%.0f)' % (cfg['pctOfBot']*100, cfg['minPosUsd'], cfg['maxPosUsd'])})",
        "",
        f"持仓 {len(state['positions'])} | 已平仓 {len(closed)} | "
        f"胜率 {len(wins)/len(closed)*100 if closed else 0:.0f}% | "
        f"今日花费 ${state['spent_today_usd']:.2f} | 今日已实现盈亏 ${state['realized_pnl_usd']:+.2f}",
        "",
        "## 当前持仓",
        "| 币种 | 仓位$ | 买入价 | 开仓时间(UTC) |",
        "|---|---|---|---|",
    ]
    for addr, p in state["positions"].items():
        t = dt.datetime.fromtimestamp(p["t_entry"], dt.timezone.utc).strftime("%H:%M:%S")
        lines.append(f"| {p['name']} | ${p['usd']:.2f} | {p['entry_price_usd']:.10g} | {t} |")
    lines += ["", "## 最近平仓 (20)", "| 币种 | 原因 | 仓位$ | 盈亏$ | 卖出sig |", "|---|---|---|---|---|"]
    for c in closed[-20:][::-1]:
        lines.append(f"| {c['name']} | {c['reason']} | ${c['usd']:.2f} | {c['pnl_usd']:+.4f} | "
                     f"{c.get('sell_sig','')[:12]}... |")
    DASHBOARD_F.write_text("\n".join(lines), encoding="utf-8")


def _esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_dashboard_html(cfg, state):
    """跟纸盘的docs/index.html同一套视觉样式,但这份是服务端直接把数据渲染进HTML里的
    静态文件(不用fetch/不用起本地服务器),双击用浏览器打开就能看,不联网也行。
    只写在VPS本地磁盘,从不提交进git/推到任何公开仓库——真实钱包的持仓和盈亏不能公开。"""
    closed = state["closed"]
    wins = [c for c in closed if c["pnl_usd"] > 0]
    win_rate = (len(wins) / len(closed) * 100) if closed else 0
    mode_label = (f"固定 ${cfg['posSizeUsd']:.2f}" if cfg["sizingMode"] == "fixed"
                 else f"机器人单笔的{cfg['pctOfBot']*100:.0f}%(${cfg['minPosUsd']:.0f}-${cfg['maxPosUsd']:.0f})")

    pos_rows = ""
    for addr, p in state["positions"].items():
        t = dt.datetime.fromtimestamp(p["t_entry"], dt.timezone.utc).strftime("%H:%M:%S")
        pos_rows += (f"<tr><td>{_esc(p['name'])}</td><td>${p['usd']:.2f}</td>"
                    f"<td>{p['entry_price_usd']:.10g}</td><td>{t} UTC</td></tr>")
    if not pos_rows:
        pos_rows = '<tr><td colspan="4" class="empty">当前无持仓</td></tr>'

    closed_rows = ""
    for c in closed[-20:][::-1]:
        cls = "pos" if c["pnl_usd"] > 0 else ("neg" if c["pnl_usd"] < 0 else "zero")
        note = c.get("_corrected_note", "")
        badge = ' <span class="badge neg" title="报价源坏数据,已用链上真实成交金额人工修正">已修正</span>' if note else ""
        closed_rows += (f"<tr><td>{_esc(c['name'])}{badge}</td><td>{c['reason']}</td>"
                        f"<td>${c['usd']:.2f}</td><td><span class=\"badge {cls}\">"
                        f"{c['pnl_usd']:+.4f}</span></td>"
                        f"<td>{_esc(c.get('sell_sig',''))[:10]}...</td></tr>")
    if not closed_rows:
        closed_rows = '<tr><td colspan="5" class="empty">尚无平仓记录</td></tr>'

    log_lines = []
    if LOG_F.exists():
        log_lines = LOG_F.read_text(encoding="utf-8", errors="replace").splitlines()[-10:][::-1]
    log_html = "".join(f"<div>{_esc(l)}</div>" for l in log_lines) or '<div class="empty">暂无日志</div>'

    pnl_cls = "pos" if state["realized_pnl_usd"] > 0 else ("neg" if state["realized_pnl_usd"] < 0 else "zero")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="20">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>实盘监控 (VPS本地专用)</title>
<style>
  :root {{
    --bg: #0b0e14; --panel: #131722; --panel2: #1a1f2e; --border: #2a3040;
    --text: #e6e9ef; --dim: #8b93a7; --green: #26a69a; --red: #ef5350; --yellow: #d4a72c;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    font-size: 14px; line-height: 1.5; }}
  header {{ padding: 16px 20px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 10px; }}
  header h1 {{ font-size: 18px; margin: 0 0 4px; font-weight: 600; }}
  header .warn {{ font-size: 12px; color: var(--yellow); }}
  #heartbeat {{ font-size: 13px; font-weight: 600; padding: 6px 12px; border-radius: 8px; white-space: nowrap; }}
  #heartbeat .dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }}
  #heartbeat .sub2 {{ display: block; font-size: 11px; font-weight: 400; color: var(--dim); margin-top: 2px; }}
  main {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 14px; padding: 16px; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}
  .card.wide {{ grid-column: 1 / -1; }}
  .card h2 {{ font-size: 15px; margin: 0 0 4px; }}
  .sub {{ font-size: 12px; color: var(--dim); margin-bottom: 10px; }}
  .stats {{ display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 10px; font-size: 12px; color: var(--dim); }}
  .stats b {{ color: var(--text); font-weight: 600; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 6px; }}
  th, td {{ text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--border);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 160px; }}
  th {{ color: var(--dim); font-weight: 500; }}
  .badge {{ font-size: 10px; padding: 1px 6px; border-radius: 4px; background: var(--panel2); color: var(--dim); }}
  .pos {{ color: var(--green); background: rgba(38,166,154,0.15); }}
  .neg {{ color: var(--red); background: rgba(239,83,80,0.15); }}
  .zero {{ color: var(--dim); background: rgba(139,147,167,0.12); }}
  .empty {{ color: var(--dim); font-style: italic; padding: 6px 0; }}
  .log-feed {{ font-family: "SF Mono", Consolas, monospace; font-size: 11px; color: var(--dim);
    max-height: 200px; overflow-y: auto; margin-top: 8px; background: var(--panel2);
    border-radius: 6px; padding: 8px; }}
  .table-wrap {{ overflow-x: auto; }}
  footer {{ text-align: center; padding: 20px; color: var(--dim); font-size: 12px; }}
</style>
</head>
<body>
<header>
  <div>
    <h1>🔒 策略D 实盘监控 (真实钱包)</h1>
    <div class="warn">本地专用文件,只在这台VPS上查看,不会上传/推送到任何公开的地方</div>
  </div>
  <div id="heartbeat">加载中...</div>
</header>
<main>
  <div class="card">
    <h2>今日概况</h2>
    <div class="sub">更新: {NOW_STR} · 仓位模式: {_esc(mode_label)}</div>
    <div class="stats">
      <span>持仓 <b>{len(state['positions'])}</b></span>
      <span>已平仓 <b>{len(closed)}</b></span>
      <span>胜率 <b>{win_rate:.0f}%</b></span>
      <span>今日花费 <b>${state['spent_today_usd']:.2f}</b></span>
      <span>今日已实现盈亏 <span class="badge {pnl_cls}">${state['realized_pnl_usd']:+.2f}</span></span>
    </div>
  </div>
  <div class="card wide">
    <h2>当前持仓</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>币种</th><th>仓位$</th><th>买入价</th><th>开仓时间</th></tr></thead>
      <tbody>{pos_rows}</tbody>
    </table></div>
  </div>
  <div class="card wide">
    <h2>最近平仓 (20)</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>币种</th><th>原因</th><th>仓位$</th><th>盈亏$</th><th>卖出sig</th></tr></thead>
      <tbody>{closed_rows}</tbody>
    </table></div>
  </div>
  <div class="card wide">
    <h2>最近日志</h2>
    <div class="log-feed">{log_html}</div>
  </div>
</main>
<footer>每20秒自动刷新此页面(需要保持浏览器标签页开着) · 数据每次run_live_vps.ps1跑完后更新</footer>
<script>
// 计划任务是每3分钟跑一次run_live_vps.ps1,这里心跳按这个周期算阈值:
// 4分钟内=正常(可能还没到下一轮),4-10分钟=偏慢(该有点警觉了),10分钟以上=大概率任务
// 停了/卡住了/VPS本身有问题——这样不用等页面20秒刷新也能一眼看出程序是不是还活着。
const GENERATED_AT = {NOW};
const HB_COLORS = {{
  green: {{ fg: "#26a69a", bg: "rgba(38,166,154,0.15)" }},
  yellow: {{ fg: "#d4a72c", bg: "rgba(212,167,44,0.15)" }},
  red: {{ fg: "#ef5350", bg: "rgba(239,83,80,0.15)" }},
}};
function tickHeartbeat() {{
  const ageSec = Math.floor(Date.now() / 1000 - GENERATED_AT);
  const el = document.getElementById("heartbeat");
  let key, label;
  if (ageSec < 240) {{ key = "green"; label = "正常运行"; }}
  else if (ageSec < 600) {{ key = "yellow"; label = "偏慢,留意一下"; }}
  else {{ key = "red"; label = "可能停了!去VPS上检查计划任务/日志"; }}
  const c = HB_COLORS[key];
  const mins = Math.floor(ageSec / 60), secs = ageSec % 60;
  const ageStr = ageSec < 60 ? `${{ageSec}}秒前` : `${{mins}}分${{secs}}秒前`;
  el.style.background = c.bg;
  el.style.color = c.fg;
  el.style.border = `1px solid ${{c.fg}}`;
  el.innerHTML = `<span class="dot" style="background:${{c.fg}}"></span>${{label}}` +
    `<span class="sub2">页面生成于 ${{ageStr}}(计划任务约每3分钟跑一轮)</span>`;
}}
tickHeartbeat();
setInterval(tickHeartbeat, 1000);
</script>
</body>
</html>
"""
    DASHBOARD_HTML_F.write_text(html, encoding="utf-8")


def main():
    cfg = load_config()
    state = load_state()
    if state.get("day") != NOW_STR[:10]:
        state["spent_today_usd"] = 0.0
        state["realized_pnl_usd"] = 0.0
        state["day"] = NOW_STR[:10]

    if not is_live_mode():
        log("=== DRY-RUN 模式 (设置 LIVE_TRADING=1 且 CONFIRM_LIVE_BOTSCALP=YES 才会真实下单) ===")

    wallet = get_wallet() if is_live_mode() else None

    cands = load_bot_candidates()
    n_entered = 0
    for c in cands:
        try:
            if try_enter(cfg, wallet, state, c):
                n_entered += 1
        except Exception as e:
            # 无人值守跑的脚本,一个候选处理出异常不能把整轮(尤其是下面的持仓止盈止损监控)搭进去
            log(f"ERROR try_enter({c.get('name')}): {e}")
        save_state(state)  # 每处理一个就落盘一次,防止中途崩溃丢失已经真实发生的交易记录

    # 高频复查持仓阶段: 流动性风险(可能100%全亏)远大于3%止损,买候选慢(要查很多池子的
    # 尽调),但盯着手上2-3个持仓的流动性很快,不用等下一次计划任务触发(最快1分钟)才
    # 复查一次——在这次脚本运行内部持续高频复查,直到跑满窗口时间或者已经没持仓了。
    window_end = time.time() + cfg["exitCheckWindowSec"]
    n_exit_rounds = 0
    while state["positions"] and time.time() < window_end:
        n_exit_rounds += 1
        for addr in list(state["positions"].keys()):
            try:
                try_exit(cfg, wallet, state, addr, state["positions"][addr])
            except Exception as e:
                log(f"ERROR try_exit({addr}): {e}")
            save_state(state)
        if state["positions"] and time.time() < window_end:
            time.sleep(cfg["exitCheckIntervalSec"])

    write_dashboard(cfg, state)
    write_dashboard_html(cfg, state)
    log(f"CYCLE OK live_mode={is_live_mode()} 扫描候选{len(cands)} 新入场{n_entered} "
       f"持仓{len(state['positions'])} 高频复查{n_exit_rounds}轮 今日花费${state['spent_today_usd']:.2f} "
       f"今日已实现盈亏${state['realized_pnl_usd']:+.2f}")


if __name__ == "__main__":
    main()
