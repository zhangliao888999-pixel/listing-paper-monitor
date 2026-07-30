# -*- coding: utf-8 -*-
"""2026-07-31新建: 给"买活下来的币"方向拉分钟级K线,供离线回测。

样本选择上刻意避开幸存者偏差: 不从"当前热门榜"取样(那样只会统计到现在还活着
的币,回测结果必然虚高),而是用我们自己历史上见过的全部池子(journal.jsonl里
966个独立地址)——这是"当时看到的全部",死掉的也在里面。筛"活过N小时"这一步
在回测时按K线自己判断,不靠事后状态。

用法: python collect_ohlcv.py [并发数,默认4]
输出: ohlcv/<addr>.json  每个文件是该池子的分钟K线列表
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

HERE = Path(__file__).parent
OUT_DIR = HERE / "ohlcv"
OUT_DIR.mkdir(exist_ok=True)
POOLS_F = HERE / "hist_pools.json"
GT = "https://api.geckoterminal.com/api/v2"
H = {"Accept": "application/json;version=20230302",
     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def fetch_one(addr):
    """拉一个池子的分钟K线。GT一次最多1000根,对我们够用(覆盖16小时)。
    返回(addr, K线根数, 状态)。已经拉过的跳过,方便断点续跑。"""
    out = OUT_DIR / f"{addr}.json"
    if out.exists():
        try:
            n = len(json.loads(out.read_text(encoding="utf-8")))
            return addr, n, "cached"
        except (json.JSONDecodeError, OSError):
            pass
    for attempt in range(3):
        try:
            r = requests.get(f"{GT}/networks/solana/pools/{addr}/ohlcv/minute",
                             params={"aggregate": 1, "limit": 1000}, headers=H, timeout=25)
            if r.status_code == 200:
                ol = r.json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])
                out.write_text(json.dumps(ol), encoding="utf-8")
                return addr, len(ol), "ok"
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 404:
                out.write_text("[]", encoding="utf-8")
                return addr, 0, "404"
            return addr, 0, f"http{r.status_code}"
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    return addr, 0, "failed"


def main():
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    pools = json.loads(POOLS_F.read_text(encoding="utf-8"))
    addrs = list(pools.keys())
    print(f"待拉取池子: {len(addrs)}个,并发={workers}")

    stats = {"ok": 0, "cached": 0, "404": 0, "failed": 0, "other": 0}
    bars_total = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_one, a): a for a in addrs}
        for fut in as_completed(futs):
            addr, n, status = fut.result()
            key = status if status in stats else "other"
            stats[key] += 1
            bars_total += n
            done += 1
            if done % 50 == 0:
                print(f"  进度 {done}/{len(addrs)}  已拿到{bars_total}根K线  {stats}")

    print(f"\n完成: {stats}")
    print(f"总K线根数: {bars_total}")
    # 统计有多少池子的数据够长(活过2小时 = 至少120根分钟线)
    long_enough = 0
    for f in OUT_DIR.glob("*.json"):
        try:
            if len(json.loads(f.read_text(encoding="utf-8"))) >= 120:
                long_enough += 1
        except (json.JSONDecodeError, OSError):
            continue
    print(f"K线覆盖>=120分钟(可用于'活过2小时'回测)的池子: {long_enough}个")


if __name__ == "__main__":
    main()
