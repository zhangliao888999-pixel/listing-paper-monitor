# -*- coding: utf-8 -*-
"""验证"跟踪高盈利钱包"这个想法是否有真实预测价值。

背景: 在某个已经暴涨的币的GMGN Traders榜单上看到"盈利第一"的钱包，
不代表这个钱包真的会挑币——很可能只是狙击机器人见币就冲、或者就是这个币自己的
操盘方马甲。要检验一个钱包是否真有价值，得看它在**很多个不同币**上的历史战绩，
而不是它在你恰好点开的这一个已经涨过的币上的排名。

方法: 用 full_dataset.jsonl 里已经有暴涨结果(peak_mult)的样本币，对每个币重新拉取
Traders榜单前2名钱包，再用GMGN的钱包全局战绩接口(wallet_stat)查这些钱包的历史
realized_profit(跨所有代币的历史已实现盈利，不是只看这一个币)，看两者是否相关：
"入场这个币的钱包，本身历史战绩越好，这个币后续涨得越多"这个假设成不成立。

样本小(GMGN限速明显，一次不敢查太多)，结论仅供参考，不是定论。
"""
import json
import random
import time
import statistics
from pathlib import Path

import requests
from scipy import stats

HERE = Path(__file__).parent
FULL_DATASET = HERE / "full_dataset.jsonl"
OUT = HERE / "wallet_track_record_test.jsonl"

GMGN_S = requests.Session()
GMGN_S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                       "Referer": "https://gmgn.ai/", "Accept": "application/json"})

SAMPLE_N = 20
TOP_K_WALLETS = 2


def get(url, params=None, tries=2):
    for i in range(tries):
        try:
            r = GMGN_S.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (403, 429):
                time.sleep(6 * (i + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(3)
    return None


def load_sample():
    rows = []
    with FULL_DATASET.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("mint") and rec.get("peak_mult") is not None:
                rows.append(rec)
    random.seed(42)
    random.shuffle(rows)
    return rows[:SAMPLE_N]


def top_wallets(mint):
    d = get(f"https://gmgn.ai/vas/api/v1/token_traders/sol/{mint}", {"limit": 10})
    if not d or d.get("code") != 0:
        return []
    data = d.get("data")
    rows = data.get("list", []) if isinstance(data, dict) else (data or [])
    return [r["address"] for r in rows[:TOP_K_WALLETS] if r.get("address")]


def wallet_reputation(wallet):
    d = get(f"https://gmgn.ai/api/v1/wallet_stat/sol/{wallet}/all")
    if not d or d.get("code") != 0:
        return None
    data = d.get("data") or {}
    return {"realized_profit": data.get("realized_profit"), "total_volume": data.get("total_volume")}


def main():
    sample = load_sample()
    print(f"样本币数: {len(sample)}")
    results = []
    with OUT.open("w", encoding="utf-8") as f:
        for i, rec in enumerate(sample):
            wallets = top_wallets(rec["mint"])
            time.sleep(2)
            reps = []
            for w in wallets:
                rep = wallet_reputation(w)
                if rep:
                    reps.append(rep)
                time.sleep(2)
            max_profit = max((r["realized_profit"] for r in reps if r.get("realized_profit") is not None), default=None)
            row = {"name": rec["name"], "mint": rec["mint"], "peak_mult": rec["peak_mult"],
                  "n_wallets_checked": len(reps), "max_realized_profit": max_profit}
            results.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            print(f"  [{i+1}/{len(sample)}] {rec['name']}: peak_mult={rec['peak_mult']:.2f} max_realized_profit={max_profit}")

    paired = [(r["max_realized_profit"], r["peak_mult"]) for r in results if r["max_realized_profit"] is not None]
    print(f"\n有效配对样本: {len(paired)}/{len(results)}")
    if len(paired) >= 6:
        profits = [p[0] for p in paired]
        mults = [p[1] for p in paired]
        rho, pval = stats.spearmanr(profits, mults)
        print(f"Spearman相关系数: {rho:.3f}  p值: {pval:.3f}")
        median_profit = statistics.median(profits)
        hi = [m for p, m in paired if p >= median_profit]
        lo = [m for p, m in paired if p < median_profit]
        print(f"高战绩组(n={len(hi)}) peak_mult均值={statistics.mean(hi):.2f} 中位={statistics.median(hi):.2f}")
        print(f"低战绩组(n={len(lo)}) peak_mult均值={statistics.mean(lo):.2f} 中位={statistics.median(lo):.2f}")
        if len(hi) >= 3 and len(lo) >= 3:
            u, p2 = stats.mannwhitneyu(hi, lo, alternative="two-sided")
            print(f"Mann-Whitney U检验 p值: {p2:.3f}")
    else:
        print("有效样本太少,不做统计检验")


if __name__ == "__main__":
    main()
