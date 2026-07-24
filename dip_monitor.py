# -*- coding: utf-8 -*-
"""策略 B 模拟盘：五所新币"回撤反转"做多 (LBank / XT / MEXC / HTX / Gate)

规则（源自 v5/v6 研究：土狗新币先砸率 70%，先砸-10%的币翻倍概率是走稳币的 3.3 倍）：
  检测   : gate/mexc/htx 用上市时间字段；lbank/xt 用交易对列表差分
  基准   : 第一根真实成交 1h K线收盘（vol >= 首日峰值 2%，规避占位价伪影）
  入场   : 上市后 24h 内，价格自基准回撤 >=10% 且从最低点反弹 >=5% 时买入
  仓位   : 500U/单，最多 10 单，独立账本 10,000 虚拟 USDT
  退出   : TP +50% / SL -15% / 入场后 72h（1h K线按时序判定，同根先触视为 SL）
  滑点   : 买卖各 0.3%

每小时由计划任务 PaperDipMonitor 运行。产出 DASHBOARD_DIP.md / nav_dip.csv / dip.log
"""
import json
import time
import datetime as dt
from pathlib import Path

import requests

HERE = Path(__file__).parent
STATE_F = HERE / "state_dip.json"
NAV_F = HERE / "nav_dip.csv"
DASH_F = HERE / "DASHBOARD_DIP.md"
LOG_F = HERE / "dip.log"

CAPITAL = 10000.0
POS_SIZE = 500.0
MAX_POS = 10
DIP, REBOUND = -0.10, 0.05
TP, SL, TIME_EXIT_H = 0.50, -0.15, 72
WATCH_MAX_H = 26
SLIP = 0.003

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


def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (400, 404):
                return None
        except (requests.RequestException, json.JSONDecodeError):
            pass
        time.sleep(1.5 * (i + 1))
    return None


# ---------- per-exchange adapters ----------

def new_listings_gate(seen):
    d = get("https://api.gateio.ws/api/v4/futures/usdt/contracts") or []
    out = []
    for c in d:
        lt = int(c.get("launch_time") or 0) or int(c.get("create_time") or 0)
        if c["name"] not in seen and lt and NOW - lt < WATCH_MAX_H * 3600 and not c.get("is_pre_market"):
            out.append((c["name"], lt))
    return out


def new_listings_mexc(seen):
    d = get("https://contract.mexc.com/api/v1/contract/detail")
    out = []
    for c in (d or {}).get("data") or []:
        ot = int(c.get("openingTime") or 0) // 1000
        sym = c.get("symbol", "")
        if "STOCK" in sym or "USD1" in sym:  # tokenized equities / non-crypto
            continue
        if sym not in seen and ot and 0 < NOW - ot < WATCH_MAX_H * 3600 and c.get("quoteCoin") == "USDT":
            out.append((sym, ot))
    return out


def new_listings_htx(seen):
    d = get("https://api.huobi.pro/v2/settings/common/symbols")
    out = []
    for s in (d or {}).get("data") or []:
        toa = int(s.get("toa") or 0) // 1000
        sym = s.get("sc", "")
        if sym not in seen and sym.endswith("usdt") and toa and 0 < NOW - toa < WATCH_MAX_H * 3600 \
                and s.get("state") == "online":
            out.append((sym, toa))
    return out


def new_listings_listdiff(exch, seen_lists):
    """lbank/xt: diff current pair list vs stored; first run seeds silently."""
    if exch == "lbank":
        d = get("https://api.lbkex.com/v2/currencyPairs.do")
        cur = set(p for p in ((d or {}).get("data") or []) if p.endswith("_usdt"))
    else:
        d = get("https://sapi.xt.com/v4/public/symbol")
        cur = set(s["symbol"] for s in (((d or {}).get("result") or {}).get("symbols") or [])
                  if s.get("symbol", "").endswith("_usdt") and s.get("state") == "ONLINE")
    if not cur:
        return []
    prev = set(seen_lists.get(exch) or [])
    seen_lists[exch] = sorted(cur | prev)
    if not prev:
        log(f"{exch}: seeded {len(cur)} pairs (first run, no signals)")
        return []
    return [(sym, NOW) for sym in cur - prev]


def klines_1h(exch, sym, t_from):
    """return [(t,o,h,l,c,vol)] ascending since t_from"""
    try:
        if exch == "gate":
            d = get("https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
                    {"contract": sym, "interval": "1h", "from": int(t_from), "to": NOW})
            return [(int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]),
                     float(r.get("sum") or 0)) for r in d or []]
        if exch == "mexc":
            d = get(f"https://contract.mexc.com/api/v1/contract/kline/{sym}",
                    {"interval": "Min60", "start": int(t_from), "end": NOW})
            k = (d or {}).get("data") or {}
            if not k.get("time"):
                return []
            return list(zip(k["time"], k["open"], k["high"], k["low"], k["close"], k["vol"]))
        if exch == "htx":
            d = get("https://api.huobi.pro/market/history/kline",
                    {"symbol": sym, "period": "60min", "size": 200})
            rows = sorted((d or {}).get("data") or [], key=lambda r: r["id"])
            return [(int(r["id"]), float(r["open"]), float(r["high"]), float(r["low"]),
                     float(r["close"]), float(r.get("vol", 0))) for r in rows if r["id"] >= t_from]
        if exch == "lbank":
            d = get("https://api.lbkex.com/v2/kline.do",
                    {"symbol": sym, "size": 200, "type": "hour1", "time": int(t_from)})
            return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                    for r in (d or {}).get("data") or []]
        if exch == "xt":
            d = get("https://sapi.xt.com/v4/public/kline",
                    {"symbol": sym, "interval": "1h", "limit": 200})
            rows = sorted((d or {}).get("result") or [], key=lambda r: int(r["t"]))
            return [(int(r["t"]) // 1000, float(r["o"]), float(r["h"]), float(r["l"]),
                     float(r["c"]), float(r["v"])) for r in rows if int(r["t"]) // 1000 >= t_from]
    except (KeyError, TypeError, ValueError):
        return []
    return []


def real_base(bars):
    """skip placeholder bars: first bar with vol >= 2% of max vol in first 24"""
    if not bars:
        return None, None
    vmax = max(b[5] for b in bars[:24]) if bars else 0
    if vmax <= 0:
        return None, None
    for i, b in enumerate(bars[:20]):
        if b[5] >= 0.02 * vmax:
            return b[4], i
    return None, None


# ---------- main cycle ----------

def main():
    if STATE_F.exists():
        state = json.loads(STATE_F.read_text(encoding="utf-8"))
    else:
        state = {"created": NOW_STR, "cash": CAPITAL, "positions": {}, "closed": [],
                 "watch": {}, "seen": [], "seen_lists": {}, "realized_pnl": 0.0}
        log(f"INIT dip-reversal paper: {CAPITAL} USDT, 5 exchanges, dip{DIP*100:.0f}%/rebound+{REBOUND*100:.0f}%")

    seen = set(state["seen"])

    # 1) detect new listings
    found = []
    for exch, fn in [("gate", new_listings_gate), ("mexc", new_listings_mexc), ("htx", new_listings_htx)]:
        try:
            found += [(exch, sym, lt) for sym, lt in fn(seen)]
        except Exception as e:
            log(f"WARN {exch} detect failed: {e}")
    for exch in ("lbank", "xt"):
        try:
            found += [(exch, sym, lt) for sym, lt in new_listings_listdiff(exch, state["seen_lists"])]
        except Exception as e:
            log(f"WARN {exch} detect failed: {e}")
    for exch, sym, lt in found:
        key = f"{exch}:{sym}"
        if key in seen:
            continue
        seen.add(key)
        state["watch"][key] = {"exch": exch, "sym": sym, "listed": lt, "status": "watching"}
        log(f"NEW LISTING {key} (listed {(NOW-lt)/3600:.1f}h ago) -> watching for dip-reversal")

    # 2) watchlist: check setup
    for key in list(state["watch"].keys()):
        w = state["watch"][key]
        if w["status"] != "watching":
            continue
        age_h = (NOW - w["listed"]) / 3600
        if age_h > WATCH_MAX_H:
            w["status"] = "expired"
            continue
        bars = klines_1h(w["exch"], w["sym"], w["listed"] - 3600)
        base, i0 = real_base(bars)
        if base is None:
            continue
        real = bars[i0:]
        low = min(b[3] for b in real)
        last = real[-1][4]
        if low <= base * (1 + DIP) and last >= low * (1 + REBOUND):
            if len(state["positions"]) >= MAX_POS or state["cash"] < POS_SIZE:
                w["status"] = "skipped_capacity"
                log(f"SETUP but no capacity: {key}")
                continue
            entry = last * (1 + SLIP)
            state["cash"] -= POS_SIZE
            state["positions"][key] = {"exch": w["exch"], "sym": w["sym"], "entry": entry,
                                       "qty": POS_SIZE / entry, "usd": POS_SIZE,
                                       "t_entry": NOW, "base": base, "low": low}
            w["status"] = "entered"
            log(f"BUY {key} @ {entry:.6g} (base {base:.6g}, dip {(low/base-1)*100:.1f}%, rebound confirmed)")
        time.sleep(0.15)

    # 3) manage positions
    for key in list(state["positions"].keys()):
        p = state["positions"][key]
        bars = [b for b in klines_1h(p["exch"], p["sym"], p["t_entry"]) if b[0] >= p["t_entry"]]
        tp_px, sl_px = p["entry"] * (1 + TP), p["entry"] * (1 + SL)
        exit_px = reason = None
        for (t, o, h, l, c, v) in bars:
            if l <= sl_px and h >= tp_px:
                exit_px, reason = sl_px, "SL(ambig)"
                break
            if l <= sl_px:
                exit_px, reason = sl_px, "SL"
                break
            if h >= tp_px:
                exit_px, reason = tp_px, "TP"
                break
        if exit_px is None and NOW - p["t_entry"] > TIME_EXIT_H * 3600:
            exit_px, reason = (bars[-1][4] if bars else p["entry"]), "TIME"
        if exit_px:
            proceeds = p["qty"] * exit_px * (1 - SLIP)
            pnl = proceeds - p["usd"]
            state["cash"] += proceeds
            state["realized_pnl"] += pnl
            state["closed"].append({**p, "key": key, "exit": exit_px, "reason": reason,
                                    "pnl": round(pnl, 2), "t_exit": NOW})
            del state["positions"][key]
            log(f"EXIT {key} [{reason}] pnl {pnl:+.2f}U")
        time.sleep(0.1)

    # prune old watch entries
    state["watch"] = {k: w for k, w in state["watch"].items()
                      if w["status"] == "watching" or NOW - w["listed"] < 7 * 86400}

    # 4) NAV
    open_val = 0.0
    for key, p in state["positions"].items():
        bars = klines_1h(p["exch"], p["sym"], NOW - 7200)
        px = bars[-1][4] if bars else p["entry"]
        p["mark"] = px
        open_val += p["qty"] * px
        time.sleep(0.1)
    nav = state["cash"] + open_val
    hdr = not NAV_F.exists()
    with NAV_F.open("a", encoding="utf-8") as f:
        if hdr:
            f.write("ts,date,nav,cash,open_positions,watching,realized_pnl\n")
        watching = sum(1 for w in state["watch"].values() if w["status"] == "watching")
        f.write(f"{NOW},{NOW_STR[:10]},{nav:.2f},{state['cash']:.2f},{len(state['positions'])},{watching},{state['realized_pnl']:.2f}\n")

    # 5) dashboard
    closed = state["closed"]
    wins = [c for c in closed if c["pnl"] > 0]
    watching = [w for w in state["watch"].values() if w["status"] == "watching"]
    lines = [
        "# 策略B：五所新币回撤反转 模拟盘",
        "",
        f"更新: {NOW_STR}  |  起始: {CAPITAL:,.0f} USDT ({state['created'][:10]})",
        "",
        f"## NAV: **{nav:,.2f} USDT**  ({(nav/CAPITAL-1)*100:+.2f}%)",
        "",
        f"规则: 五所(LBank/XT/MEXC/HTX/Gate)新币，回撤≥10%后反弹≥5%入场，TP+50%/SL-15%/72h",
        f"研究依据: 先砸-10%的新币翻倍率 14.6%（走稳币 4.4%）；五所合计~12 暴涨候选/月",
        "",
        f"持仓 {len(state['positions'])} | 观察中 {len(watching)} | 已平仓 {len(closed)} | "
        f"胜率 {len(wins)/len(closed)*100 if closed else 0:.0f}% | 已实现 {state['realized_pnl']:+.2f}U",
        "",
        "## 观察名单（等待回撤反转）",
        "| 标的 | 上市 | 状态 |",
        "|---|---|---|",
    ]
    for w in watching[:20]:
        t = dt.datetime.fromtimestamp(w["listed"], dt.timezone.utc).strftime("%m-%d %H:%M")
        lines.append(f"| {w['exch']}:{w['sym']} | {t} | 等待形态 |")
    lines += ["", "## 当前持仓", "| 标的 | 入场 | 现价 | 浮盈 |", "|---|---|---|---|"]
    for key, p in state["positions"].items():
        mark = p.get("mark", p["entry"])
        lines.append(f"| {key} | {p['entry']:.6g} | {mark:.6g} | {(mark/p['entry']-1)*100:+.1f}% |")
    lines += ["", "## 最近平仓", "| 标的 | 原因 | 盈亏 |", "|---|---|---|"]
    for c in closed[-20:][::-1]:
        lines.append(f"| {c['key']} | {c['reason']} | {c['pnl']:+.2f}U |")
    DASH_F.write_text("\n".join(lines), encoding="utf-8")

    state["seen"] = sorted(seen)
    STATE_F.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"CYCLE OK nav={nav:.2f} pos={len(state['positions'])} watch={len(watching)} closed={len(closed)}")


if __name__ == "__main__":
    main()
