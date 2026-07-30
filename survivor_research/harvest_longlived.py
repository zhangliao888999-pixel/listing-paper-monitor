# -*- coding: utf-8 -*-
"""2026-07-31新建: 收集"活得够久"的币的完整分钟K线,给survivor回测当样本。

为什么要单独做: 我们journal里966个池子几乎全是"抢新币"策略抓的,大部分几分钟
就死(K线中位数只有8根),正好是新策略要避开的那种。要回测"买活过几小时的币",
必须有大量"真活了几小时"的样本。

样本来源(尽量减少幸存者偏差):
  1. GT trending/volume榜 —— 当前活跃的币,能拉到完整历史
  2. GT new_pools往前翻 —— 最近几天创建的池子,不管现在死没死都收
对每个候选拉分钟K线,只保留K线>=120根(活过2小时)的,其余丢弃。
关键: 回测时把"死亡"当全损处理(backtest.py已经这么做),所以即使这批样本
偏向"活得久的",回测里遇到中途死亡照样记归零,不会高估。

用法: python harvest_longlived.py [目标数量,默认800] [并发,默认5]
输出: ohlcv2/<addr>.json  (跟ohlcv/分开,这批是干净的长寿样本)
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

HERE = Path(__file__).parent
OUT_DIR = HERE / "ohlcv2"
OUT_DIR.mkdir(exist_ok=True)
META_F = HERE / "longlived_meta.jsonl"
GT = "https://api.geckoterminal.com/api/v2"
H = {"Accept": "application/json;version=20230302",
     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# 2026-07-31改: GT免费版约30请求/分钟,每个IP独立预算。之前本机同时跑两个采集器
# (collect_ohlcv 4线程 + harvest 5线程)把本机IP的预算撑爆,全程429连候选都没拿到。
# VPS上单独跑的前向采集器在自己IP上完全正常,证明就是本机并发太多。现在本机只跑
# 这一个采集器,配一个全局最小请求间隔的节流器,遇到429指数退避,不再盲目并发。
_last_req = [0.0]
MIN_GAP_SEC = 2.2   # 全局请求最小间隔 ≈ 27请求/分钟,压在免费版限额下


def get(url, params=None, tries=5):
    for i in range(tries):
        gap = time.time() - _last_req[0]
        if gap < MIN_GAP_SEC:
            time.sleep(MIN_GAP_SEC - gap)
        _last_req[0] = time.time()
        try:
            r = requests.get(url, params=params, headers=H, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(5 * (i + 1)); continue
            return None
        except requests.RequestException:
            time.sleep(2 * (i + 1))
    return None


def gather_candidates(target):
    """从多个榜单+new_pools翻页,凑够target个不重复的候选池子地址。"""
    seen = {}
    sources = []
    for dur in ("5m", "1h", "6h", "24h"):
        sources.append((f"{GT}/networks/solana/trending_pools", {"duration": dur}))
    for sort in ("h24_volume_usd_desc", "h24_tx_count_desc", "h6_trend_score_desc"):
        sources.append((f"{GT}/networks/solana/pools", {"sort": sort}))
    sources.append((f"{GT}/networks/solana/new_pools", {}))

    for base, extra in sources:
        for page in range(1, 11):
            if len(seen) >= target * 2:
                return list(seen.items())
            params = dict(extra); params["page"] = page
            d = get(base, params)
            rows = (d or {}).get("data", [])
            if not rows:
                break
            for row in rows:
                a = row["attributes"]
                addr = a.get("address")
                if addr and addr not in seen:
                    rel = row.get("relationships", {})
                    seen[addr] = {
                        "name": a.get("name"),
                        "dex": rel.get("dex", {}).get("data", {}).get("id", ""),
                        "created_at": a.get("pool_created_at"),
                    }
            time.sleep(0.25)
    return list(seen.items())


def fetch_ohlcv(item):
    addr, meta = item
    out = OUT_DIR / f"{addr}.json"
    if out.exists():
        try:
            n = len(json.loads(out.read_text(encoding="utf-8")))
            return addr, n, "cached", meta
        except (json.JSONDecodeError, OSError):
            pass
    d = get(f"{GT}/networks/solana/pools/{addr}/ohlcv/minute", {"aggregate": 1, "limit": 1000})
    if d is None:
        return addr, 0, "failed", meta
    ol = d.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    if len(ol) >= 120:                     # 只留活过2小时的
        out.write_text(json.dumps(ol), encoding="utf-8")
        return addr, len(ol), "kept", meta
    return addr, len(ol), "tooshort", meta


def main():
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 800
    print(f"收集候选中(目标{target}个长寿样本)...", flush=True)
    cands = gather_candidates(target)
    print(f"拿到{len(cands)}个不重复候选,开始拉K线并筛>=120根...", flush=True)

    # 单线程串行: 全局节流器已经把请求序列化了,多线程只会互相抢锁没有意义,
    # 反而更容易触发429。串行 + 2.2秒间隔 = 稳定在限额下,慢但不会断。
    stats = {"kept": 0, "cached": 0, "tooshort": 0, "failed": 0}
    with META_F.open("a", encoding="utf-8") as mf:
        for done, it in enumerate(cands, 1):
            addr, n, status, meta = fetch_ohlcv(it)
            stats[status] = stats.get(status, 0) + 1
            if status == "kept":
                mf.write(json.dumps({"addr": addr, "bars": n, **meta}, ensure_ascii=False) + "\n")
                mf.flush()
            if done % 25 == 0:
                total = stats['kept'] + stats['cached']
                print(f"  {done}/{len(cands)}  已保留{total}个长寿样本  {stats}", flush=True)

    total_kept = len([f for f in OUT_DIR.glob('*.json') if f.stat().st_size > 200])
    print(f"\n完成: {stats}", flush=True)
    print(f"ohlcv2/里现有长寿样本(活过2小时): {total_kept}个", flush=True)


if __name__ == "__main__":
    main()
