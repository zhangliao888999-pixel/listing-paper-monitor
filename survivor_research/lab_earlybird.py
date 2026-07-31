# -*- coding: utf-8 -*-
"""按进场顺位统计结局。2026-07-31建。

验证用户的假设:
  "发币后第一笔买入是安全的,即使发币钱包马上卖出,池子流动性还在"
  "狗庄把价格炒上去后,第二笔买入的仓位也能跑掉"

这个假设可以直接判: 把每个池子里的买家按**第一次买入的时间**排序,算出
第1个买家、第2个、第3个……各自最终是赚是亏、有没有真的卖出去过。如果
"前几名进场"真有优势,那么按顺位分组的收益应该单调递减。

两个必须注意的口径问题:

  1. **按"谁的SOL真的动了"归属,不按谁签名。** $GATE 那个池子里,机器人
     签了193笔只付gas,钱全走另一个账户 —— 按签名归属会算出"成本$0"。

  2. **没卖出去的仓位要按0计,不能按最后价格估值。** 上一轮纸盘就是败在
     "按最后已知价结算",给流动性归零的币算出正收益。这里只认真金白银:
     净收益 = 实际收到的SOL - 实际付出的SOL,还拿在手里的一律不计价。
     这样算出来偏保守,但保守的方向是对的 —— 卖不掉的币确实一文不值。

用法:
  python lab_earlybird.py            用库里已有的池子
  python lab_earlybird.py <pool>...  指定池子
"""
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import lab_db as db          # noqa: E402
import lab_forensics as fx   # noqa: E402

SOL_USD = 75.0
GAS_FLOOR = 1e-7          # 小于这个量级的SOL变动当gas忽略
MAX_TX = 3000             # 单个池子最多分析这么多笔
BUCKETS = [(1, 1, "第1个"), (2, 2, "第2个"), (3, 3, "第3个"),
           (4, 5, "第4-5"), (6, 10, "第6-10"), (11, 20, "第11-20"),
           (21, 50, "第21-50"), (51, 10 ** 9, "第51+")]


def analyze_pool(pool):
    """返回 [(顺位, 投入USD, 取出USD, 净USD, 是否卖出过)]。"""
    sigs = [s for s in fx.get_signatures(pool, cap=MAX_TX + 100)
            if not s["err"] and s.get("ts")]
    if len(sigs) < 8 or len(sigs) > MAX_TX:
        return None, f"交易数{len(sigs)}不适合"
    txs = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for t in ex.map(fx.parse_tx, sigs):
            if t:
                txs.append(t)
    if len(txs) < len(sigs) * 0.9:
        return None, f"覆盖率{len(txs)/max(len(sigs),1):.0%}不足"
    txs.sort(key=lambda x: x["ts"])

    # 按"谁的SOL真的动了"归属。池子账户本身排除。
    first_buy, inn, out = {}, defaultdict(float), defaultdict(float)
    for t in txs:
        for acc, d in t["sol_delta"].items():
            if acc == pool or abs(d) < GAS_FLOOR:
                continue
            if d < 0:
                inn[acc] += -d
                first_buy.setdefault(acc, t["ts"])
            else:
                out[acc] += d
    if not first_buy:
        return None, "没有买入"
    order = sorted(first_buy, key=lambda a: first_buy[a])
    rows = []
    for i, a in enumerate(order, 1):
        i_usd, o_usd = inn[a] * SOL_USD, out[a] * SOL_USD
        if i_usd < 0.5:            # 太小的当噪音(关户退租金之类)
            continue
        rows.append((i, i_usd, o_usd, o_usd - i_usd, o_usd > 0.01))
    return rows, None


def main():
    pools = [a for a in sys.argv[1:] if not a.startswith("-")
             and not a.endswith(".txt")]
    files = [a for a in sys.argv[1:] if a.endswith(".txt")]
    for f in files:
        pools += [l.strip() for l in Path(f).read_text().splitlines()
                  if len(l.strip()) > 30]
    if not pools:
        c = db.conn()
        pools = [r["pool"] for r in c.execute(
            "SELECT DISTINCT pool FROM watchlist ORDER BY added_at DESC LIMIT 60")]
    pools = list(dict.fromkeys(pools))
    print(f"待分析池子 {len(pools)} 个\n")

    agg = defaultdict(lambda: {"n": 0, "win": 0, "sold": 0, "net": [], "inn": []})
    ok = bad = 0
    for i, p in enumerate(pools, 1):
        rows, err = analyze_pool(p)
        if rows is None:
            bad += 1
            print(f"  [{i}/{len(pools)}] {p[:10]}.. 跳过: {err}", flush=True)
            continue
        ok += 1
        for rank, i_usd, o_usd, net, sold in rows:
            for lo, hi, name in BUCKETS:
                if lo <= rank <= hi:
                    b = agg[name]
                    b["n"] += 1
                    b["win"] += 1 if net > 0 else 0
                    b["sold"] += 1 if sold else 0
                    b["net"].append(net / max(i_usd, 0.01) * 100)
                    b["inn"].append(i_usd)
                    break
        print(f"  [{i}/{len(pools)}] {p[:10]}.. {len(rows)}个买家", flush=True)
        # 增量落盘: 276个池子要跑很久,中途断了不用从头再来
        with open(HERE / "earlybird_rows.csv", "a", encoding="utf-8") as fh:
            for rank, i_usd, o_usd, net, sold in rows:
                fh.write(f"{p},{rank},{i_usd:.4f},{o_usd:.4f},"
                         f"{net:.4f},{int(sold)}\n")

    print(f"\n有效池子 {ok} 个,跳过 {bad} 个")
    if not agg:
        print("没有可用数据")
        return

    def q(v, p):
        v = sorted(v)
        return v[max(0, min(len(v) - 1, int(round(p * (len(v) - 1)))))] if v else 0

    print()
    print("=" * 78)
    print("  按进场顺位看结局  (收益率 = 净收益/投入, 没卖出的按0计价)")
    print("=" * 78)
    print(f"  {'顺位':<10}{'样本':>6}{'卖出过':>8}{'赚钱':>7}{'中位收益':>10}"
          f"{'均值':>9}{'25分位':>9}{'75分位':>9}{'中位投入':>10}")
    for lo, hi, name in BUCKETS:
        b = agg.get(name)
        if not b or not b["n"]:
            continue
        n = b["n"]
        print(f"  {name:<10}{n:>6}{b['sold']/n:>7.0%}{b['win']/n:>7.0%}"
              f"{q(b['net'],0.5):>9.0f}%{sum(b['net'])/n:>8.0f}%"
              f"{q(b['net'],0.25):>8.0f}%{q(b['net'],0.75):>8.0f}%"
              f"{'$'+format(q(b['inn'],0.5),',.0f'):>10}")
    print()
    print("  '卖出过' = 真的把币换回SOL过。这一列低说明大部分人根本卖不掉。")
    print("  '赚钱'   = 拿回来的SOL多于投进去的。没卖出的一律算亏光。")


if __name__ == "__main__":
    main()
