# -*- coding: utf-8 -*-
"""高频买新币模拟盘 (paper trading — long every new Gate USDT perp)

策略（471 合约回测选出的最优做多配置，用户知情选择部署）：
  - 入场: 每个非 pre-market 新永续，上线后 1-6 小时内以现价买入（+0.3% 滑点）
  - 仓位: 每单 500 USDT（5%），最多同时 10 单
  - 退出: TP +50% / SL -15% / 72 小时时间退出（用 1h K线按时间顺序判定先触哪个）
  - 卖出 -0.3% 滑点+费

回测预期（诚实展示在仪表盘上）：
  毛均值 +3.4%/单、中位 -0.5%、胜率 47% —— 收益全靠 ~10% 的止盈单，中位单是亏的。

每小时由计划任务 PaperNewCoinMonitor 运行一次。
"""
import json
import time
import datetime as dt
from pathlib import Path

import requests

HERE = Path(__file__).parent
STATE_F = HERE / "state_newcoin.json"
NAV_F = HERE / "nav_newcoin.csv"
DASH_F = HERE / "DASHBOARD_NEWCOIN.md"
LOG_F = HERE / "newcoin.log"

CAPITAL = 10000.0
POS_SIZE = 500.0
MAX_POS = 10
TP, SL, TIME_EXIT_H = 0.50, -0.15, 72
SLIP = 0.003
ENTRY_MIN_H, ENTRY_MAX_H = 1, 6

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
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


def get_json(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (400, 404):
                return None
        except (requests.RequestException, json.JSONDecodeError):
            pass
        time.sleep(2 * (i + 1))
    return None


def last_price(contract):
    d = get_json("https://api.gateio.ws/api/v4/futures/usdt/tickers", {"contract": contract})
    if isinstance(d, list) and d:
        return float(d[0]["last"])
    return None


def klines_1h(contract, t_from):
    d = get_json("https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
                 {"contract": contract, "interval": "1h", "from": int(t_from), "to": NOW})
    if not isinstance(d, list):
        return []
    return [(int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])) for r in d]


def main():
    if STATE_F.exists():
        state = json.loads(STATE_F.read_text(encoding="utf-8"))
    else:
        state = {"created": NOW_STR, "cash": CAPITAL, "positions": {}, "closed": [],
                 "seen_contracts": [], "realized_pnl": 0.0}
        log(f"INIT newcoin paper: capital={CAPITAL} USDT, {POS_SIZE}/trade, TP+{TP*100:.0f}%/SL{SL*100:.0f}%/72h")

    # 1) discover new launches
    contracts = get_json("https://api.gateio.ws/api/v4/futures/usdt/contracts") or []
    for c in contracts:
        lt = int(c.get("launch_time") or 0) or int(c.get("create_time") or 0)
        name = c["name"]
        if name in state["seen_contracts"] or name in state["positions"]:
            continue
        age_h = (NOW - lt) / 3600 if lt else 1e9
        if age_h > ENTRY_MAX_H:
            if lt and age_h < 240:
                state["seen_contracts"].append(name)  # too late, don't retry
            continue
        if age_h < ENTRY_MIN_H:
            continue  # wait for the 1h mark, next cycle picks it up
        if c.get("is_pre_market") or c.get("in_delisting"):
            state["seen_contracts"].append(name)
            continue
        if len(state["positions"]) >= MAX_POS or state["cash"] < POS_SIZE:
            log(f"SKIP {name}: at capacity")
            state["seen_contracts"].append(name)
            continue
        px = last_price(name)
        if not px:
            continue
        entry = px * (1 + SLIP)
        qty = POS_SIZE / entry
        state["cash"] -= POS_SIZE
        state["positions"][name] = {"entry": entry, "qty": qty, "t_entry": NOW,
                                    "launch": lt, "usd": POS_SIZE}
        state["seen_contracts"].append(name)
        log(f"BUY {name} @ {entry:.6g} ({POS_SIZE}U, launch age {age_h:.1f}h)")

    # 2) manage open positions (path-accurate exit via 1h klines)
    for name in list(state["positions"].keys()):
        p = state["positions"][name]
        exit_px, reason = None, None
        bars = klines_1h(name, p["t_entry"])
        tp_px, sl_px = p["entry"] * (1 + TP), p["entry"] * (1 + SL)
        for (t, o, h, l, cl) in bars:
            if l <= sl_px and h >= tp_px:
                exit_px, reason = sl_px, "SL(ambig)"  # both in one bar: assume worst
                break
            if l <= sl_px:
                exit_px, reason = sl_px, "SL"
                break
            if h >= tp_px:
                exit_px, reason = tp_px, "TP"
                break
        if exit_px is None and NOW - p["t_entry"] > TIME_EXIT_H * 3600:
            exit_px, reason = (bars[-1][4] if bars else last_price(name) or p["entry"]), "TIME"
        if exit_px:
            proceeds = p["qty"] * exit_px * (1 - SLIP)
            pnl = proceeds - p["usd"]
            state["cash"] += proceeds
            state["realized_pnl"] += pnl
            state["closed"].append({**p, "name": name, "exit": exit_px, "reason": reason,
                                    "pnl": round(pnl, 2), "t_exit": NOW})
            del state["positions"][name]
            log(f"EXIT {name} [{reason}] pnl {pnl:+.2f}U")

    # 3) NAV
    open_val = 0.0
    for name, p in state["positions"].items():
        px = last_price(name) or p["entry"]
        open_val += p["qty"] * px
        time.sleep(0.1)
    nav = state["cash"] + open_val
    hdr = not NAV_F.exists()
    with NAV_F.open("a", encoding="utf-8") as f:
        if hdr:
            f.write("ts,date,nav,cash,open_positions,realized_pnl\n")
        f.write(f"{NOW},{NOW_STR[:10]},{nav:.2f},{state['cash']:.2f},{len(state['positions'])},{state['realized_pnl']:.2f}\n")

    # 4) dashboard
    closed = state["closed"]
    wins = [c for c in closed if c["pnl"] > 0]
    lines = [
        "# 高频买新币模拟盘 DASHBOARD",
        "",
        f"更新: {NOW_STR}  |  起始: {CAPITAL:,.0f} USDT ({state['created'][:10]})",
        "",
        f"## NAV: **{nav:,.2f} USDT**  ({(nav/CAPITAL-1)*100:+.2f}%)",
        "",
        f"策略: 买入每个新 Gate 永续（上线1-6h内），{POS_SIZE:.0f}U/单，TP+50%/SL-15%/72h",
        f"回测预期: 毛均值 +3.4%/单、中位 -0.5%、胜率 47% —— 靠 ~10% 止盈单扛收益的彩票组合",
        "",
        f"已平仓: {len(closed)} 单 | 胜率: {len(wins)/len(closed)*100 if closed else 0:.0f}% | "
        f"累计已实现: {state['realized_pnl']:+.2f}U | 持仓: {len(state['positions'])}",
        "",
        "## 当前持仓",
        "| 合约 | 入场价 | 现价 | 浮盈 | 入场时间 |",
        "|---|---|---|---|---|",
    ]
    for name, p in state["positions"].items():
        px = last_price(name) or p["entry"]
        t = dt.datetime.fromtimestamp(p["t_entry"], dt.timezone.utc).strftime("%m-%d %H:%M")
        lines.append(f"| {name} | {p['entry']:.6g} | {px:.6g} | {(px/p['entry']-1)*100:+.1f}% | {t} |")
    lines += ["", "## 最近平仓 (20)", "| 合约 | 出场原因 | 盈亏 |", "|---|---|---|"]
    for c in closed[-20:][::-1]:
        lines.append(f"| {c['name']} | {c['reason']} | {c['pnl']:+.2f}U |")
    DASH_F.write_text("\n".join(lines), encoding="utf-8")

    STATE_F.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"CYCLE OK nav={nav:.2f} open={len(state['positions'])} closed={len(closed)}")


if __name__ == "__main__":
    main()
