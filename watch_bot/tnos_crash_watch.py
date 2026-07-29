# -*- coding: utf-8 -*-
"""2026-07-29新增: 专门给TNOS搭的高密度崩盘取证监控——GDWR那次教训是只有两个
孤立快照(10小时6.1%卖出、死亡时99.95%),中间怎么变化的完全是黑的,而且崩盘那
一刻的逐笔记录被后续的恐慌抛售冲出了300笔保留窗口,永久丢失了触发那一下的证据。

这次两个问题一起解决:
  1. 逐笔成交持续存档(每60秒抓一次/trades,追加写入jsonl,不依赖事后去查,
     不会再被冲掉)
  2. 操盘方卖出比例按更密的间隔记录(每5分钟,比lifecycle_logger的10分钟更细),
     真崩的时候能看清楚是渐变爬升还是瞬间跳变
一旦流动性暴跌,立刻额外做一次完整快照+逐笔抓取,并在日志里标注"崩盘时刻"。
"""
import sys
import time
import json
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from check_coin import GT_BASE, S, GMGN_S, get

ADDR = "jLVprkYrvzAgWaJDuYxhHaTRtXZAQw6MZWyHm1n22g9"
MINT = "XQ3UaYsPJP5pvusq4PcnJDSV8STw33TGMJzq9ospump"
HERE = Path(__file__).parent
LOG_F = HERE / "tnos_crash_watch.log"
TRADES_ARCHIVE_F = HERE / "tnos_trades_archive.jsonl"

PRICE_POLL_SEC = 60      # 价格/流动性,便宜,可以勤查
TRADES_POLL_SEC = 60     # 逐笔存档,同样每60秒抓一次,不依赖事后回查
SELLPCT_POLL_SEC = 300   # GMGN卖出比例,贵一点,5分钟一次(比lifecycle_logger的10分钟更细)
LIQ_CRASH_THRESHOLD = 0.7  # 流动性单次跌破70%,判定疑似崩盘,立刻加密取证


def log(msg):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        pass
    with LOG_F.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def archive_trades(tag=""):
    d = get(S, f"{GT_BASE}/networks/solana/pools/{ADDR}/trades", {"trade_volume_in_usd_greater_than": 0})
    rows = (d or {}).get("data", [])
    with TRADES_ARCHIVE_F.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"archived_at": time.time(), "tag": tag, "n_trades": len(rows), "trades": rows}, ensure_ascii=False) + "\n")
    return len(rows)


def get_pool():
    d = get(S, f"{GT_BASE}/networks/solana/pools/{ADDR}")
    a = (d or {}).get("data", {}).get("attributes", {})
    try:
        return {"price": float(a.get("base_token_price_usd") or 0), "liq": float(a.get("reserve_in_usd") or 0)}
    except (TypeError, ValueError):
        return None


def get_insider_sell_pct():
    d = get(GMGN_S, f"https://gmgn.ai/vas/api/v1/token_traders/sol/{MINT}", {"limit": 40})
    rows = (d or {}).get("data", {}).get("list", [])
    insiders = [r for r in rows if any(t in (r.get("maker_token_tags") or [])
               for t in ("bundler", "transfer_in", "creator", "dev_team"))]
    clean = [r for r in insiders if 0 < (r.get("total_cost") or 0) < 1_000_000]
    if not clean:
        return None
    total_cost = sum(r.get("total_cost") or 0 for r in clean)
    total_sold = sum((r.get("total_cost") or 0) * (r.get("sell_amount_percentage") or 0) for r in clean)
    return total_sold / total_cost if total_cost else None


log("=== TNOS高密度崩盘取证监控启动 ===")
baseline = get_pool()
log(f"基线: 价格${baseline['price']:.8f} 流动性${baseline['liq']:,.0f}")
peak_liq = baseline["liq"]

last_sellpct_check = 0
crashed = False
t_start = time.time()
MAX_RUNTIME_SEC = 3600  # 这一段先跑1小时,配合用户要及时看到进展的要求

while time.time() - t_start < MAX_RUNTIME_SEC and not crashed:
    time.sleep(PRICE_POLL_SEC)
    pool = get_pool()
    if not pool:
        continue
    peak_liq = max(peak_liq, pool["liq"])
    n_archived = archive_trades()

    line = f"价格${pool['price']:.8f}  流动性${pool['liq']:,.0f}(峰值${peak_liq:,.0f})  已存档{n_archived}笔逐笔"

    if time.time() - last_sellpct_check >= SELLPCT_POLL_SEC:
        sell_pct = get_insider_sell_pct()
        last_sellpct_check = time.time()
        line += f"  操盘方卖出比例={sell_pct*100:.1f}%" if sell_pct is not None else "  卖出比例=查不到"

    log(line)

    if pool["liq"] < peak_liq * (1 - LIQ_CRASH_THRESHOLD):
        log("*** 流动性暴跌,疑似崩盘发生! 立刻加密取证 ***")
        archive_trades(tag="CRASH_MOMENT")
        sell_pct = get_insider_sell_pct()
        log(f"崩盘时刻快照: 价格${pool['price']:.8f} 流动性${pool['liq']:,.0f} 操盘方卖出比例={sell_pct}")
        crashed = True

log(f"=== 监控结束(崩盘={'是' if crashed else '否,本段时间内仍存活'}) ===")
