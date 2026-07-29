# -*- coding: utf-8 -*-
"""2026-07-29新增(通用版,从tnos_insider_sell_watch.py抽出来): 直接盯着某个池子
已知操盘方钱包地址列表,逐笔成交里只要出现这些具体地址在卖,立刻报警——比"流动性
暴跌"这种事后才能确认的信号更早一步。TNOS那次实测抓到过一笔比背景噪音大5-10倍
的异常卖出,21秒后价格就崩了98%。

用法: python insider_sell_watch.py <池子地址> <钱包列表json文件> <日志文件名前缀>
"""
import sys
import time
import json
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from check_coin import GT_BASE, S, get

POLL_SEC = 8
MAX_RUNTIME_SEC = 3600


def log(log_f, msg):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        pass
    with log_f.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    if len(sys.argv) < 4:
        print("用法: python insider_sell_watch.py <池子地址> <钱包列表json文件> <日志文件名前缀>")
        return
    addr, wallets_f, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
    here = Path(__file__).parent
    log_f = here / f"{prefix}_insider_sell_watch.log"

    insiders = set(json.loads(Path(wallets_f).read_text(encoding="utf-8")))
    log(log_f, f"=== 开始盯防{len(insiders)}个已知操盘方钱包的卖出动作 ===")

    seen_tx = set()
    first_pass = True
    n_alerts = 0
    consecutive_insider_sells = 0
    t_start = time.time()

    while time.time() - t_start < MAX_RUNTIME_SEC:
        time.sleep(POLL_SEC)
        d = get(S, f"{GT_BASE}/networks/solana/pools/{addr}/trades", {"trade_volume_in_usd_greater_than": 0})
        rows = (d or {}).get("data", [])
        rows.sort(key=lambda r: r["attributes"]["block_timestamp"])
        new_rows = [r for r in rows if r["attributes"]["tx_hash"] not in seen_tx]
        round_insider_sells = []
        for row in new_rows:
            a = row["attributes"]
            seen_tx.add(a["tx_hash"])
            if first_pass:
                continue
            wallet = a.get("tx_from_address")
            if wallet in insiders and a["kind"] == "sell":
                usd = float(a.get("volume_in_usd") or 0)
                n_alerts += 1
                round_insider_sells.append((wallet, usd))
                log(log_f, f"*** 操盘方钱包卖出信号#{n_alerts} *** {wallet[:10]}... 卖出${usd:,.2f} (tx={a['tx_hash'][:12]}...)")
        first_pass = False
        if round_insider_sells:
            consecutive_insider_sells += 1
            if consecutive_insider_sells >= 2:
                log(log_f, f"*** 连续{consecutive_insider_sells}轮出现操盘方卖出,疑似真的开始批量出货了! ***")
        else:
            consecutive_insider_sells = 0

    log(log_f, f"=== 监控结束,共{n_alerts}次操盘方卖出信号 ===")


if __name__ == "__main__":
    main()
