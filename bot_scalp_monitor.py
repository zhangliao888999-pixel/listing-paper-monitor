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
DEAD_PRICE_STREAK = 15       # 兜底:连续15轮(约45分钟)彻底拿不到任何数据(网络/接口问题),判定池子已死
DEAD_LIQ_USD = 100.0         # 更直接的判死信号:同一次查价请求里顺便看流动性,趋近于0直接判死,
                             # 不用等上面那个45分钟的间接推断(用户指出Phantom一查价格没了就知道
                             # 死了,GeckoTerminal的池子接口本来就带流动性字段,没必要绕远路)
SLIP = 0.02                  # 链上滑点保守估计，跟策略C一致

# 2026-07-27新增: 用户拿SPY/SOL这个真实案例点出的"拉高出货"风险,跟live_runner.py同一套逻辑
MIN_LOCKED_LIQ_PCT = 25.0    # 流动性锁仓比例低于这个数,LP随时能被抽干,不进场
                             # (2026-07-28晚从50%降到25%: 50%把候选筛到接近0个,同一批币
                             # 反复卡在0%被拦、新候选进不来,先降到25%看效果,不是撤掉这道防线)
MAX_BUY_SELL_RATIO = 30.0    # 过去1小时买家人数/卖家人数超过这个比例,像"广撒网吸引接盘"
MAX_RECENT_PUMP_PCT = 100.0  # 过去1小时已经涨了这么多,大概率追高接盘

# 2026-07-27新增: 用户明确指出"流动性比3%止损重要得多——流动性没了是100%全亏,
# 3%止损根本不算什么"。买候选慢(一轮要查很多候选),但盯着手上几个持仓的流动性很快,
# 不用等下一次计划任务触发(最快1分钟)才复查一次——脚本内部在扫描完新候选后,进入
# 一段高频复查窗口,持续盯着已有持仓直到没仓位或者到时间。
EXIT_CHECK_INTERVAL_SEC = 15
EXIT_CHECK_WINDOW_SEC = 150

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


def get_pool_diligence(addr):
    """2026-07-27新增: 只在进场前调用一次。用户拿SPY/SOL这个真实池子举例点出的风险:
    池子创建2-3小时内涨335%,过去1小时3011个钱包在买、只有38个在卖(典型"广撒网吸引
    散户接盘,少数人偷偷出货"信号)，最要命的是locked_liquidity_percentage(流动性锁仓
    比例)几乎是0——流动性提供方随时能把池子一次性抽干,这跟"流动性慢慢枯竭"是完全
    不同的风险,是主动rug机制。这几个字段本来就在同一个pools接口响应里,跟查价格/
    流动性是同一次请求,不用额外调用。"""
    d = get(S, f"{GT_BASE}/networks/solana/pools/{addr}")
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
        "locked_liq_pct": _f(a.get("locked_liquidity_percentage")),
        "buyers_h1": h1.get("buyers"),
        "sellers_h1": h1.get("sellers"),
        "price_change_h1_pct": _f((a.get("price_change_percentage") or {}).get("h1")),
    }


def try_enter(state, c):
    addr = c["addr"]
    if addr in state["positions"] or len(state["positions"]) >= MAX_POS:
        return False
    liq = c.get("liq")
    if liq is not None and liq < MIN_LIQUIDITY_USD:
        log(f"SKIP {c['name']}: 流动性只有${liq:,.0f},低于${MIN_LIQUIDITY_USD:,.0f}门槛,进得去也可能出不来")
        return False
    # 2026-07-27曾经改成三条全部只记录不拦截(发现锁仓比例一上来就100%拦截所有候选)，
    # 但2026-07-28锁仓比例这条改回硬拦截了——用户当晚亲身踩坑印证:锁仓比例低意味着
    # 主力随时能一笔交易瞬间抽干流动性,这个动作在链上一个区块内就完成,不管止盈止损
    # 检查多快都来不及反应,是物理时间差不是代码速度问题,唯一有效的办法是压根不进场。
    # 买卖比例/涨幅这两条继续只记录观察(那两个是"过程类"信号,反应速度还有意义)。
    dil = get_pool_diligence(addr)
    buy_sell_ratio, buyers, sellers = None, None, None
    if dil:
        buyers, sellers = dil["buyers_h1"], dil["sellers_h1"]
        if buyers and sellers is not None:
            buy_sell_ratio = float("inf") if (sellers == 0 and buyers > 20) else buyers / max(sellers, 1)
        # 2026-07-28修复: 原来"查不到锁仓数据就放行"是个漏洞——bulltom这笔真实持仓就是
        # 靠"None"混过去的,不是真的验证过安全。查不到=当作没锁仓处理,宁可错过不能选错。
        if dil["locked_liq_pct"] is None or dil["locked_liq_pct"] < MIN_LOCKED_LIQ_PCT:
            pct_str = f"{dil['locked_liq_pct']:.1f}%" if dil["locked_liq_pct"] is not None else "查不到(当作0%处理)"
            log(f"SKIP {c['name']}: 流动性锁仓比例{pct_str},主力随时能一笔交易瞬间抽干,不进场")
            return False
        if buy_sell_ratio is not None and buy_sell_ratio > MAX_BUY_SELL_RATIO:
            log(f"NOTE(未拦截) {c['name']}: 过去1小时买家{buyers}人/卖家{sellers}人,比例{buy_sell_ratio:.0f}:1")
        # 2026-07-28改回硬拦截: 入场时机是这个策略最粗糙的一环(检测到信号就立刻按当前价
        # 买,不管是不是已经涨过头)。这条数据已经在查了,只是没拦——如果检测到信号时过去
        # 1小时已经涨了100%+,大概率是追高接盘在给别人接盘,应该直接跳过。
        if dil["price_change_h1_pct"] is not None and dil["price_change_h1_pct"] > MAX_RECENT_PUMP_PCT:
            log(f"SKIP {c['name']}: 过去1小时已经涨了{dil['price_change_h1_pct']:.0f}%,大概率追高接盘,不进场")
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
        # 入场时的尽调快照,只记录不拦截,明天回看这些值分布跟实际盈亏的关系再定门槛。
        # ratio存None统一代表"数据缺失或买家人数远超卖家(sellers=0)导致比例无穷大",
        # 靠entry_buyers_h1/entry_sellers_h1两个原始人数字段区分是哪种情况
        "entry_locked_liq_pct": dil["locked_liq_pct"] if dil else None,
        "entry_buyers_h1": buyers, "entry_sellers_h1": sellers,
        "entry_buy_sell_ratio_h1": None if buy_sell_ratio == float("inf") else buy_sell_ratio,
        "entry_price_change_h1_pct": dil["price_change_h1_pct"] if dil else None,
    }
    log(f"BUY {c['name']} ({addr[:8]}...) @ {entry:.10g} 仓位=${pos_usd:.2f}(机器人单笔${avg_bot_usd:.0f}的{POS_SIZE_PCT_OF_BOT*100:.0f}%)")
    return True


def get_price_and_liq(addr):
    """一次请求同时拿价格和流动性——流动性字段(reserve_in_usd)本来就在同一个响应里,
    不用另外调用,也不用靠"连续拿不到数据"这种间接推断去判断池子是否已死。"""
    d = get(S, f"{GT_BASE}/networks/solana/pools/{addr}")
    if not d:
        return None, None
    attrs = d.get("data", {}).get("attributes", {})
    try:
        price = float(attrs["base_token_price_usd"])
    except (KeyError, ValueError, TypeError):
        price = None
    try:
        liq = float(attrs.get("reserve_in_usd"))
    except (TypeError, ValueError):
        liq = None
    return price, liq


def manage_positions(state):
    for addr in list(state["positions"].keys()):
        pos = state["positions"][addr]
        pos["total_checks"] = pos.get("total_checks", 0) + 1
        cur, liq_now = get_price_and_liq(addr)
        if liq_now is not None and liq_now < DEAD_LIQ_USD:
            state["realized_pnl"] += -pos["usd"]
            state["closed"].append({**pos, "addr": addr, "exit": 0.0, "reason": "DEAD",
                                    "pnl": round(-pos["usd"], 4), "t_exit": NOW,
                                    "_note": f"流动性只剩${liq_now:.4f},判定死亡,不用等连续多轮"})
            del state["positions"][addr]
            log(f"EXIT {pos['name']} [DEAD] 流动性只剩${liq_now:.4f}(低于${DEAD_LIQ_USD:.0f}门槛),"
               f"判定池子已死,按本金全亏${pos['usd']:.2f}平仓")
            continue
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
        # 2026-07-27新增(用户明确要求): 流动性快要不足,不管别的条件,必须马上卖出——
        # 不等跌到DEAD_LIQ_USD(=$100)那种彻底归零才处理,而是一旦低于MIN_LIQUIDITY_USD
        # (入场门槛同一个数,$15000)这个"还有得救"的区间就立刻按当前价卖出,不用等
        # 正常的止盈止损/超时判断——流动性告急是独立信号,不依赖(可能出错的)价格比例，
        # 该跑就必须跑,等真的跌到DEAD级别再处理就晚了(CXMT/Grok/breadcat都是这样)。
        if liq_now is not None and liq_now < MIN_LIQUIDITY_USD:
            exit_px = cur * (1 - SLIP)
            proceeds = pos["qty"] * exit_px
            pnl = proceeds - pos["usd"]
            state["cash"] += proceeds
            state["realized_pnl"] += pnl
            state["closed"].append({**pos, "addr": addr, "exit": exit_px, "reason": "LIQ_LOW",
                                    "pnl": round(pnl, 4), "t_exit": NOW})
            del state["positions"][addr]
            log(f"EXIT {pos['name']} [LIQ_LOW] 流动性只剩${liq_now:,.0f}(低于${MIN_LIQUIDITY_USD:,.0f}门槛),"
               f"不等止盈止损,立刻卖出 pnl={pnl:+.4f}U")
            time.sleep(0.3)
            continue
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

    # 高频复查持仓阶段: 流动性风险(可能100%全亏)远大于3%止损,盯着手上几个持仓的
    # 流动性很快,不用等下一次计划任务触发才复查——脚本内部持续高频复查,直到跑满
    # 窗口时间或者已经没持仓了。
    window_end = time.time() + EXIT_CHECK_WINDOW_SEC
    n_exit_rounds = 0
    while state["positions"] and time.time() < window_end:
        n_exit_rounds += 1
        manage_positions(state)
        if state["positions"] and time.time() < window_end:
            time.sleep(EXIT_CHECK_INTERVAL_SEC)

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
    log(f"CYCLE OK nav={nav:.4f} 扫描候选{len(cands)} 新入场{n_entered} 高频复查{n_exit_rounds}轮 "
       f"持仓{len(state['positions'])} 已平仓{len(closed)}")


if __name__ == "__main__":
    main()
