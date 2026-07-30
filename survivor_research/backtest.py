# -*- coding: utf-8 -*-
"""2026-07-31新建: "买活下来的币"策略回测引擎。

策略规则来自用户提供的访谈(受访者两个牛市实盘验证过的手法),翻译成可执行信号:
  1. 只看活过一定时间的币("能玩几个小时那种",排除几分钟就死的收割盘)
  2. 币要有过一波真实拉升("已经慢慢在起了")
  3. 入场信号: 冲高->回落->横盘几分钟不动("跌到这,几分钟不动的话,我就去买")
  4. 出场: 目标几十个点/几分钟到几十分钟就走;跌了直接割肉不补仓

执行真实性建模(全部来自我们5笔实盘用真钱换的教训,不做理想化假设):
  - 成交量约束: 该分钟成交量不足仓位的VOLUME_MULT倍时判定买不进/卖不出
    (Coupe教训: 纸面上"能卖",实际没有对手盘)
  - 滑点+手续费: 每边收SLIP_PCT,双边合计约2%起
    (实测: Jupiter往返+优先费+价差,3%滑点档经常不够用)
  - 崩盘不可逃逸: 单根K线跌幅超过CRASH_BAR_PCT时,这根和下一根都不允许成交
    (mer教训: 崩的那一两分钟里你的卖单只会被拒,能出的价是崩完后的价)
  - 持仓中币死亡(连续DEAD_SILENT_MIN分钟零成交量)=本金归零,不是"按最后价退出"
    (纸盘给流动性归零的币记+18.59%幻影收益的教训)

用法: python backtest.py [参数组合上限,默认全扫]
输出: results.csv 每行一个参数组合的汇总指标
"""
import csv
import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
# 2026-07-31改: 默认用ohlcv2/(harvest_longlived.py收集的"活过2小时"样本)。
# 老的ohlcv/是journal里966个抢新币池子,大部分几分钟就死(K线中位数8根),
# 是新策略要避开的那种,不适合回测。可用环境变量OHLCV_DIR覆盖。
import os as _os
OHLCV_DIR = HERE / _os.environ.get("OHLCV_DIR", "ohlcv2")
RESULTS_F = HERE / "results.csv"

# ---- 执行真实性常量(不进扫参,这些是物理约束不是策略选择) ----
POS_USD = 5.0            # 和实盘一致的仓位
VOLUME_MULT = 20         # 该分钟成交量须>=仓位的20倍才认为能无冲击成交
SLIP_PCT = 1.0           # 每边1%滑点+手续费(保守中间值)
CRASH_BAR_PCT = 40       # 单根K线(高->收)跌超40%视为崩盘K线,当根和下一根禁止成交
DEAD_SILENT_MIN = 10     # 连续10分钟零成交量 = 币死了,持仓归零

# ---- 扫参空间(策略选择) ----
GRID = {
    "min_age_min":   [60, 120, 240],        # 入场前币至少活了多久(分钟)
    "pump_mult":     [2.0, 3.0, 5.0],       # 此前从最低点至少涨过几倍(证明"起来过")
    "pullback_pct":  [20, 35, 50],          # 从近期高点回落至少百分之几
    "quiet_min":     [3, 5, 8],             # 横盘持续几分钟
    "quiet_band_pct":[4, 8],                # 横盘期间价格波动带宽(高低差/均价)
    "target_pct":    [15, 30, 50],          # 止盈目标
    "stop_pct":      [10, 15, 20],          # 硬止损
    "max_hold_min":  [15, 45, 120],         # 最长持有
}


def load_pool(f):
    try:
        ol = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not ol or len(ol) < 60:
        return None
    # GT返回最新在前,转成时间正序 [ts,o,h,l,c,vol]
    bars = sorted(ol, key=lambda x: x[0])
    return bars


def simulate(bars, p):
    """在单个池子的K线上模拟。返回该池子产生的交易列表[(pnl_pct, hold_min, reason)]。
    信号逻辑与真实执行约束都在这里。同一池子允许多次入场(视频原话: 割完之后
    它又涨几十,又来一波)。"""
    trades = []
    n = len(bars)
    i = max(p["min_age_min"], 20)  # 币龄不足前不看

    while i < n - 2:
        # ---- 入场条件评估(截至第i根) ----
        window = bars[max(0, i - 240):i + 1]
        lows = [b[3] for b in window if b[3] > 0]
        highs = [b[2] for b in window]
        closes = [b[4] for b in window]
        if not lows or not highs:
            i += 1; continue
        base = min(lows)
        peak = max(highs)
        cur = closes[-1]
        if base <= 0 or cur <= 0:
            i += 1; continue

        pumped = peak / base >= p["pump_mult"]
        pulled_back = cur <= peak * (1 - p["pullback_pct"] / 100)

        # 横盘检测: 最近quiet_min根,高低差/均价 <= quiet_band_pct,且每根都有成交量
        q = p["quiet_min"]
        qbars = bars[i - q + 1:i + 1]
        if len(qbars) < q:
            i += 1; continue
        qhi = max(b[2] for b in qbars); qlo = min(b[3] for b in qbars)
        qmid = (qhi + qlo) / 2
        quiet = qmid > 0 and (qhi - qlo) / qmid * 100 <= p["quiet_band_pct"]
        has_vol = all(b[5] > 0 for b in qbars)

        if not (pumped and pulled_back and quiet and has_vol):
            i += 1; continue

        # ---- 买入执行约束 ----
        entry_bar = bars[i + 1]
        if entry_bar[5] < POS_USD * VOLUME_MULT:
            i += 1; continue          # 量不够,买不进
        prev = bars[i]
        if prev[2] > 0 and (prev[2] - prev[4]) / prev[2] * 100 >= CRASH_BAR_PCT:
            i += 2; continue          # 刚崩过,禁止成交
        entry_price = entry_bar[1] * (1 + SLIP_PCT / 100)   # 下一根开盘价+滑点
        if entry_price <= 0:
            i += 1; continue

        # ---- 持仓模拟 ----
        exit_pnl = None; exit_reason = None; hold = 0
        silent = 0
        crash_lockout = 0
        for j in range(i + 1, min(n, i + 1 + p["max_hold_min"])):
            b = bars[j]
            hold = j - i
            # 死亡检测
            silent = silent + 1 if b[5] <= 0 else 0
            if silent >= DEAD_SILENT_MIN:
                exit_pnl = -100.0; exit_reason = "DEAD"; break
            # 崩盘K线: 本根和下一根禁止成交
            if b[2] > 0 and (b[2] - b[4]) / b[2] * 100 >= CRASH_BAR_PCT:
                crash_lockout = 2
            if crash_lockout > 0:
                crash_lockout -= 1
                continue
            if b[5] < POS_USD * VOLUME_MULT:
                continue              # 这一分钟量不够,卖不出去,只能拿着
            sell_px = lambda px: px * (1 - SLIP_PCT / 100)
            # 止盈: 该根最高价够到目标(用目标价成交,不用最高价占便宜)
            tgt = entry_price * (1 + p["target_pct"] / 100)
            if b[2] >= tgt:
                exit_pnl = (sell_px(tgt) / entry_price - 1) * 100
                exit_reason = "TARGET"; break
            # 止损: 该根最低价击穿止损(按止损价再打滑点,略保守)
            stp = entry_price * (1 - p["stop_pct"] / 100)
            if b[3] <= stp:
                exit_pnl = (sell_px(stp) / entry_price - 1) * 100
                exit_reason = "STOP"; break
        if exit_pnl is None:
            # 超时: 在之后第一根量够的K线按收盘价离场;一直没有量就是DEAD
            done = False
            for j in range(min(n - 1, i + p["max_hold_min"]), min(n, i + p["max_hold_min"] + 30)):
                b = bars[j]
                if b[5] >= POS_USD * VOLUME_MULT:
                    exit_pnl = (b[4] * (1 - SLIP_PCT / 100) / entry_price - 1) * 100
                    exit_reason = "TIMEOUT"; hold = j - i; done = True
                    break
            if not done:
                exit_pnl = -100.0; exit_reason = "DEAD"
        trades.append((exit_pnl, hold, exit_reason))
        i = i + hold + 5   # 离场后歇5分钟再找下一次机会

    return trades


def eval_combo(args):
    combo, files = args
    p = dict(zip(GRID.keys(), combo))
    all_trades = []
    pools_traded = 0
    for f in files:
        bars = load_pool(f)
        if not bars:
            continue
        t = simulate(bars, p)
        if t:
            pools_traded += 1
            all_trades.extend(t)
    if not all_trades:
        return {**p, "n_trades": 0}
    pnls = [t[0] for t in all_trades]
    wins = [x for x in pnls if x > 0]
    # 2026-07-31修复: 原来按"每笔押全部本金"算复利,只要有一笔死亡(-100%)整个
    # 复利就归零,所有组合的compound_x都是0,这个指标完全没有区分度也不符合
    # 实际——访谈里的做法是单笔只押总资产1-3%(原话"百来万拿个最多几万块")。
    # 改成按POSITION_FRAC分数仓位滚动: 每笔盈亏只作用于这一小部分本金,
    # 这才是"一笔亏光不会伤筋动骨"的真实数学。
    POSITION_FRAC = 0.02
    bankroll = 1.0
    for x in pnls:
        bankroll *= (1 + POSITION_FRAC * max(x, -100) / 100)
    total_return = bankroll
    n = len(pnls)
    mean = sum(pnls) / n
    pnls_sorted = sorted(pnls)
    return {
        **p, "n_trades": n, "pools_traded": pools_traded,
        "mean_pnl": round(mean, 2),
        "median_pnl": round(pnls_sorted[n // 2], 2),
        "win_rate": round(100 * len(wins) / n, 1),
        "p10": round(pnls_sorted[max(0, n // 10)], 2),
        "dead_rate": round(100 * sum(1 for t in all_trades if t[2] == "DEAD") / n, 1),
        "compound_x": round(total_return, 3),
        "avg_hold_min": round(sum(t[1] for t in all_trades) / n, 1),
    }


def main():
    files = sorted(OHLCV_DIR.glob("*.json"))
    usable = [f for f in files if f.stat().st_size > 200]
    print(f"可用池子数据: {len(usable)}/{len(files)}")
    combos = list(itertools.product(*GRID.values()))
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(combos)
    combos = combos[:limit]
    print(f"参数组合: {len(combos)}个,开始扫描...")

    rows = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        for k, res in enumerate(ex.map(eval_combo, [(c, usable) for c in combos], chunksize=8)):
            rows.append(res)
            if (k + 1) % 200 == 0:
                print(f"  {k + 1}/{len(combos)}")

    rows = [r for r in rows if r.get("n_trades", 0) > 0]
    if not rows:
        print("没有任何组合产生交易")
        return
    fields = list(GRID.keys()) + ["n_trades", "pools_traded", "mean_pnl", "median_pnl",
                                   "win_rate", "p10", "dead_rate", "compound_x", "avg_hold_min"]
    with RESULTS_F.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(rows)
    print(f"写出 {len(rows)} 行 -> {RESULTS_F.name}")
    # 快速预览: 按复利倍数排序的前10
    rows.sort(key=lambda r: -r.get("compound_x", 0))
    print("\n复利倍数Top10:")
    for r in rows[:10]:
        print(f"  compound={r['compound_x']:8.3f}  n={r['n_trades']:4d}  均值={r['mean_pnl']:+6.2f}%  "
              f"胜率={r['win_rate']:5.1f}%  死亡率={r['dead_rate']:4.1f}%  "
              f"age>={r['min_age_min']}m pump>={r['pump_mult']}x 回落{r['pullback_pct']}% "
              f"横盘{r['quiet_min']}m/{r['quiet_band_pct']}% 止盈{r['target_pct']}% 止损{r['stop_pct']}% 持有<={r['max_hold_min']}m")


if __name__ == "__main__":
    main()
