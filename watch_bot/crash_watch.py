# -*- coding: utf-8 -*-
"""2026-07-29新增(通用版,从tnos_crash_watch.py抽出来): 给任意池子搭高密度崩盘
取证监控——GDWR那次教训是只有两个孤立快照,中间怎么变化的完全是黑的,而且崩盘
那一刻的逐笔记录被后续恐慌抛售冲出了300笔保留窗口,永久丢失了触发那一下的证据。

两个问题一起解决:
  1. 逐笔成交持续存档(每60秒抓一次/trades,追加写入jsonl,不依赖事后去查)
  2. 操盘方卖出比例每5分钟记一次(比lifecycle_logger的10分钟更细)
一旦流动性暴跌,立刻额外做一次完整快照+逐笔抓取,标注"崩盘时刻"。

用法: python crash_watch.py <池子地址> <mint地址> <日志文件名前缀>
"""
import os
import sys
import time
import json
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from check_coin import GT_BASE, S, GMGN_S, get

PRICE_POLL_SEC = 60
SELLPCT_POLL_SEC = 300
LIQ_CRASH_THRESHOLD = 0.7
# 2026-07-29晚间改: 原来1小时上限是为了配合Claude逐小时手动续,通宵没人盯着重启
# 风险更高(错过一次通知,这个币就断档几小时没人看),改成8小时,覆盖一整晚睡眠时间。
# 白天再改: 云端job单次最长6小时,容器到点就被强制杀掉,用MAX_RUNTIME_SEC环境
# 变量覆盖,云端workflow设成比job本身timeout-minutes略短,本地不设时还是8小时。
MAX_RUNTIME_SEC = int(os.environ.get("MAX_RUNTIME_SEC", "28800"))


def log(log_f, msg):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        pass
    with log_f.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def archive_trades(addr, archive_f, seen_tx, tag=""):
    """2026-07-29晚间修复: 原来每次把整段300笔/trades响应原样追加写入,慢速池子
    (比如tnos2这种能健康跑好几个小时的)相邻两次轮询之间大量重复,几小时下来
    文件涨到100MB+,把GitHub单文件100MB上限直接顶爆,导致整个push被拒绝(今晚
    实测发现)。改成只追加没见过的tx_hash(逻辑跟snipe_exit.py的seen_tx去重
    一样),存储量只跟真实发生的成交数成正比,不再被轮询次数乘出虚高体积。"""
    d = get(S, f"{GT_BASE}/networks/solana/pools/{addr}/trades", {"trade_volume_in_usd_greater_than": 0})
    rows = (d or {}).get("data", [])
    new_rows = [r for r in rows if r.get("attributes", {}).get("tx_hash") not in seen_tx]
    for r in new_rows:
        seen_tx.add(r.get("attributes", {}).get("tx_hash"))
    if new_rows:
        with archive_f.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"archived_at": time.time(), "tag": tag, "n_trades": len(new_rows), "trades": new_rows}, ensure_ascii=False) + "\n")
    return len(new_rows)


def get_pool(addr):
    d = get(S, f"{GT_BASE}/networks/solana/pools/{addr}")
    a = (d or {}).get("data", {}).get("attributes", {})
    try:
        return {"price": float(a.get("base_token_price_usd") or 0), "liq": float(a.get("reserve_in_usd") or 0)}
    except (TypeError, ValueError):
        return None


def get_insider_sell_pct(mint):
    d = get(GMGN_S, f"https://gmgn.ai/vas/api/v1/token_traders/sol/{mint}", {"limit": 40})
    rows = (d or {}).get("data", {}).get("list", [])
    insiders = [r for r in rows if any(t in (r.get("maker_token_tags") or [])
               for t in ("bundler", "transfer_in", "creator", "dev_team"))]
    clean = [r for r in insiders if 0 < (r.get("total_cost") or 0) < 1_000_000]
    if not clean:
        return None
    total_cost = sum(r.get("total_cost") or 0 for r in clean)
    total_sold = sum((r.get("total_cost") or 0) * (r.get("sell_amount_percentage") or 0) for r in clean)
    return total_sold / total_cost if total_cost else None


def main():
    if len(sys.argv) < 4:
        print("用法: python crash_watch.py <池子地址> <mint地址> <日志文件名前缀>")
        return
    addr, mint, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
    here = Path(__file__).parent
    log_f = here / f"{prefix}_crash_watch.log"
    archive_f = here / f"{prefix}_trades_archive.jsonl"

    log(log_f, f"=== {prefix} 高密度崩盘取证监控启动 ===")
    baseline = get_pool(addr)
    if not baseline:
        log(log_f, "拿不到基线数据,退出")
        return
    log(log_f, f"基线: 价格${baseline['price']:.8f} 流动性${baseline['liq']:,.0f}")
    peak_liq = baseline["liq"]

    last_sellpct_check = 0
    crashed = False
    t_start = time.time()
    seen_tx = set()

    while time.time() - t_start < MAX_RUNTIME_SEC and not crashed:
        time.sleep(PRICE_POLL_SEC)
        pool = get_pool(addr)
        if not pool:
            continue
        peak_liq = max(peak_liq, pool["liq"])
        n_archived = archive_trades(addr, archive_f, seen_tx)

        line = f"价格${pool['price']:.8f}  流动性${pool['liq']:,.0f}(峰值${peak_liq:,.0f})  已存档{n_archived}笔逐笔"

        if time.time() - last_sellpct_check >= SELLPCT_POLL_SEC:
            sell_pct = get_insider_sell_pct(mint)
            last_sellpct_check = time.time()
            line += f"  操盘方卖出比例={sell_pct*100:.1f}%" if sell_pct is not None else "  卖出比例=查不到"

        log(log_f, line)

        if pool["liq"] < peak_liq * (1 - LIQ_CRASH_THRESHOLD):
            log(log_f, "*** 流动性暴跌,疑似崩盘发生! 立刻加密取证 ***")
            archive_trades(addr, archive_f, seen_tx, tag="CRASH_MOMENT")
            sell_pct = get_insider_sell_pct(mint)
            log(log_f, f"崩盘时刻快照: 价格${pool['price']:.8f} 流动性${pool['liq']:,.0f} 操盘方卖出比例={sell_pct}")
            crashed = True

    log(log_f, f"=== 监控结束(崩盘={'是' if crashed else '否,本段时间内仍存活'}) ===")


if __name__ == "__main__":
    main()
