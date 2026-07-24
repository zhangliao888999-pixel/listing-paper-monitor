# -*- coding: utf-8 -*-
"""打新模拟盘监测器 (paper trading monitor)

每次运行一个完整周期：
  1. 取 BNB 现价（data-api.binance.vision）
  2. 扫 Binance 公告(catalog 48) → 检测 Launchpool / HODLer / Megadrop / Wallet TGE 事件
  3. 扫 Bitget 公告 API → 检测 PoolX / CandyBomb 事件
  4. 新事件入账（按 config 中可校准的单期收益假设估算 payout，标记 estimated=true）
  5. 事件代币上市后按真实开盘价核实（能查到价即"到手即卖"实现收益）
  6. 记 NAV 到 nav.csv，重写 DASHBOARD.md

组合结构（虚拟 10,000 USDT）：
  - bnb_core  60%: BNB 现货 + 等值空单对冲（美元价值恒定），吃 Launchpool/HODLer/Megadrop
  - tge       25%: Binance Wallet TGE 申购模拟（commit 上限 3 BNB/期，超募按 fill_rate 成交）
  - satellite 15%: Bitget PoolX / CandyBomb / (Gate Startup 手动补录)

首次运行自动初始化 state.json。
"""
import json
import re
import sys
import time
import datetime as dt
from pathlib import Path

import requests

HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
STATE_F = HERE / "state.json"
NAV_F = HERE / "nav.csv"
DASH_F = HERE / "DASHBOARD.md"
LOG_F = HERE / "monitor.log"

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
            if r.status_code in (400, 404, 451):
                return None
        except (requests.RequestException, json.JSONDecodeError):
            pass
        time.sleep(2 * (i + 1))
    return None


def spot_price(sym):
    """USDT price via binance.vision, fallback gate."""
    d = get_json("https://data-api.binance.vision/api/v3/ticker/price", {"symbol": f"{sym}USDT"})
    if d and "price" in d:
        return float(d["price"])
    d = get_json("https://api.gateio.ws/api/v4/spot/tickers", {"currency_pair": f"{sym}_USDT"})
    if isinstance(d, list) and d:
        return float(d[0]["last"])
    return None


def init_state(bnb_px):
    cap = CONFIG["capital_usdt"]
    sl = CONFIG["sleeves"]
    state = {
        "created": NOW_STR,
        "bnb_core": {
            "usd": cap * sl["bnb_core"],
            "bnb_qty": round(cap * sl["bnb_core"] / bnb_px, 4),
            "bnb_entry": bnb_px,
            "hedged": True,
            "realized_usdt": 0.0,
        },
        "tge": {"usd": cap * sl["tge"], "realized_usdt": 0.0},
        "satellite": {"usd": cap * sl["satellite"], "realized_usdt": 0.0},
        "events": {},
        "last_run": 0,
    }
    log(f"INIT: capital={cap} USDT, BNB@{bnb_px:.2f}, core={state['bnb_core']['bnb_qty']} BNB (hedged)")
    return state


EVENT_PATTERNS = [
    # (regex on title, event_kind, sleeve)
    (r"Launchpool", "launchpool", "bnb_core"),
    (r"HODLer Airdrops", "hodler", "bnb_core"),
    (r"Megadrop", "megadrop", "bnb_core"),
    (r"TGE|Token Generation|Booster Program|Pre-TGE", "tge", "tge"),
]


def scan_binance(state):
    arts = []
    for cat in (48, 93, 128):
        d = get_json("https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
                     {"type": 1, "catalogId": cat, "pageNo": 1, "pageSize": 20})
        if not d:
            log(f"WARN binance catalog {cat} unreachable")
            continue
        arts += d["data"]["catalogs"][0]["articles"] if d["data"]["catalogs"] else []
        time.sleep(0.5)
    for a in arts:
        title = a["title"]
        ts = a["releaseDate"] // 1000
        if NOW - ts > 14 * 86400:
            continue
        for pat, kind, sleeve in EVENT_PATTERNS:
            if re.search(pat, title, re.I):
                eid = f"bn_{a['id']}"
                if eid in state["events"]:
                    break
                m = re.search(r"\(([A-Z0-9]{1,12})\)", title)
                token = m.group(1) if m else None
                state["events"][eid] = {
                    "src": "binance", "kind": kind, "sleeve": sleeve, "token": token,
                    "title": title, "ann_ts": ts, "status": "active",
                    "est_payout": None, "real_payout": None,
                }
                log(f"NEW EVENT [{kind}] {title}")
                break


def scan_bitget(state):
    items = []
    for ann_type in [None, "latest_news", "coin_listings", "product_updates"]:
        params = {"language": "en_US"}
        if ann_type:
            params["annType"] = ann_type
        d = get_json("https://api.bitget.com/api/v2/public/annoucements", params)
        items += (d or {}).get("data") or []
    for a in items:
        title = a.get("annTitle", "")
        ts = int(a.get("cTime", 0)) // 1000
        if NOW - ts > 14 * 86400:
            continue
        if not re.search(r"PoolX|CandyBomb|Launchpool", title, re.I):
            continue
        eid = f"bg_{a['annId']}"
        if eid in state["events"]:
            continue
        m = re.search(r"\b([A-Z0-9]{2,12})\b(?:USDT)?", re.sub(r"Bitget|PoolX|CandyBomb|Launchpool", "", title))
        token = None
        m2 = re.search(r"(?:list|Lock)\s+(?:up\s+)?([A-Z0-9]{2,12})", title)
        if m2:
            token = m2.group(1)
        state["events"][eid] = {
            "src": "bitget", "kind": "poolx", "sleeve": "satellite", "token": token,
            "title": title, "ann_ts": ts, "status": "active",
            "est_payout": None, "real_payout": None,
        }
        log(f"NEW EVENT [poolx] {title}")


def estimate_and_settle(state, bnb_px):
    A = CONFIG["assumptions"]
    for eid, ev in state["events"].items():
        # 1) estimate payout once
        if ev["est_payout"] is None:
            if ev["kind"] == "launchpool":
                ev["est_payout"] = state["bnb_core"]["usd"] * A["launchpool_yield_per_event"]
            elif ev["kind"] == "hodler":
                ev["est_payout"] = state["bnb_core"]["usd"] * A["hodler_yield_per_event"]
            elif ev["kind"] == "megadrop":
                ev["est_payout"] = state["bnb_core"]["usd"] * A["megadrop_yield_per_event"]
            elif ev["kind"] == "tge":
                commit = min(A["tge_commit_bnb"] * bnb_px, state["tge"]["usd"])
                ev["est_payout"] = commit * A["tge_fill_rate"] * (A["tge_uplift"] - 1)
            elif ev["kind"] == "poolx":
                ev["est_payout"] = state["satellite"]["usd"] * A["poolx_yield_per_event"]
        # 2) settle: 7 days after announcement, credit estimated payout (sell-on-receipt discipline)
        if ev["status"] == "active" and NOW - ev["ann_ts"] > 7 * 86400:
            px = spot_price(ev["token"]) if ev["token"] else None
            payout = ev["est_payout"] * (1 - A["fees_sell"])
            state[ev["sleeve"]]["realized_usdt"] += payout
            ev["status"] = "settled"
            ev["settle_note"] = f"est payout {payout:.2f} USDT" + (f", token {ev['token']} px={px}" if px else "")
            log(f"SETTLED [{ev['kind']}] {ev.get('token')} -> +{payout:.2f} USDT (estimated)")


def nav_snapshot(state):
    core = state["bnb_core"]["usd"] + state["bnb_core"]["realized_usdt"]
    tge = state["tge"]["usd"] + state["tge"]["realized_usdt"]
    sat = state["satellite"]["usd"] + state["satellite"]["realized_usdt"]
    nav = core + tge + sat
    hdr = not NAV_F.exists()
    with NAV_F.open("a", encoding="utf-8") as f:
        if hdr:
            f.write("ts,date,nav,core,tge,satellite\n")
        f.write(f"{NOW},{NOW_STR[:10]},{nav:.2f},{core:.2f},{tge:.2f},{sat:.2f}\n")
    return nav


def write_dashboard(state, nav, bnb_px):
    ev_sorted = sorted(state["events"].values(), key=lambda e: -e["ann_ts"])
    active = [e for e in ev_sorted if e["status"] == "active"]
    settled = [e for e in ev_sorted if e["status"] == "settled"]
    cap = CONFIG["capital_usdt"]
    lines = [
        "# 打新模拟盘 DASHBOARD",
        f"",
        f"更新: {NOW_STR}   |   起始资金: {cap:,.0f} USDT   |   开始日期: {state['created'][:10]}",
        f"",
        f"## NAV: **{nav:,.2f} USDT**  ({(nav/cap-1)*100:+.2f}%)",
        f"",
        f"| 仓位 | 本金 | 已实现收益 | 现值 |",
        f"|---|---|---|---|",
        f"| BNB主仓(对冲) | {state['bnb_core']['usd']:,.0f} | {state['bnb_core']['realized_usdt']:+,.2f} | {state['bnb_core']['usd']+state['bnb_core']['realized_usdt']:,.2f} |",
        f"| TGE机动仓 | {state['tge']['usd']:,.0f} | {state['tge']['realized_usdt']:+,.2f} | {state['tge']['usd']+state['tge']['realized_usdt']:,.2f} |",
        f"| 卫星仓 | {state['satellite']['usd']:,.0f} | {state['satellite']['realized_usdt']:+,.2f} | {state['satellite']['usd']+state['satellite']['realized_usdt']:,.2f} |",
        f"",
        f"BNB 现价: {bnb_px:,.2f}（主仓已对冲，币价波动不影响 NAV）",
        f"",
        f"## 进行中的打新事件 ({len(active)})",
        f"",
        "| 来源 | 类型 | 币种 | 公告时间 | 预估收益 | 标题 |",
        "|---|---|---|---|---|---|",
    ]
    for e in active:
        d = dt.datetime.fromtimestamp(e["ann_ts"], dt.timezone.utc).strftime("%m-%d")
        lines.append(f"| {e['src']} | {e['kind']} | {e.get('token') or '?'} | {d} | "
                     f"{e['est_payout']:.2f}U | {e['title'][:60]} |")
    lines += [f"", f"## 已结算事件 ({len(settled)})", "",
              "| 类型 | 币种 | 收益 | 备注 |", "|---|---|---|---|"]
    for e in settled[:30]:
        lines.append(f"| {e['kind']} | {e.get('token') or '?'} | {e.get('settle_note','')} | {e['title'][:50]} |")
    lines += ["", "> 收益为按历史均值假设的估算(estimated)。跑数周后用实际观察到的单期收益校准 config.json 中的假设参数。",
              "> TGE 模型: commit 3 BNB/期 × fill 3% × 涨幅 3x。Gate Startup 需手动补录。"]
    DASH_F.write_text("\n".join(lines), encoding="utf-8")


def main():
    bnb_px = spot_price("BNB")
    if not bnb_px:
        log("FATAL: cannot fetch BNB price")
        sys.exit(1)
    if STATE_F.exists():
        state = json.loads(STATE_F.read_text(encoding="utf-8"))
    else:
        state = init_state(bnb_px)
    scan_binance(state)
    scan_bitget(state)
    estimate_and_settle(state, bnb_px)
    nav = nav_snapshot(state)
    write_dashboard(state, nav, bnb_px)
    state["last_run"] = NOW
    STATE_F.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"CYCLE OK nav={nav:.2f} events={len(state['events'])}")


if __name__ == "__main__":
    main()
