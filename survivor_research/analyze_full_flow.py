# -*- coding: utf-8 -*-
"""2026-07-31新建: 用全量链上流水回答"那493 SOL到底是谁的钱"。

用户的质疑(我认为是对的): 前期不可能钓到那么多鱼,如果真钓到了狗庄早就
收网了。所以砸盘时提走的493 SOL里,很可能大部分是狗庄自己对倒打进去的钱
在回流,真实利润远小于$36K。

只有全生命周期的逐笔流水能分清楚。这个脚本算三件事:

  1. 池子SOL储备曲线 —— 钱是什么时候进来的?是一路涨还是最后突然冲进来?
     如果撒饵阶段储备一直很低,说明前期根本没鱼,拉盘全靠自买自卖。

  2. 每个钱包的全周期净SOL —— 不看单个窗口,看它从生到死一共是净投入还是
     净提走。狗庄用多钱包倒手也没用,只要把这批钱包加总,自己转给自己的部分
     会互相抵消。

  3. 那12个提款钱包的完整历史 —— 它们在砸盘前买过没有?买了多少?
     这是判定"回流自己的钱"还是"真赚鱼的钱"的直接证据。

用法: python analyze_full_flow.py <txs_xxx.jsonl>
"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
SOL_USD = 75.0

# 砸盘时提走钱的12个钱包(来自链上核对,已验证真实收到SOL)
EXTRACTORS = ["3XzoYtrM8m", "3hkE5nU1zR", "665ka25gw", "ATDqes3AQ3", "J6bPtj1xED",
              "7rfUUPBQcj", "3FV3Wn9Wdk", "GUByKNpuEm", "8vsK8dePPx", "3kkhsLjh5j",
              "NqchCBxMER", "21rgNb1EhW"]


def hhmm(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")


def main():
    f = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "txs_BFBM1Nqj.jsonl"
    txs = []
    with f.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                t = json.loads(line)
                if t.get("ts"):
                    txs.append(t)
            except json.JSONDecodeError:
                pass
    txs.sort(key=lambda x: x["ts"])
    print(f"全量交易: {len(txs)}笔  {hhmm(txs[0]['ts'])} -> {hhmm(txs[-1]['ts'])}"
          f"  ({(txs[-1]['ts']-txs[0]['ts'])/60:.0f}分钟)\n")

    # ---------- 找出池子的SOL金库账户 ----------
    # 特征: 出现在最多交易里,且SOL变化累计绝对值最大
    freq, mag = defaultdict(int), defaultdict(float)
    for t in txs:
        for acc, d in t["sol"].items():
            freq[acc] += 1
            mag[acc] += abs(d)
    vault = max(mag, key=lambda a: mag[a] if freq[a] > len(txs) * 0.5 else 0)
    print(f"池子金库账户: {vault[:16]}..  (出现在{freq[vault]}笔交易, 累计流量{mag[vault]:,.0f} SOL)\n")

    # ---------- 1. 池子SOL储备曲线 ----------
    print("=" * 70)
    print("1. 池子SOL储备曲线 —— 钱是什么时候进来的")
    print("=" * 70)
    cum, curve = 0.0, []
    for t in txs:
        cum += t["sol"].get(vault, 0.0)
        curve.append((t["ts"], cum))
    # 按10分钟分桶打印
    t0 = curve[0][0]
    buckets = {}
    for ts, c in curve:
        b = int((ts - t0) / 600)
        buckets[b] = c                      # 每桶保留最后一个值
    peak_b, peak_v = max(buckets.items(), key=lambda x: x[1])
    print(f"{'时刻':>6} {'池内SOL':>10} {'折USD':>10}  储备走势")
    for b in sorted(buckets):
        v = buckets[b]
        bar = "#" * int(max(v, 0) / max(peak_v, 1) * 40)
        print(f"{b*10:>4}分 {v:>10.1f} {v*SOL_USD:>10,.0f}  {bar}")
    print(f"\n  峰值储备: {peak_v:.1f} SOL (${peak_v*SOL_USD:,.0f})  出现在第{peak_b*10}分钟")

    # ---------- 2. 每个钱包全周期净SOL ----------
    print()
    print("=" * 70)
    print("2. 每个钱包的全周期净SOL(正=净提走, 负=净投入)")
    print("=" * 70)
    net = defaultdict(float)
    nbuy, nsell = defaultdict(int), defaultdict(int)
    for t in txs:
        s = t.get("signer")
        if not s or s == vault:
            continue
        d = t["sol"].get(s, 0.0) + t.get("fee", 0.0)   # 加回手续费,只看交易本身
        net[s] += d
        if d < 0:
            nbuy[s] += 1
        elif d > 0:
            nsell[s] += 1

    win = sorted([(v, k) for k, v in net.items() if v > 0.001], reverse=True)
    lose = sorted([(v, k) for k, v in net.items() if v < -0.001])
    tot_win = sum(v for v, _ in win)
    tot_lose = -sum(v for v, _ in lose)
    print(f"  赚钱的钱包: {len(win):>5}个  净提走 {tot_win:>9.1f} SOL  (${tot_win*SOL_USD:>10,.0f})")
    print(f"  亏钱的钱包: {len(lose):>5}个  净投入 {tot_lose:>9.1f} SOL  (${tot_lose*SOL_USD:>10,.0f})")
    print(f"  差额(手续费/还没卖的): {tot_lose-tot_win:>9.1f} SOL")

    print(f"\n  提走最多的15个:")
    print(f"  {'钱包':<14}{'净SOL':>10}{'折USD':>11}{'买':>5}{'卖':>5}  {'首次':>6}{'末次':>7}")
    first_seen, last_seen = {}, {}
    for t in txs:
        s = t.get("signer")
        if s:
            first_seen.setdefault(s, t["ts"])
            last_seen[s] = t["ts"]
    for v, k in win[:15]:
        mark = "  <-- 砸盘提款钱包" if any(k.startswith(e) for e in EXTRACTORS) else ""
        print(f"  {k[:12]:<14}{v:>10.2f}{v*SOL_USD:>11,.0f}{nbuy[k]:>5}{nsell[k]:>5}"
              f"  {hhmm(first_seen[k]):>6}{hhmm(last_seen[k]):>7}{mark}")

    # ---------- 3. 那12个提款钱包的完整账 ----------
    print()
    print("=" * 70)
    print("3. 砸盘提款的12个钱包 —— 它们之前投进去多少?")
    print("=" * 70)
    print(f"  {'钱包':<14}{'投入SOL':>10}{'提走SOL':>10}{'净赚SOL':>10}{'净赚USD':>11}  {'首次买入':>9}")
    g_in = g_out = 0.0
    for e in EXTRACTORS:
        cand = [k for k in net if k.startswith(e)]
        if not cand:
            print(f"  {e:<14}{'(全量数据里没找到)':>30}")
            continue
        k = cand[0]
        inn = sum(-(t["sol"].get(k, 0) + t.get("fee", 0))
                  for t in txs if t.get("signer") == k and (t["sol"].get(k, 0) + t.get("fee", 0)) < 0)
        out = sum((t["sol"].get(k, 0) + t.get("fee", 0))
                  for t in txs if t.get("signer") == k and (t["sol"].get(k, 0) + t.get("fee", 0)) > 0)
        g_in += inn; g_out += out
        print(f"  {k[:12]:<14}{inn:>10.2f}{out:>10.2f}{out-inn:>10.2f}{(out-inn)*SOL_USD:>11,.0f}"
              f"  {hhmm(first_seen[k]):>9}")
    print(f"  {'-'*66}")
    print(f"  {'合计':<14}{g_in:>10.2f}{g_out:>10.2f}{g_out-g_in:>10.2f}{(g_out-g_in)*SOL_USD:>11,.0f}")

    print()
    print("=" * 70)
    print("结论")
    print("=" * 70)
    print(f"  这12个钱包一共投入 {g_in:.1f} SOL (${g_in*SOL_USD:,.0f})")
    print(f"  一共提走           {g_out:.1f} SOL (${g_out*SOL_USD:,.0f})")
    print(f"  净赚               {g_out-g_in:.1f} SOL (${(g_out-g_in)*SOL_USD:,.0f})")
    if g_in > 0:
        print(f"  自有资金占提款额的 {g_in/max(g_out,0.001)*100:.0f}%  <- 这个比例越高,说明越多是自己的钱在回流")


if __name__ == "__main__":
    main()
