# -*- coding: utf-8 -*-
"""2026-07-31新建: survivor策略的前向数据采集器(给VPS 24小时跑)。

回测用的是历史K线,有个天生局限: 只能看到"已经发生"的价格,而且GT对老池子
会清数据。这个采集器做互补的事——每隔几分钟给当前所有"存活候选"拍一张快照
存下来,持续跟踪同一批币接下来几小时的真实走势。攒够之后,这批前向数据
(没有任何幸存者偏差,因为我们是在币还活着的时候就记下它、再看它后来死没死)
是验证回测结论最干净的样本。

不下单、不碰钱,纯数据采集。跟实盘完全独立,不共用任何名额/私钥。

输出: forward_snapshots.jsonl 每行一条快照(时间戳+池子+当时的价格/流动性/量/涨跌幅)

用法: python forward_collector.py [轮次上限,默认无限]
"""
import json
import os
import sys
import time
import datetime as dt
from pathlib import Path

import requests

HERE = Path(__file__).parent
OUT_F = HERE / "forward_snapshots.jsonl"
GT = "https://api.geckoterminal.com/api/v2"
H = {"Accept": "application/json;version=20230302",
     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

INTERVAL_SEC = int(os.environ.get("FORWARD_INTERVAL_SEC", "180"))
MIN_AGE_HOURS = 1.0
MAX_AGE_HOURS = 48.0
MIN_LIQ_USD = 8000
MIN_VOL24_USD = 15000


def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=H, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 * (i + 1)); continue
            return None
        except requests.RequestException:
            time.sleep(1.5 * (i + 1))
    return None


def snapshot_round():
    now = dt.datetime.now(dt.timezone.utc)
    seen = set()
    recorded = 0
    endpoints = [
        (f"{GT}/networks/solana/trending_pools", {"duration": "1h"}),
        (f"{GT}/networks/solana/trending_pools", {"duration": "6h"}),
        (f"{GT}/networks/solana/pools", {"sort": "h24_volume_usd_desc"}),
    ]
    with OUT_F.open("a", encoding="utf-8") as out:
        for base, extra in endpoints:
            for page in (1, 2, 3):
                params = dict(extra); params["page"] = page
                d = get(base, params)
                rows = (d or {}).get("data", [])
                if not rows:
                    break
                for row in rows:
                    a = row["attributes"]
                    addr = a.get("address")
                    if not addr or addr in seen:
                        continue
                    seen.add(addr)
                    created = a.get("pool_created_at")
                    if not created:
                        continue
                    try:
                        age_h = (now - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 3600
                        liq = float(a.get("reserve_in_usd") or 0)
                        vol24 = float((a.get("volume_usd") or {}).get("h24") or 0)
                    except (TypeError, ValueError):
                        continue
                    if not (MIN_AGE_HOURS <= age_h <= MAX_AGE_HOURS):
                        continue
                    if liq < MIN_LIQ_USD or vol24 < MIN_VOL24_USD:
                        continue
                    rel = row.get("relationships", {})
                    pc = a.get("price_change_percentage") or {}
                    rec = {
                        "snap_ts": int(now.timestamp()), "addr": addr, "name": a.get("name"),
                        "age_hours": round(age_h, 2), "price_usd": a.get("base_token_price_usd"),
                        "liq_usd": round(liq, 2), "vol24_usd": round(vol24, 2),
                        "fdv_usd": a.get("fdv_usd"), "dex": rel.get("dex", {}).get("data", {}).get("id", ""),
                        "chg_5m": pc.get("m5"), "chg_1h": pc.get("h1"), "chg_6h": pc.get("h6"),
                    }
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    recorded += 1
                time.sleep(0.3)
    return recorded


def main():
    max_rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    print(f"=== 前向数据采集启动,每{INTERVAL_SEC}秒一轮 ===")
    r = 0
    while max_rounds == 0 or r < max_rounds:
        r += 1
        try:
            n = snapshot_round()
            ts = dt.datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] 第{r}轮: 记录{n}个存活候选快照", flush=True)
        except Exception as e:
            print(f"本轮出错(不影响下一轮): {e}", flush=True)
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
