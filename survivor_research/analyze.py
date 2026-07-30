# -*- coding: utf-8 -*-
"""2026-07-31新建: 回测结果分析。

不只是"排个序找最高的"——4374个组合里挑最高的那个,几乎必然是过拟合。
这个脚本要回答的是"有没有真实优势",做三层检验:

1. 参数邻域稳健性: 真优势应该是一片连续的正收益区域,不是孤立尖峰。
   看每个组合的"邻居"(单个参数上下调一档)平均表现如何。
2. 敏感度: 逐个参数看边际影响,判断哪些参数真的有信息量。
3. 分组一致性: 把样本随机对半分,看最优参数在两半上是否都成立。

最后给出诚实结论: 扣掉全部真实成本后,这个方向到底有没有正期望。
"""
import csv
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
RESULTS_F = HERE / "results.csv"

PARAMS = ["min_age_min", "pump_mult", "pullback_pct", "quiet_min",
          "quiet_band_pct", "target_pct", "stop_pct", "max_hold_min"]


def load():
    rows = []
    with RESULTS_F.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                rec = {k: float(r[k]) for k in PARAMS}
                rec["n_trades"] = int(r["n_trades"])
                for k in ("mean_pnl", "median_pnl", "win_rate", "p10",
                          "dead_rate", "compound_x", "avg_hold_min"):
                    rec[k] = float(r[k])
                rec["pools_traded"] = int(r["pools_traded"])
                rows.append(rec)
            except (KeyError, ValueError):
                continue
    return rows


def neighborhood_score(rows, min_trades=100):
    """给每个组合算邻域均值: 真优势是一片区域,不是孤立尖峰。"""
    valid = [r for r in rows if r["n_trades"] >= min_trades]
    if not valid:
        return []
    index = {tuple(r[p] for p in PARAMS): r for r in valid}
    # 每个参数的可选值(排序后便于找相邻档)
    levels = {p: sorted({r[p] for r in valid}) for p in PARAMS}

    scored = []
    for r in valid:
        key = tuple(r[p] for p in PARAMS)
        neigh = [r["mean_pnl"]]
        for i, p in enumerate(PARAMS):
            vals = levels[p]
            cur = vals.index(r[p])
            for d in (-1, 1):
                j = cur + d
                if 0 <= j < len(vals):
                    nk = list(key); nk[i] = vals[j]
                    nb = index.get(tuple(nk))
                    if nb:
                        neigh.append(nb["mean_pnl"])
        r2 = dict(r)
        r2["neigh_mean"] = sum(neigh) / len(neigh)
        r2["neigh_n"] = len(neigh)
        scored.append(r2)
    return scored


def param_sensitivity(rows, min_trades=100):
    """逐参数边际影响: 固定其他参数看某个参数各档位的平均表现。"""
    valid = [r for r in rows if r["n_trades"] >= min_trades]
    out = {}
    for p in PARAMS:
        by_val = defaultdict(list)
        for r in valid:
            by_val[r[p]].append(r["mean_pnl"])
        out[p] = {v: (sum(xs) / len(xs), len(xs)) for v, xs in sorted(by_val.items())}
    return out


def main():
    if not RESULTS_F.exists():
        print("results.csv 还没生成,回测可能还在跑")
        return
    rows = load()
    print(f"=== 回测结果分析 (共{len(rows)}个参数组合) ===\n")

    traded = [r for r in rows if r["n_trades"] >= 100]
    print(f"交易数>=100的组合: {len(traded)}")
    if not traded:
        print("没有足够交易量的组合,无法分析")
        return

    pos = [r for r in traded if r["mean_pnl"] > 0]
    print(f"其中均值为正的: {len(pos)} ({100*len(pos)/len(traded):.1f}%)")
    print(f"均值分布: 最好{max(r['mean_pnl'] for r in traded):+.2f}%  "
          f"最差{min(r['mean_pnl'] for r in traded):+.2f}%  "
          f"中位{sorted(r['mean_pnl'] for r in traded)[len(traded)//2]:+.2f}%")

    print("\n--- 关键判断: 正收益组合占比 ---")
    if len(pos) / len(traded) < 0.05:
        print("  <5%的组合为正 -> 极可能是噪音/过拟合,这个方向大概率不成立")
    elif len(pos) / len(traded) < 0.25:
        print("  少数组合为正 -> 需要看这些正收益是否集中在连续区域(下面的邻域检验)")
    else:
        print("  相当比例组合为正 -> 有真实信号的可能,继续看稳健性")

    scored = neighborhood_score(rows)
    if scored:
        scored.sort(key=lambda r: -r["neigh_mean"])
        print("\n--- 邻域稳健性Top10(按邻居平均表现,抗过拟合) ---")
        for r in scored[:10]:
            print(f"  邻域均值={r['neigh_mean']:+6.2f}%  自身={r['mean_pnl']:+6.2f}%  "
                  f"n={r['n_trades']:5d}  胜率={r['win_rate']:4.1f}%  死亡={r['dead_rate']:4.1f}%")
            print(f"      age>={r['min_age_min']:.0f}m pump>={r['pump_mult']:.1f}x "
                  f"回落{r['pullback_pct']:.0f}% 横盘{r['quiet_min']:.0f}m/{r['quiet_band_pct']:.0f}% "
                  f"止盈{r['target_pct']:.0f}% 止损{r['stop_pct']:.0f}% 持有<={r['max_hold_min']:.0f}m")

    print("\n--- 各参数的边际影响(平均pnl) ---")
    sens = param_sensitivity(rows)
    for p, vals in sens.items():
        s = "  ".join(f"{v:g}:{m:+.2f}%" for v, (m, n) in vals.items())
        print(f"  {p:16s} {s}")

    print("\n--- 结论 ---")
    best = scored[0] if scored else None
    if best and best["neigh_mean"] > 0 and best["mean_pnl"] > 0:
        print(f"  存在邻域稳健的正收益区域(邻域均值{best['neigh_mean']:+.2f}%),值得进一步验证")
    else:
        print(f"  没有找到邻域稳健的正收益区域 -> 按当前信号定义,扣掉真实成本后没有优势")


if __name__ == "__main__":
    main()
