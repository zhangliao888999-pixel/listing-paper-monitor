# -*- coding: utf-8 -*-
"""策略 C 模拟盘：模仿"徐师傅"访谈中成功者的操作思路 —— Solana 新币动量捕捉

用户明确要求：不做历史回测，直接实时模拟观察，让未来数据说话。

原始访谈提炼的操作要点（尽量忠实还原，非我方优化）：
  监控    : Telegram式消息(此处用GeckoTerminal新池子流替代) + 筛选(市值倍数/流动性)
  标的池  : 只玩"能撑几个小时"的币 —— 太快死的(几分钟)和太长的(能玩一周)都不要
  入场    : 不买绝对起点，等它"已经慢慢在起了"再追进去(动量确认)
            或: 跌下来之后"一般几分钟不动"的企稳形态
  仓位    : v9起改为100U/单、最多30单同时持有——广撒网、单次冲击小，摇骰子次数多
  出场    : 吃到"几十个点"(约30-50%)就走，只拿"几分钟到几小时"；不动就砍(小额止损=手续费)
            —— 注意这与我们CEX策略"让赢家跑"完全相反，他的风格是快进快出、见好就收
  风控    : 流动性门槛过滤明显死盘/貔貅(暂无法获取"开发者是否持币"字段，用流动性+成交量代理)

数据源: GeckoTerminal 公开API(免费，无需key)，Solana链新池子流+单池详情
已知数据缺口(如实标注): 无法获取链上持仓分布(开发者持仓%)，无法达到他描述的
  秒级反应速度(受限于轮询频率)，无历史回测验证 —— 这就是用户要求的"直接实测"部分

独立虚拟账本 10,000 USDT。计划任务每 15 分钟运行一次(链上节奏比CEX快得多)。
产出 DASHBOARD_PUMPFUN.md / nav_pumpfun.csv / pumpfun.log
"""
import json
import time
import datetime as dt
from pathlib import Path

import requests

HERE = Path(__file__).parent
STATE_F = HERE / "state_pumpfun.json"
NAV_F = HERE / "nav_pumpfun.csv"
DASH_F = HERE / "DASHBOARD_PUMPFUN.md"
LOG_F = HERE / "pumpfun.log"
RESEARCH_LOG_F = HERE / "research" / "tx_snapshots.jsonl"

CAPITAL = 10000.0
# v9(用户判断: 这是广撒网赌概率的游戏，回测测不出什么，只能极小仓位快进快出):
# 仓位减半、上限翻倍，摇骰子次数更多、单次冲击更小；止盈从+40%降到+20%，"涨了就跑"，
# 不死等可能到不了的目标价。
POS_SIZE = 100.0
MAX_POS = 30
MIN_AGE_MIN = 15          # 太新(几分钟内)的池子噪音/貔貅概率极高，先观察
MAX_AGE_HOURS = 8         # 超过这个窗口不再是他说的"能玩几小时"这个类别
MIN_LIQUIDITY_USD = 8000  # 流动性地板，过滤掉明显的死盘/貔貅雏形
TP, SL = 0.20, -0.20      # 涨了就跑，别死等到不了的目标；止损维持-20%容错
MAX_HOLD_MIN = 150        # 他拿几分钟到几小时；给轮询延迟留余量，2.5小时强制离场
SLIP = 0.02               # 链上滑点远高于CEX，AMM薄流动性下的保守假设

# 取样范围：实测 Solana 新池子真实生成速度约 24个/分钟，15分钟一轮的话真实会有~360个新池子。
# 15页new_pools(300个) + 3页trending(60个) 覆盖大部分窗口，同时把请求间隔拉长避开限速(约6次/次触发429)。
NEW_POOLS_PAGES = 15
TRENDING_PAGES = 3

GT_BASE = "https://api.geckoterminal.com/api/v2"
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                  "Accept": "application/json;version=20230302"})
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


def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            return None
        except (requests.RequestException, json.JSONDecodeError):
            time.sleep(1.5 * (i + 1))
    return None


def parse_pool(attrs):
    try:
        created = dt.datetime.fromisoformat(attrs["pool_created_at"].replace("Z", "+00:00"))
        age_min = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 60
        return {
            "price": float(attrs["base_token_price_usd"]),
            "liq": float(attrs.get("reserve_in_usd") or 0),
            "age_min": age_min,
            "chg_m5": float((attrs.get("price_change_percentage") or {}).get("m5") or 0),
            "chg_m15": float((attrs.get("price_change_percentage") or {}).get("m15") or 0),
            "chg_h1": float((attrs.get("price_change_percentage") or {}).get("h1") or 0),
            "vol_m5": float((attrs.get("volume_usd") or {}).get("m5") or 0),
            "vol_h1": float((attrs.get("volume_usd") or {}).get("h1") or 0),
            "tx_m5": (attrs.get("transactions") or {}).get("m5") or {},
            "tx_h1": (attrs.get("transactions") or {}).get("h1") or {},
            "liq_raw": attrs.get("reserve_in_usd"),
            "name": attrs.get("name", "?"),
        }
    except (KeyError, ValueError, TypeError):
        return None


def try_enter(state, addr, p, source):
    """用列表接口自带的字段直接判断入场，不再为每个观察中的池子单独发请求"""
    if p["age_min"] > MAX_AGE_HOURS * 60:
        w = state["watch"].get(addr)
        if w:
            w["status"] = "expired"
        return False
    if p["age_min"] < MIN_AGE_MIN or p["liq"] < MIN_LIQUIDITY_USD:
        return False
    # 入场信号(原文两种形态的简化编码):
    #  a) 动量确认: 短周期在涨,且有真实成交量支撑("已经慢慢在起了")
    #  b) 企稳反弹: 跌了一段后近5分钟确认转涨、且仍有真实成交量("跌到这然后不动/反弹")
    #     v8修正(用户实盘观察反馈): 原来"跌幅收窄到-2%以内"太松，会把"已经死透没人交易、
    #     价格趴着不动"的死币也当成企稳信号买入。改为要求最近5分钟真正转正 + 有成交量支撑，
    #     排除"只是不再下跌"但其实没有资金在推动的假企稳。
    momentum = p["chg_m15"] > 5 and p["chg_h1"] > 0 and p["vol_h1"] > MIN_LIQUIDITY_USD * 0.3
    stabilize = (p["chg_h1"] < -10 and p["chg_m5"] > 0.5
                and p["vol_m5"] > MIN_LIQUIDITY_USD * 0.01)
    if not (momentum or stabilize):
        return False
    if len(state["positions"]) >= MAX_POS or state["cash"] < POS_SIZE:
        w = state["watch"].get(addr)
        if w:
            w["status"] = "skipped_capacity"
        return False
    entry = p["price"] * (1 + SLIP)
    state["cash"] -= POS_SIZE
    state["positions"][addr] = {"name": p["name"], "entry": entry,
                                "qty": POS_SIZE / entry, "usd": POS_SIZE,
                                "t_entry": NOW, "signal": "momentum" if momentum else "stabilize"}
    if addr in state["watch"]:
        state["watch"][addr]["status"] = "entered"
    log(f"BUY {p['name']} ({addr[:8]}...) @ {entry:.10g}  [{'momentum' if momentum else 'stabilize'}/{source}] "
        f"liq=${p['liq']:,.0f} age={p['age_min']:.0f}min")
    return True


def log_snapshot(f, addr, p):
    """研究用：记录买卖笔数/买卖人数快照，供几天后做'拉升前是否有买压先兆'分析。
    GeckoTerminal不提供这个字段的历史，只能从现在起持续采样攒时间序列。"""
    f.write(json.dumps({
        "ts": NOW, "addr": addr, "name": p["name"], "age_min": round(p["age_min"], 1),
        "price": p["price"], "liq": p["liq"], "chg_m5": p["chg_m5"], "chg_h1": p["chg_h1"],
        "vol_m5": p["vol_m5"], "vol_h1": p["vol_h1"], "tx_m5": p["tx_m5"], "tx_h1": p["tx_h1"],
    }, ensure_ascii=False) + "\n")


def scan_new_pools(state):
    """扫最近新建的池子 + 热门池子列表：一次性拿到价格/涨跌幅/流动性等全部字段，
    既用来维护观察名单，也直接在同一次拉取里判断入场——不再对每个池子单独发请求，
    避免观察名单变大后请求量线性膨胀导致超时。同时给研究用途记一份买卖快照。"""
    found = entered = 0
    RESEARCH_LOG_F.parent.mkdir(exist_ok=True)
    with RESEARCH_LOG_F.open("a", encoding="utf-8") as rf:
        for kind, url, pages in [("new", f"{GT_BASE}/networks/solana/new_pools", NEW_POOLS_PAGES),
                                 ("trend", f"{GT_BASE}/networks/solana/trending_pools", TRENDING_PAGES)]:
            for page in range(1, pages + 1):
                d = get(url, {"page": page})
                rows = (d or {}).get("data") or []
                if not rows:
                    break
                for row in rows:
                    addr = row["id"].split("_")[-1]
                    p = parse_pool(row["attributes"])
                    if not p:
                        continue
                    log_snapshot(rf, addr, p)
                    if addr not in state["watch"] and addr not in state["positions"]:
                        state["watch"][addr] = {"addr": addr, "name": p["name"],
                                                "created_price": p["price"], "status": "watching",
                                                "first_seen": NOW}
                        found += 1
                    w = state["watch"].get(addr)
                    if w and w["status"] == "watching":
                        if try_enter(state, addr, p, kind):
                            entered += 1
                time.sleep(0.5)
    # 用我们自己记的 first_seen 做年龄淘汰，不需要额外请求
    for addr, w in state["watch"].items():
        if w["status"] == "watching" and (NOW - w["first_seen"]) > MAX_AGE_HOURS * 3600:
            w["status"] = "expired"
    if found or entered:
        log(f"scan: +{found} 新加入观察名单, {entered} 笔新入场")
    return entered


def manage_positions(state):
    for addr in list(state["positions"].keys()):
        pos = state["positions"][addr]
        d = get(f"{GT_BASE}/networks/solana/pools/{addr}")
        if not d:
            continue
        p = parse_pool(d["data"]["attributes"])
        if not p:
            continue
        cur = p["price"]
        ret = cur / pos["entry"] - 1
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
                                    "pnl": round(pnl, 2), "t_exit": NOW})
            del state["positions"][addr]
            log(f"EXIT {pos['name']} [{reason}] ret={ret*100:+.1f}% pnl={pnl:+.2f}U held={hold_min:.0f}min")
        time.sleep(0.25)


def main():
    if STATE_F.exists():
        state = json.loads(STATE_F.read_text(encoding="utf-8"))
    else:
        state = {"created": NOW_STR, "cash": CAPITAL, "positions": {}, "closed": [],
                 "watch": {}, "realized_pnl": 0.0}
        log(f"INIT pumpfun-style paper: {CAPITAL} USDT, {POS_SIZE}/单, TP+{TP*100:.0f}%/SL{SL*100:.0f}%/最长{MAX_HOLD_MIN}分钟")
        log("说明: 实时模拟、不做历史回测(用户明确要求)；无法获取开发者持仓%字段，用流动性/成交量代理防貔貅")

    n_new = scan_new_pools(state)
    manage_positions(state)

    # prune expired/old watch entries to keep state small
    state["watch"] = {k: w for k, w in state["watch"].items()
                      if w["status"] == "watching" or NOW - w["first_seen"] < 2 * 86400}

    # NAV
    open_val = sum(p["qty"] * p.get("mark", p["entry"]) for p in state["positions"].values())
    nav = state["cash"] + open_val
    hdr = not NAV_F.exists()
    watching = sum(1 for w in state["watch"].values() if w["status"] == "watching")
    with NAV_F.open("a", encoding="utf-8") as f:
        if hdr:
            f.write("ts,date,nav,cash,open_positions,watching,realized_pnl\n")
        f.write(f"{NOW},{NOW_STR[:10]},{nav:.2f},{state['cash']:.2f},{len(state['positions'])},{watching},{state['realized_pnl']:.2f}\n")

    # dashboard
    closed = state["closed"]
    wins = [c for c in closed if c["pnl"] > 0]
    lines = [
        "# 策略C：模仿访谈操盘手 —— Solana 新币动量 模拟盘",
        "",
        f"更新: {NOW_STR}  |  起始: {CAPITAL:,.0f} USDT ({state['created'][:10]})",
        "",
        f"## NAV: **{nav:,.2f} USDT**  ({(nav/CAPITAL-1)*100:+.2f}%)",
        "",
        "**方法论**: 不做历史回测，直接实时模拟观察(用户明确要求)。规则尽量忠实还原访谈中",
        "成功交易者的操作思路：只玩\"能撑几小时\"的币、等确认起势或企稳后入场、吃几十个点快进快出、",
        "不动就砍。**这与我们CEX九所策略(让赢家跑)风格相反**，两条曲线未来可以对照观察。",
        "",
        f"**已知数据缺口**：无开发者持仓%字段(用流动性/成交量代理防貔貅)；轮询频率(15分钟)",
        f"远达不到他描述的秒级反应速度；此策略引入了CEX层完全没有的**貔貅/蜜罐/清池风险**。",
        "",
        f"持仓 {len(state['positions'])} | 观察中 {watching} | 已平仓 {len(closed)} | "
        f"胜率 {len(wins)/len(closed)*100 if closed else 0:.0f}% | 已实现 {state['realized_pnl']:+.2f}U",
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
    lines += ["", "## 最近平仓 (20)", "| 币种 | 信号 | 原因 | 盈亏 |", "|---|---|---|---|"]
    for c in closed[-20:][::-1]:
        lines.append(f"| {c['name']} | {c.get('signal','?')} | {c['reason']} | {c['pnl']:+.2f}U |")
    DASH_F.write_text("\n".join(lines), encoding="utf-8")

    STATE_F.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"CYCLE OK nav={nav:.2f} new_entries={n_new} pos={len(state['positions'])} "
        f"watch={watching} closed={len(closed)}")


if __name__ == "__main__":
    main()
