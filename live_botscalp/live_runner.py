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
    line = f"[{NOW_STR}] {msg}"
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
    try:
        r = requests.get(url, headers={"Accept": "application/json;version=20230302"}, timeout=15)
        if r.status_code != 200:
            return None
        return float(r.json()["data"]["attributes"][field])
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return None


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
            if c.get("scalping_flag") and c["addr"] not in seen:
                seen.add(c["addr"])
                cands.append(c)
    return cands


def try_enter(cfg, wallet, state, c):
    if c["addr"] in state["positions"] or len(state["positions"]) >= cfg["maxPositions"]:
        return False
    if state["realized_pnl_usd"] <= cfg["dailyLossKillUsd"]:
        log(f"SKIP {c['name']}: 今日已实现亏损${state['realized_pnl_usd']:.2f}已触发熔断,停止开新仓")
        return False

    pos_size_usd = decide_pos_size_usd(cfg, c["addr"])
    if pos_size_usd is None:
        log(f"SKIP {c['name']}: pct_of_bot模式下现在查不到机器人在刷(市场状况变了),不用旧数据凑仓位")
        return False
    if state["spent_today_usd"] + pos_size_usd > cfg["dailyMaxUsd"]:
        log(f"SKIP {c['name']}: 今日累计开仓将超过上限${cfg['dailyMaxUsd']}")
        return False

    fresh_price = get_fresh_price_usd(c["addr"], is_pool_addr=True)
    if fresh_price is None:
        log(f"SKIP {c['name']}: 拿不到实时报价")
        return False

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
    }
    state["spent_today_usd"] += pos_size_usd
    log(f"BUY {c['name']} ({c['addr'][:8]}...) 成交 sig={sig} 价格={fresh_price:.10g} 仓位=${pos_size_usd:.2f}")
    audit({**record, "status": "filled", "sig": sig})
    return True


def try_exit(cfg, wallet, state, addr, pos):
    fresh_price = get_fresh_price_usd(addr, is_pool_addr=True)
    if fresh_price is None:
        return
    ret = fresh_price / pos["entry_price_usd"] - 1
    # 报价源(GeckoTerminal)偶尔会给出离谱的坏数据(实测出现过池子price_usd差了240万倍的情况，
    # 跟之前screener那边碰到的"流动性显示14亿美元"是同一类报价异常)。这么夸张的比例基本可以
    # 确定是数据错误而不是真实行情，不能拿它去触发止盈止损决策——先跳过这一轮，等下一轮报价
    # 恢复正常再判断，比"按错误数据强行卖出"安全。
    if abs(ret) > 50:
        log(f"SKIP EXIT CHECK {pos['name']}: fresh_price={fresh_price:.10g}相对入场价异常(ret={ret*100:.0f}%),"
           f"疑似报价数据错误,这轮不判断止盈止损")
        return
    hold_min = (NOW - pos["t_entry"]) / 60
    reason = None
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

    for addr in list(state["positions"].keys()):
        try:
            try_exit(cfg, wallet, state, addr, state["positions"][addr])
        except Exception as e:
            log(f"ERROR try_exit({addr}): {e}")
        save_state(state)
    log(f"CYCLE OK live_mode={is_live_mode()} 扫描候选{len(cands)} 新入场{n_entered} "
       f"持仓{len(state['positions'])} 今日花费${state['spent_today_usd']:.2f} "
       f"今日已实现盈亏${state['realized_pnl_usd']:+.2f}")


if __name__ == "__main__":
    main()
