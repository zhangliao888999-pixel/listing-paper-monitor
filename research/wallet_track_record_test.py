# -*- coding: utf-8 -*-
"""验证两个和"跟单"相关的想法是否有真实数据支持。

问题1: "跟踪高盈利钱包"是否有价值——在某个已经暴涨的币的GMGN Traders榜单上看到
"盈利第一"的钱包，不代表这个钱包真的会挑币，可能只是狙击机器人见币就冲、或者
就是这个币自己的操盘方马甲。要检验一个钱包是否真有价值，得看它在很多个不同币
上的历史战绩(GMGN wallet_stat的realized_profit，跨所有代币)，而不是它在这一个
已经涨过的币上的排名，看这个历史战绩跟这个币后续的peak_mult是否相关。

问题2(用户在看到高频刷量机器人后追问): 那些被check_scalping()检测出"有机器人
高频刷量"的币，是不是往往也伴随着更明显的主力/大户特征(比如更集中的持仓、
更高的清仓比例、更多可疑标签钱包)？如果机器人活跃的币背后确实常有主力坐镇，
这类币可能值得重点关注；如果两者没关系，那机器人活跃就只是噪音，不代表什么。

复用 check_coin.py 已经写好的 check_scalping() / check_wallets()，同一批样本币
一次性把两个问题都测了，不重复消耗GMGN配额(GMGN限速明显，一次不敢查太多)。

样本小，结论仅供参考，不是定论。
"""
import json
import random
import statistics
import sys
import time
from pathlib import Path

import requests
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent.parent))
from check_coin import check_scalping, check_wallets  # noqa: E402

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
            if rec.get("mint") and rec.get("addr") and rec.get("peak_mult") is not None:
                rows.append(rec)
    random.seed(42)
    random.shuffle(rows)
    return rows[:SAMPLE_N]


def top_wallet_addrs(mint):
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
    return data.get("realized_profit")


def main():
    sample = load_sample()
    print(f"样本币数: {len(sample)}")
    results = []
    with OUT.open("w", encoding="utf-8") as f:
        for i, rec in enumerate(sample):
            wallets = top_wallet_addrs(rec["mint"])
            time.sleep(1.5)
            profits = [p for w in wallets if (p := wallet_reputation(w)) is not None]
            for _ in wallets:
                time.sleep(1.5)
            max_profit = max(profits, default=None)

            scalp = check_scalping(rec["addr"])
            wallet_check = check_wallets(rec["mint"])
            time.sleep(1)

            row = {
                "name": rec["name"], "mint": rec["mint"], "peak_mult": rec["peak_mult"],
                "max_realized_profit": max_profit,
                "scalping_flag": scalp.get("flag", False),
                "whale_exit_ratio": wallet_check.get("exit_ratio"),
                "n_suspicious": wallet_check.get("n_suspicious"),
                "n_traders": wallet_check.get("n_traders"),
            }
            results.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            print(f"  [{i+1}/{len(sample)}] {rec['name']}: peak_mult={rec['peak_mult']:.2f} "
                 f"max_profit={max_profit} scalping={row['scalping_flag']} exit_ratio={row['whale_exit_ratio']}")

    print("\n=== 问题1: 早期大买家的历史战绩(realized_profit) vs 这个币的peak_mult ===")
    paired = [(r["max_realized_profit"], r["peak_mult"]) for r in results if r["max_realized_profit"] is not None]
    print(f"有效配对样本: {len(paired)}/{len(results)}")
    if len(paired) >= 6:
        profits = [p[0] for p in paired]
        mults = [p[1] for p in paired]
        rho, pval = stats.spearmanr(profits, mults)
        print(f"Spearman相关系数: {rho:.3f}  p值: {pval:.3f}")
        median_profit = statistics.median(profits)
        hi = [m for p, m in paired if p >= median_profit]
        lo = [m for p, m in paired if p < median_profit]
        print(f"高战绩组(n={len(hi)}) peak_mult中位={statistics.median(hi):.2f}")
        print(f"低战绩组(n={len(lo)}) peak_mult中位={statistics.median(lo):.2f}")
        if len(hi) >= 3 and len(lo) >= 3:
            u, p2 = stats.mannwhitneyu(hi, lo, alternative="two-sided")
            print(f"Mann-Whitney U检验 p值: {p2:.3f}")
    else:
        print("有效样本太少,不做统计检验")

    print("\n=== 问题2: 有机器人刷量(scalping_flag) 的币 是否伴随更明显的主力特征 ===")
    with_bot = [r for r in results if r["scalping_flag"]]
    without_bot = [r for r in results if not r["scalping_flag"]]
    print(f"有刷量机器人: {len(with_bot)}个  无: {len(without_bot)}个")
    for label, group in [("有机器人", with_bot), ("无机器人", without_bot)]:
        exit_ratios = [r["whale_exit_ratio"] for r in group if r["whale_exit_ratio"] is not None]
        sus_ratios = [r["n_suspicious"] / r["n_traders"] for r in group if r.get("n_traders")]
        print(f"  {label}(n={len(group)}): 主力清仓比例中位={statistics.median(exit_ratios) if exit_ratios else '数据不足'}  "
             f"可疑钱包占比中位={statistics.median(sus_ratios) if sus_ratios else '数据不足'}")


if __name__ == "__main__":
    main()
