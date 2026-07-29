# -*- coding: utf-8 -*-
"""2026-07-29新增: 直接盯着TNOS这40个已知操盘方钱包地址(tnos_insider_wallets.json,
bundler/transfer_in/creator/dev_team标签),逐笔成交里只要出现这些具体地址在卖,
立刻报警——比"流动性暴跌"这种事后才能确认的信号更早一步,理论上能抓到"刚开始
卖、还没砸崩流动性"的那一瞬间。

用户明确说清楚了这个的难度和局限:操盘方如果是"软件一点瞬间全跑",这个工具
不一定来得及反应(链上确认速度的物理限制,前面已经反复验证过),但至少钱包
发起交易本身是有先后顺序的连续过程,不是真正意义上的"同时"——试一下能不能
抓到这个过程刚开始的头几笔。"""
import sys
import time
import json
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from check_coin import GT_BASE, S, get

ADDR = "jLVprkYrvzAgWaJDuYxhHaTRtXZAQw6MZWyHm1n22g9"
HERE = Path(__file__).parent
LOG_F = HERE / "tnos_insider_sell_watch.log"
WALLETS_F = HERE / "tnos_insider_wallets.json"

POLL_SEC = 8  # 这是最高优先级信号,轮询间隔比其他监控都紧


def log(msg):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        pass
    with LOG_F.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


insiders = set(json.loads(WALLETS_F.read_text(encoding="utf-8")))
log(f"=== 开始盯防{len(insiders)}个已知操盘方钱包的卖出动作 ===")

seen_tx = set()
first_pass = True
n_alerts = 0
consecutive_insider_sells = 0  # 连续几轮内出现操盘方卖出,用来判断是不是真的开始批量跑了
t_start = time.time()
MAX_RUNTIME_SEC = 3600

while time.time() - t_start < MAX_RUNTIME_SEC:
    time.sleep(POLL_SEC)
    d = get(S, f"{GT_BASE}/networks/solana/pools/{ADDR}/trades", {"trade_volume_in_usd_greater_than": 0})
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
            log(f"*** 操盘方钱包卖出信号#{n_alerts} *** {wallet[:10]}... 卖出${usd:,.2f} (tx={a['tx_hash'][:12]}...)")
    first_pass = False
    if round_insider_sells:
        consecutive_insider_sells += 1
        if consecutive_insider_sells >= 2:
            log(f"*** 连续{consecutive_insider_sells}轮出现操盘方卖出,疑似真的开始批量出货了! ***")
    else:
        consecutive_insider_sells = 0

log(f"=== 监控结束,共{n_alerts}次操盘方卖出信号 ===")
