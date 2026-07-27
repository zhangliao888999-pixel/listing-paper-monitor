# -*- coding: utf-8 -*-
"""策略D 模拟盘："薅机器人羊毛" —— 只买screener检测到高频刷量机器人的币，
跟着机器人的仓位规模小额进出，5%止盈/3%止损，快进快出。

用户的假设：机器人在一个价格区间反复对倒制造成交量，如果我们用远小于机器人
单笔规模的仓位(机器人单笔的10%)跟进/跟出，理论上机器人能接住我们抛出的仓位，
可以蹭到这个区间震荡里的小价差。

**我方对此持怀疑态度，但不代表结论**：2026-07-26的研究(research/bot_strategy_analysis.py)
测过这些机器人自己的对倒价差胜率只有48%(约等于抛硬币)、均值净亏$7(还没算手续费)——
机器人自己都吃不到这个价差的正期望，我们的执行速度更慢，理论上更难跟上。
但这只是推测，模拟盘跑出来的真实数据才算数，所以照常搭建、照常观察。

只做虚拟资金模拟，不做真实下单(这条线不会碰)。

数据来源：直接复用screener.py已经在跑的候选币扫描结果(screener_candidates.json
和screener_candidates_local.json，云端+本地合并，逻辑同看盘页面)，只挑其中
scalping_flag=True(check_scalping判定的高频刷量机器人)的币作为入场标的，
不重复造轮子扫描。
"""
import json
import time
import datetime as dt
from pathlib import Path

import requests

from check_coin import check_scalping, GT_BASE, S, get

HERE = Path(__file__).parent
STATE_F = HERE / "state_botscalp.json"
NAV_F = HERE / "nav_botscalp.csv"
DASH_F = HERE / "DASHBOARD_BOTSCALP.md"
LOG_F = HERE / "botscalp.log"

CAPITAL = 10000.0
MAX_POS = 20
POS_SIZE_PCT_OF_BOT = 0.10   # 用户指定：机器人单笔交易金额的10%
MIN_POS_USD = 5              # 地板:机器人单笔太小的话我们仓位会小到手续费都覆盖不了
MAX_POS_USD = 50             # 天花板:防止某个异常大单把我们的仓位也算得离谱大
TP, SL = 0.05, -0.03         # 用户指定：5%止盈/3%止损
MAX_HOLD_MIN = 30            # "快进快出"，止盈止损都没触发的话30分钟强制离场
DEAD_PRICE_STREAK = 15       # 连续15轮(每轮3分钟,约45分钟)拿不到报价,判定这个池子已经死了
SLIP = 0.02                  # 链上滑点保守估计，跟策略C一致

NOW = int(time.time())
NOW_STR = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def log(msg):
    line = f"[{NOW_STR}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        pass
    with LOG_F.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# 2026-07-27确认: GeckoTerminal对这个池子的报价持续异常(同一小时内先后读到
# 0.00006/0.32/0.045/0.12/0.16这种互相对不上的"价格")，实盘那边live_runner.py
# 的$1测试仓位因此吃了4笔假信号触发的亏损；纸盘这边同一个坑把cash炸到过1.3亿。
# 不是一次性坏点，是这个池子的数据源本身不可信，直接拉黑不再进场。
BLOCKED_POOL_ADDRS = {
    "FLUMAEUTHQ3X8xzAQdGA45BXS94yNjkmDZHx9WTR3fCA",  # CXMT / SOL
}

# 2026-07-27新增: 实盘那边breadcat/Tepe/Grok三笔连续闪崩+卖不掉之后回查,三个池子出事
# 后流动性都趴在接近0,但24小时成交量几十万到上千万美元不等——"看着活跃、兜不住"的
# 典型信号,买入前直接量深度比事后猜特征更直接。这里照搬实盘live_runner.py同一版逻辑：
# ①流动性地板值 ②模拟的价格冲击(纸盘没有真实Jupiter报价,用仓位占流动性的比例近似:
# 对constant-product型AMM,小额交易的价格冲击大约是"仓位/流动性"比例的2倍左右,这里
# 用2%的仓位占比近似对应实盘5%冲击门槛,不是精确复刻,只是同一个方向的粗略模拟)。
MIN_LIQUIDITY_USD = 15000.0
MAX_POS_LIQ_RATIO = 0.02


def load_bot_candidates():
    """复用screener的候选扫描结果(云端+本地合并),只要scalping_flag=True的"""
    cands, seen = [], set()
    for name in ("screener_candidates.json", "screener_candidates_local.json"):
        f = HERE / name
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for c in d.get("candidates", []):
            if c.get("scalping_flag") and c["addr"] not in seen and c["addr"] not in BLOCKED_POOL_ADDRS:
                seen.add(c["addr"])
                cands.append(c)
    return cands


def try_enter(state, c):
    addr = c["addr"]
    if addr in state["positions"] or len(state["positions"]) >= MAX_POS:
        return False
    liq = c.get("liq")
    if liq is not None and liq < MIN_LIQUIDITY_USD:
        log(f"SKIP {c['name']}: 流动性只有${liq:,.0f},低于${MIN_LIQUIDITY_USD:,.0f}门槛,进得去也可能出不来")
        return False
    result = check_scalping(addr)
    if not result.get("flag"):
        return False  # screener缓存可能有点旧,重新确认一遍还在刷量再进场
    avg_bot_usd = result.get("suspect_wallet_avg_trade_usd")
    if not avg_bot_usd:
        return False
    pos_usd = max(MIN_POS_USD, min(MAX_POS_USD, avg_bot_usd * POS_SIZE_PCT_OF_BOT))
    if liq and pos_usd / liq > MAX_POS_LIQ_RATIO:
        log(f"SKIP {c['name']}: 仓位${pos_usd:.0f}占流动性${liq:,.0f}的{pos_usd/liq*100:.1f}%,池子太薄")
        return False
    if state["cash"] < pos_usd:
        return False
    entry = c["price"] * (1 + SLIP)
    state["cash"] -= pos_usd
    state["positions"][addr] = {
        "name": c["name"], "entry": entry, "qty": pos_usd / entry, "usd": pos_usd,
        "t_entry": NOW, "bot_avg_trade_usd": avg_bot_usd,
        "no_price_checks": 0,  # 持仓期间"拿不到报价"发生了几次,落进closed记录方便事后统计
        "total_checks": 0,     # 持仓期间总共检查了几次(算比例用)
    }
    log(f"BUY {c['name']} ({addr[:8]}...) @ {entry:.10g} 仓位=${pos_usd:.2f}(机器人单笔${avg_bot_usd:.0f}的{POS_SIZE_PCT_OF_BOT*100:.0f}%)")
    return True


def get_price(addr):
    d = get(S, f"{GT_BASE}/networks/solana/pools/{addr}")
    if not d:
        return None
    try:
        return float(d["data"]["attributes"]["base_token_price_usd"])
    except (KeyError, ValueError, TypeError):
        return None


def manage_positions(state):
    for addr in list(state["positions"].keys()):
        pos = state["positions"][addr]
        pos["total_checks"] = pos.get("total_checks", 0) + 1
        cur = get_price(addr)
        if cur is None:
            pos["no_price_checks"] = pos.get("no_price_checks", 0) + 1
            pos["consecutive_no_price"] = pos.get("consecutive_no_price", 0) + 1
            # 2026-07-27修复: 这里原来是静默continue,一个持仓从买入到卖出全程拿不到
            # 报价的话,日志/记录里完全看不出发生过这件事——用户查breadcat/Grok/Tepe这几个
            # 闪崩+卖不掉的真实案例时发现的同一类盲区,live_runner.py那边也是同一个bug。
            log(f"SKIP EXIT CHECK {pos['name']}: 拿不到实时报价,这轮跳过止盈止损判断"
               f"(连续{pos['consecutive_no_price']}轮)")
            # 用户指出的更严重的漏洞: 止盈/止损/超时这三个判断全都要求先成功拿到价格才会
            # 执行,如果一个池子彻底死透、永远查不到报价,原来的代码会让这个仓位永远挂在
            # state["positions"]里,NAV计算时还按最后一次成功的价格(甚至买入价)继续算它的
            # "账面价值"——利润被这些实际上已经清零的死仓位悄悄撑高。连续太多轮拿不到报价,
            # 直接判死、强制平仓、按整笔本金全亏处理,不再让它悬空占着账面价值。
            if pos["consecutive_no_price"] >= DEAD_PRICE_STREAK:
                state["realized_pnl"] += -pos["usd"]
                state["closed"].append({**pos, "addr": addr, "exit": 0.0, "reason": "DEAD",
                                        "pnl": round(-pos["usd"], 4), "t_exit": NOW})
                del state["positions"][addr]
                log(f"EXIT {pos['name']} [DEAD] 连续{pos['consecutive_no_price']}轮拿不到报价,"
                   f"判定池子已死,按本金全亏${pos['usd']:.2f}平仓")
            time.sleep(0.3)
            continue
        pos["consecutive_no_price"] = 0
        ret = cur / pos["entry"] - 1
        # GeckoTerminal的报价偶尔会离谱出错(实测CXMT这个池子出现过价格差了几百万倍的情况，
        # 这份代码的live版本live_botscalp/live_runner.py也踩过同一个坑，是同一类报价异常，
        # 不是真实行情)。这么夸张的比例不能拿去当真触发止盈止损/算盈亏，跳过这一轮，
        # 不更新mark/mark_ret，等下一轮报价恢复正常再判断。
        if abs(ret) > 50:
            log(f"SKIP EXIT CHECK {pos['name']}: cur={cur:.10g}相对入场价异常(ret={ret*100:.0f}%),"
               f"疑似报价数据错误,这轮不判断止盈止损")
            time.sleep(0.3)
            continue
        hold_min = (NOW - pos["t_entry"]) / 60
        reason = None
        if ret >= TP:
            reason = "TP"
        elif ret <= SL:
            reason = "SL"
        elif hold_min >= MAX_HOLD_MIN:
            reason = "TIME"
        pos["mark"] = cur
        pos["mark_ret"] = ret
        if reason:
            exit_px = cur * (1 - SLIP)
            proceeds = pos["qty"] * exit_px
            pnl = proceeds - pos["usd"]
            state["cash"] += proceeds
            state["realized_pnl"] += pnl
            state["closed"].append({**pos, "addr": addr, "exit": exit_px, "reason": reason,
                                    "pnl": round(pnl, 4), "t_exit": NOW})
            del state["positions"][addr]
            log(f"EXIT {pos['name']} [{reason}] ret={ret*100:+.1f}% pnl={pnl:+.4f}U held={hold_min:.0f}min")
        time.sleep(0.3)


def main():
    if STATE_F.exists():
        state = json.loads(STATE_F.read_text(encoding="utf-8"))
    else:
        state = {"created": NOW_STR, "cash": CAPITAL, "positions": {}, "closed": [], "realized_pnl": 0.0}
        log(f"INIT 策略D(薅机器人羊毛): {CAPITAL} USDT, 仓位=机器人单笔{POS_SIZE_PCT_OF_BOT*100:.0f}%(${MIN_POS_USD}-${MAX_POS_USD}), "
           f"TP+{TP*100:.0f}%/SL{SL*100:.0f}%/最长{MAX_HOLD_MIN}分钟")
        log("说明: 用户假设可以跟着机器人的仓位规模蹭价差；我方持怀疑(机器人自己胜率约48%均值净亏，"
           "见research/bot_strategy_analysis.py)，但结论以这个模拟盘的真实数据为准，不预设立场")

    cands = load_bot_candidates()
    n_entered = 0
    for c in cands:
        if try_enter(state, c):
            n_entered += 1

    manage_positions(state)

    open_val = sum(p["qty"] * p.get("mark", p["entry"]) for p in state["positions"].values())
    nav = state["cash"] + open_val
    hdr = not NAV_F.exists()
    with NAV_F.open("a", encoding="utf-8") as f:
        if hdr:
            f.write("ts,date,nav,cash,open_positions,realized_pnl\n")
        f.write(f"{NOW},{NOW_STR[:10]},{nav:.4f},{state['cash']:.4f},{len(state['positions'])},{state['realized_pnl']:.4f}\n")

    closed = state["closed"]
    wins = [c for c in closed if c["pnl"] > 0]
    lines = [
        "# 策略D：薅机器人羊毛 —— 模拟盘",
        "",
        f"更新: {NOW_STR}  |  起始: {CAPITAL:,.0f} USDT ({state['created'][:10]})",
        "",
        f"## NAV: **{nav:,.4f} USDT**  ({(nav/CAPITAL-1)*100:+.2f}%)",
        "",
        "**用户假设**: 找到有高频刷量机器人的新币，用机器人单笔交易金额的10%跟进/跟出，",
        "5%止盈/3%止损快进快出，理论上机器人能接住我们抛出的小仓位。",
        "",
        "**我方顾虑(不预设结论)**: 机器人自己的对倒胜率实测只有48%、均值净亏$7(未计手续费)，",
        "机器人本身都吃不到这个价差的正期望；此策略最终是否有效，以下面的真实模拟数据为准。",
        "",
        f"持仓 {len(state['positions'])} | 已平仓 {len(closed)} | "
        f"胜率 {len(wins)/len(closed)*100 if closed else 0:.0f}% | 已实现 {state['realized_pnl']:+.4f}U",
        "",
        "## 当前持仓",
        "| 币种 | 入场 | 现价 | 浮盈 | 持有时长 |",
        "|---|---|---|---|---|",
    ]
    for addr, p in state["positions"].items():
        held = (NOW - p["t_entry"]) / 60
        mark = p.get("mark", p["entry"])
        lines.append(f"| {p['name']} | {p['entry']:.6g} | {mark:.6g} | "
                     f"{(mark/p['entry']-1)*100:+.1f}% | {held:.0f}分钟 |")
    lines += ["", "## 最近平仓 (20)", "| 币种 | 原因 | 盈亏 |", "|---|---|---|"]
    for c in closed[-20:][::-1]:
        lines.append(f"| {c['name']} | {c['reason']} | {c['pnl']:+.4f}U |")
    DASH_F.write_text("\n".join(lines), encoding="utf-8")

    STATE_F.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"CYCLE OK nav={nav:.4f} 扫描候选{len(cands)} 新入场{n_entered} 持仓{len(state['positions'])} 已平仓{len(closed)}")


if __name__ == "__main__":
    main()
