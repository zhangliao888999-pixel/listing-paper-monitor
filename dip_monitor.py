# -*- coding: utf-8 -*-
"""策略 B 模拟盘：五所新币"回撤反转"做多 (LBank / XT / MEXC / HTX / Gate)

规则（v7 修订，源自严格回测 backtest_dip_rule.py/backtest_dip_rule2.py）：
  检测   : gate/mexc/htx/blofin 用上市时间字段；lbank/xt/ourbit/coinw/poloniex 用交易对列表差分
  基准   : 第一根真实成交 1h K线收盘（vol >= 首日峰值 2%，规避占位价伪影）
  入场   : 上市后 24h 内，价格自基准回撤 >=10% 且从最低点反弹 >=5%，且流动性达标(见下)时买入
  流动性过滤: 观察窗口内累计成交额折算 24h 等效 >= $100,000 才入场（v7新增，防止 ATOP 式假流动性信号）
  仓位   : 500U/单，最多 10 单，独立账本 10,000 虚拟 USDT
  退出   : 不设止盈(让赢家跑) / SL -20% / 入场后 72h（v7 修订：TP+50%严格回测显示会砍掉驱动收益的极端赢家）
  滑点   : 买卖各 0.3%

严格回测结论（gate+lbank+xt, n=1125笔完整交易, 流动性过滤后n=845）：
  旧规则(TP+50/SL-15) 均值 -0.29% ；新规则(无TP/SL-20) 流动性过滤后均值 +6.41%，中位-20.24%，胜率30%
  → 少数大赢家(数倍到十几倍)驱动全部收益，大部分单子(70%)会止损离场，这是高方差策略，非稳定小赚

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
TP, SL, TIME_EXIT_H = None, -0.20, 72  # v7: no TP (let winners run), wider SL
MIN_VOL24_USD = 100000  # v7: liquidity gate, computed as observed-volume-so-far scaled to 24h
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


def new_listings_blofin(seen):
    d = get("https://openapi.blofin.com/api/v1/market/instruments")
    out = []
    for x in (d or {}).get("data") or []:
        lt = int(x.get("listTime") or 0) // 1000
        if x["instId"] not in seen and lt and 0 < NOW - lt < WATCH_MAX_H * 3600:
            out.append((x["instId"], lt))
    return out


def new_listings_listdiff(exch, seen_lists):
    """lbank/xt/ourbit/coinw/poloniex: diff pair list vs stored; first run seeds silently."""
    if exch == "lbank":
        d = get("https://api.lbkex.com/v2/currencyPairs.do")
        cur = set(p for p in ((d or {}).get("data") or []) if p.endswith("_usdt"))
    elif exch == "xt":
        d = get("https://sapi.xt.com/v4/public/symbol")
        cur = set(s["symbol"] for s in (((d or {}).get("result") or {}).get("symbols") or [])
                  if s.get("symbol", "").endswith("_usdt") and s.get("state") == "ONLINE")
    elif exch == "ourbit":
        d = get("https://api.ourbit.com/api/v3/exchangeInfo")
        cur = set(s["symbol"] for s in (d or {}).get("symbols") or []
                  if s.get("symbol", "").endswith("USDT"))
    elif exch == "coinw":
        d = get("https://api.coinw.com/api/v1/public", {"command": "returnSymbol"})
        rows = (d or {}).get("data") or []
        cur = set(r["currencyPair"] for r in rows
                  if isinstance(r, dict) and r.get("currencyPair", "").endswith("_USDT"))
    elif exch == "poloniex":
        d = get("https://api.poloniex.com/markets")
        cur = set(m["symbol"] for m in d or [] if m.get("symbol", "").endswith("_USDT"))
    else:
        cur = set()
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
            # k["vol"] is base-asset volume; convert to quote(USD) for consistent liquidity units
            return list(zip(k["time"], k["open"], k["high"], k["low"], k["close"],
                            [v * c for v, c in zip(k["vol"], k["close"])]))
        if exch == "htx":
            d = get("https://api.huobi.pro/market/history/kline",
                    {"symbol": sym, "period": "60min", "size": 200})
            rows = sorted((d or {}).get("data") or [], key=lambda r: r["id"])
            return [(int(r["id"]), float(r["open"]), float(r["high"]), float(r["low"]),
                     float(r["close"]), float(r.get("vol", 0))) for r in rows if r["id"] >= t_from]
        if exch == "lbank":
            d = get("https://api.lbkex.com/v2/kline.do",
                    {"symbol": sym, "size": 200, "type": "hour1", "time": int(t_from)})
            # lbank r[5] is base-asset volume; convert to quote(USD)
            return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                     float(r[5]) * float(r[4])) for r in (d or {}).get("data") or []]
        if exch == "xt":
            d = get("https://sapi.xt.com/v4/public/kline",
                    {"symbol": sym, "interval": "1h", "limit": 200})
            rows = sorted((d or {}).get("result") or [], key=lambda r: int(r["t"]))
            return [(int(r["t"]) // 1000, float(r["o"]), float(r["h"]), float(r["l"]),
                     float(r["c"]), float(r["v"])) for r in rows if int(r["t"]) // 1000 >= t_from]
        if exch == "blofin":
            d = get("https://openapi.blofin.com/api/v1/market/candles",
                    {"instId": sym, "bar": "1H", "limit": "200"})
            rows = sorted((d or {}).get("data") or [], key=lambda r: int(r[0]))
            return [(int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                     float(r[6])) for r in rows if int(r[0]) // 1000 >= t_from]
        if exch == "ourbit":
            d = get("https://api.ourbit.com/api/v3/klines",
                    {"symbol": sym, "interval": "60m", "limit": 200})
            rows = d if isinstance(d, list) else []
            return [(int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                     float(r[7])) for r in rows if int(r[0]) // 1000 >= t_from]
        if exch == "coinw":
            d = get("https://api.coinw.com/api/v1/public",
                    {"command": "returnChartData", "currencyPair": sym, "period": 1800,
                     "start": int(t_from), "end": NOW})
            bars = []
            for r in (d or {}).get("data") or []:
                if not isinstance(r, dict):
                    continue
                t = int(r.get("date", r.get("time", 0)))
                if t > 10**12:
                    t //= 1000
                # coinw "volume" is base-asset volume; convert to quote(USD)
                close_px = float(r["close"])
                bars.append((t, float(r["open"]), float(r["high"]), float(r["low"]),
                             close_px, float(r.get("volume", 0)) * close_px))
            return sorted(bars)
        if exch == "poloniex":
            d = get(f"https://api.poloniex.com/markets/{sym}/candles",
                    {"interval": "HOUR_1", "limit": 200})
            rows = d if isinstance(d, list) else []
            # r[4] ('amount') is already quote(USD)-denominated turnover
            bars = [(int(r[12]) // 1000, float(r[2]), float(r[1]), float(r[0]), float(r[3]),
                     float(r[4])) for r in rows]
            return sorted(b for b in bars if b[0] >= t_from)
    except (KeyError, TypeError, ValueError):
        return []
    return []


def orderbook_audit(exch, sym, size_usd=POS_SIZE):
    """实盘前滑点审计：抓当前订单簿，算价差 + 吃进 size_usd 的实际成本(bps)。失败返回 None。"""
    try:
        if exch == "gate":
            d = get("https://api.gateio.ws/api/v4/futures/usdt/order_book",
                    {"contract": sym, "limit": 20})
            asks = [(float(a["p"]), float(a["s"])) for a in (d or {}).get("asks", [])]
            bids = [(float(b["p"]), float(b["s"])) for b in (d or {}).get("bids", [])]
        elif exch == "mexc":
            d = get(f"https://contract.mexc.com/api/v1/contract/depth/{sym}")
            dd = (d or {}).get("data") or {}
            asks = [(float(a[0]), float(a[1])) for a in dd.get("asks", [])[:20]]
            bids = [(float(b[0]), float(b[1])) for b in dd.get("bids", [])[:20]]
        elif exch == "lbank":
            d = get("https://api.lbkex.com/v2/depth.do", {"symbol": sym, "size": 20})
            dd = (d or {}).get("data") or {}
            asks = [(float(a[0]), float(a[1])) for a in dd.get("asks", [])]
            bids = [(float(b[0]), float(b[1])) for b in dd.get("bids", [])]
        elif exch == "xt":
            d = get("https://sapi.xt.com/v4/public/depth", {"symbol": sym, "limit": 20})
            dd = (d or {}).get("result") or {}
            asks = [(float(a[0]), float(a[1])) for a in dd.get("asks", [])]
            bids = [(float(b[0]), float(b[1])) for b in dd.get("bids", [])]
        elif exch == "htx":
            d = get("https://api.huobi.pro/market/depth", {"symbol": sym, "type": "step0"})
            tick = (d or {}).get("tick") or {}
            asks = [(float(a[0]), float(a[1])) for a in tick.get("asks", [])[:20]]
            bids = [(float(b[0]), float(b[1])) for b in tick.get("bids", [])[:20]]
        elif exch == "ourbit":
            d = get("https://api.ourbit.com/api/v3/depth", {"symbol": sym, "limit": 20})
            asks = [(float(a[0]), float(a[1])) for a in (d or {}).get("asks", [])]
            bids = [(float(b[0]), float(b[1])) for b in (d or {}).get("bids", [])]
        elif exch == "blofin":
            d = get("https://openapi.blofin.com/api/v1/market/books", {"instId": sym, "size": 20})
            bk = ((d or {}).get("data") or [{}])[0]
            asks = [(float(a[0]), float(a[1])) for a in bk.get("asks", [])]
            bids = [(float(b[0]), float(b[1])) for b in bk.get("bids", [])]
        else:
            return None
        if not asks or not bids:
            return None
        mid = (asks[0][0] + bids[0][0]) / 2
        spread_bps = (asks[0][0] - bids[0][0]) / mid * 1e4
        # walk the ask side to fill size_usd
        remain, cost = size_usd, 0.0
        for p, q in asks:
            take = min(remain, p * q)
            cost += take * (p / mid - 1)
            remain -= take
            if remain <= 0:
                break
        fill_bps = cost / (size_usd - max(remain, 0)) * 1e4 if size_usd > remain else None
        return {"spread_bps": round(spread_bps, 1),
                "fill_cost_bps": round(fill_bps, 1) if fill_bps is not None else None,
                "depth_filled_usd": round(size_usd - max(remain, 0), 0)}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


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
    for exch, fn in [("gate", new_listings_gate), ("mexc", new_listings_mexc),
                     ("htx", new_listings_htx), ("blofin", new_listings_blofin)]:
        try:
            found += [(exch, sym, lt) for sym, lt in fn(seen if exch == "blofin" else seen)]
        except Exception as e:
            log(f"WARN {exch} detect failed: {e}")
    for exch in ("lbank", "xt", "ourbit", "coinw", "poloniex"):
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
            hours_elapsed = max((real[-1][0] - real[0][0]) / 3600, 1.0)
            vol24_equiv = sum(b[5] for b in real) / hours_elapsed * 24
            if vol24_equiv < MIN_VOL24_USD:
                w["status"] = "skipped_illiquid"
                log(f"SKIP illiquid: {key} vol24_equiv=${vol24_equiv:,.0f} < ${MIN_VOL24_USD:,.0f}")
                continue
            if len(state["positions"]) >= MAX_POS or state["cash"] < POS_SIZE:
                w["status"] = "skipped_capacity"
                log(f"SETUP but no capacity: {key}")
                continue
            entry = last * (1 + SLIP)
            ob = orderbook_audit(w["exch"], w["sym"])
            state["cash"] -= POS_SIZE
            state["positions"][key] = {"exch": w["exch"], "sym": w["sym"], "entry": entry,
                                       "qty": POS_SIZE / entry, "usd": POS_SIZE,
                                       "t_entry": NOW, "base": base, "low": low,
                                       "vol24_equiv": vol24_equiv, "ob_at_entry": ob}
            w["status"] = "entered"
            ob_s = (f", 盘口: spread {ob['spread_bps']}bps, 吃{POS_SIZE:.0f}U成本 {ob['fill_cost_bps']}bps"
                    if ob else ", 盘口: 抓取失败")
            log(f"BUY {key} @ {entry:.6g} (base {base:.6g}, dip {(low/base-1)*100:.1f}%, "
                f"vol24_equiv ${vol24_equiv:,.0f}{ob_s})")
        time.sleep(0.15)

    # 3) manage positions
    for key in list(state["positions"].keys()):
        p = state["positions"][key]
        bars = [b for b in klines_1h(p["exch"], p["sym"], p["t_entry"]) if b[0] >= p["t_entry"]]
        sl_px = p["entry"] * (1 + SL)
        exit_px = reason = None
        for (t, o, h, l, c, v) in bars:
            if l <= sl_px:
                exit_px, reason = sl_px, "SL"
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
        "# 策略B：九所新币回撤反转 模拟盘",
        "",
        f"更新: {NOW_STR}  |  起始: {CAPITAL:,.0f} USDT ({state['created'][:10]})",
        "",
        f"## NAV: **{nav:,.2f} USDT**  ({(nav/CAPITAL-1)*100:+.2f}%)",
        "",
        f"规则(v7): 九所新币，回撤≥10%后反弹≥5%+流动性(24h等效≥$10万)入场，无止盈/SL-20%/72h",
        f"回测依据(1125笔严格复盘): 无止盈+流动性过滤后单笔均值+6.4%，中位-20.2%，胜率30%——高方差、少数大赢家驱动收益",
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
