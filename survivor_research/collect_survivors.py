# -*- coding: utf-8 -*-
"""2026-07-31新建: "买活下来的币"方向的候选采集器。

背景: 之前"抢新币"的路子实盘证伪了——我们买1-2分钟的新生币,正好撞上操盘方
约75秒的收割节奏,5笔实盘净亏13%本金,4个币直接烂在钱包里。链上数据显示
纸盘和实盘的死亡时间几乎一样(76s vs 74s),说明不是被针对,是这类币本来就
这么死,而纸盘用"最后已知价"结算,给流动性归零的币记了+18.59%的幻影收益。

新方向来自用户提供的访谈记录,受访者的原话:
  "有的币一天就几分钟结束,有的能玩三五个小时,有的能玩一星期。
   我是找那种能玩几个小时那种币去玩的"
  "已经慢慢在起了,然后我再追进去"
  "从这跌到这,然后一般不动的话,一般它几分钟不动的话,我就去买"
  "我跌了不补仓的,我跌了直接割肉"
关键差异: 买的是已经活过几小时、初期收割已经结束的币,是完全不同的币群。

这个脚本只做一件事: 采集"存活时间足够长"的候选池子的基础信息,存成csv,
后续用collect_ohlcv.py拉分钟K线做离线回测。不下单、不碰钱。

用法: python collect_survivors.py [目标条数,默认300]
"""
import csv
import json
import sys
import time
import datetime as dt
from pathlib import Path

import requests

HERE = Path(__file__).parent
OUT_F = HERE / "survivors.csv"
GT = "https://api.geckoterminal.com/api/v2"
H = {"Accept": "application/json;version=20230302",
     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 视频里的筛选条件翻译成可执行阈值:
MIN_AGE_HOURS = 2.0      # "能玩几个小时那种" —— 至少活过2小时,排除几分钟就结束的
MAX_AGE_HOURS = 72.0     # 太老的已经不是这个玩法的标的
MIN_LIQ_USD = 8000       # 流动性太薄的卖不掉(上一轮实盘用$4.6换来的教训)
MIN_VOL24_USD = 20000    # 得有真实成交量,没量的币进去就是自己跟自己玩


def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=H, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1.5 * (i + 1))
    return None


def iter_pools(max_pages=10):
    """从多个入口捞候选: 涨幅榜/成交量榜比new_pools更适合找"已经在起来"的币。"""
    seen = set()
    endpoints = [
        (f"{GT}/networks/solana/trending_pools", {"duration": "1h"}),
        (f"{GT}/networks/solana/trending_pools", {"duration": "6h"}),
        (f"{GT}/networks/solana/pools", {"sort": "h24_volume_usd_desc"}),
        (f"{GT}/networks/solana/pools", {"sort": "h24_tx_count_desc"}),
    ]
    for base, extra in endpoints:
        for page in range(1, max_pages + 1):
            params = dict(extra); params["page"] = page
            d = get(base, params)
            rows = (d or {}).get("data", [])
            if not rows:
                break
            for row in rows:
                addr = row["attributes"].get("address")
                if addr and addr not in seen:
                    seen.add(addr)
                    yield row
            time.sleep(0.3)


def main():
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    now = dt.datetime.now(dt.timezone.utc)
    kept = []
    scanned = 0

    for row in iter_pools():
        scanned += 1
        a = row["attributes"]
        created = a.get("pool_created_at")
        if not created:
            continue
        try:
            age_h = (now - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 3600
        except ValueError:
            continue
        if not (MIN_AGE_HOURS <= age_h <= MAX_AGE_HOURS):
            continue
        try:
            liq = float(a.get("reserve_in_usd") or 0)
            vol24 = float((a.get("volume_usd") or {}).get("h24") or 0)
            fdv = float(a.get("fdv_usd") or 0)
        except (TypeError, ValueError):
            continue
        if liq < MIN_LIQ_USD or vol24 < MIN_VOL24_USD:
            continue

        rel = row.get("relationships", {})
        mint = rel.get("base_token", {}).get("data", {}).get("id", "").split("_")[-1]
        dex = rel.get("dex", {}).get("data", {}).get("id", "")
        pc = a.get("price_change_percentage") or {}
        kept.append({
            "addr": a.get("address"), "name": a.get("name"), "mint": mint, "dex": dex,
            "age_hours": round(age_h, 2), "liq_usd": round(liq, 2),
            "vol24_usd": round(vol24, 2), "fdv_usd": round(fdv, 2),
            "chg_5m": pc.get("m5"), "chg_1h": pc.get("h1"), "chg_6h": pc.get("h6"), "chg_24h": pc.get("h24"),
            "created_at": created, "collected_at": now.isoformat(),
        })
        if len(kept) >= target:
            break

    if kept:
        with OUT_F.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(kept[0].keys()))
            w.writeheader()
            w.writerows(kept)
    print(f"扫描{scanned}个池子,符合条件的存活候选: {len(kept)}个 -> {OUT_F.name}")
    if kept:
        ages = sorted(x["age_hours"] for x in kept)
        liqs = sorted(x["liq_usd"] for x in kept)
        print(f"  年龄分布: 中位数{ages[len(ages)//2]:.1f}小时 (最小{ages[0]:.1f} 最大{ages[-1]:.1f})")
        print(f"  流动性: 中位数${liqs[len(liqs)//2]:,.0f}")
        from collections import Counter
        for d, c in Counter(x["dex"] for x in kept).most_common(6):
            print(f"  {d}: {c}个")


if __name__ == "__main__":
    main()
