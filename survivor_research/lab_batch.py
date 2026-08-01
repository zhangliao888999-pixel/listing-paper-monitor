# -*- coding: utf-8 -*-
"""批量出标准尸检报告。2026-08-01建。

把已采集的币全部按 lab_report 的规格跑一遍,进 coin_report/coin_wallets/
coin_timeline 三张表,然后出跨币汇总。

两个执行上的考虑:
  - **按交易量从小到大排**。一个3.6万笔的币要80分钟,而几百笔的只要1分钟。
    先跑小的,单位时间内出的样本最多,趋势能更早看出来。
  - **串行跑**。之前并行跑两个批量任务,Helius的10请求/秒被抢,大量池子
    解析覆盖率只有83%被闸门拒绝 —— 慢一点但数据是对的。

用法:
  python lab_batch.py <清单.txt> [--max-tx N] [--dead-only]
  python lab_batch.py --summary          只出跨币汇总
"""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import lab_db as db          # noqa: E402
import lab_forensics as fx   # noqa: E402
import lab_registry as reg   # noqa: E402
import lab_report as rp      # noqa: E402


def money(v):
    return "$" + format(v or 0, ",.0f")


def q(vals, p):
    v = sorted(x for x in vals if x is not None)
    return v[max(0, min(len(v) - 1, int(round(p * (len(v) - 1)))))] if v else 0


def summary():
    c = db.conn()
    rows = c.execute("SELECT * FROM coin_report").fetchall()
    if not rows:
        print("库里还没有报告")
        return
    print("=" * 96)
    print("  全币盈亏汇总   共 %d 个币" % len(rows))
    print("=" * 96)
    print("  %-18s%7s%8s%11s%11s%11s%9s%8s%9s"
          % ("币", "存活h", "钱包", "狗庄成本", "鱼投入", "狗庄盈亏",
             "逃脱率", "质量", "结局"))
    for r in sorted(rows, key=lambda x: -(x["op_pnl_usd"] or 0)):
        esc = (r["fish_escaped_n"] / r["fish_n"] * 100) if r["fish_n"] else 0
        print("  %-18s%7.1f%8d%11s%11s%11s%8.0f%%%8s%9s"
              % (str(r["name"])[:16], (r["life_min"] or 0) / 60, r["n_wallet"],
                 money(r["op_cost_usd"]), money(r["fish_in_usd"]),
                 money(r["op_pnl_usd"]), esc, r["data_quality"] or "?",
                 r["outcome"] or "?"))

    good = [r for r in rows if r["attribution_ok"]]
    win = [r for r in good if (r["op_pnl_usd"] or 0) > 0]
    lose = [r for r in good if (r["op_pnl_usd"] or 0) <= 0]
    print("")
    print("=" * 96)
    print("  赚钱的狗庄 vs 亏钱的狗庄   (只统计归属可信的 %d 个)" % len(good))
    print("=" * 96)
    print("  %-14s%8s%13s%13s%13s%12s%11s"
          % ("", "样本", "狗庄成本中位", "鱼投入中位", "撒饵速度", "存活中位h", "钱包数中位"))
    for lbl, grp in (("赚钱", win), ("亏钱", lose)):
        if not grp:
            continue
        print("  %-14s%8d%13s%13s%13s%12.1f%11.0f"
              % (lbl, len(grp),
                 money(q([x["op_cost_usd"] for x in grp], 0.5)),
                 money(q([x["fish_in_usd"] for x in grp], 0.5)),
                 money(q([x["ratchet_usd_min"] for x in grp], 0.5)) + "/分",
                 q([(x["life_min"] or 0) / 60 for x in grp], 0.5),
                 q([x["n_wallet"] for x in grp], 0.5)))

    tot_op = sum(x["op_pnl_usd"] or 0 for x in good)
    tot_fish_in = sum(x["fish_in_usd"] or 0 for x in good)
    tot_trapped = sum(x["fish_trapped_usd"] or 0 for x in good)
    n_fish = sum(x["fish_n"] or 0 for x in good)
    n_esc = sum(x["fish_escaped_n"] or 0 for x in good)
    print("")
    print("  全样本合计:")
    print("    狗庄净盈亏   %s" % money(tot_op))
    print("    鱼投入       %s" % money(tot_fish_in))
    print("    鱼被套住     %s" % money(tot_trapped))
    print("    买家 %d 个,逃出来 %d 个 (%.0f%%)"
          % (n_fish, n_esc, n_esc / max(n_fish, 1) * 100))
    print("    赚钱的狗庄 %d/%d = %.0f%%"
          % (len(win), len(good), len(win) / max(len(good), 1) * 100))

    fm = [x["fish_first_min"] for x in good if x["fish_first_min"] is not None]
    if fm:
        print("")
        print("  第一条鱼上钩的时间: 中位第%.0f分钟  25分位第%.0f分  75分位第%.0f分"
              % (q(fm, 0.5), q(fm, 0.25), q(fm, 0.75)))
    dg = [x["danger_at_dump"] for x in good if x["danger_at_dump"] is not None]
    if dg:
        print("  收网时危险度: 中位%.2f  10分位%.2f  90分位%.2f"
              % (q(dg, 0.5), q(dg, 0.10), q(dg, 0.90)))


def main():
    args = sys.argv[1:]
    rp.init()
    reg.init()
    if "--summary" in args:
        summary()
        return
    max_tx = 4000
    if "--max-tx" in args:
        max_tx = int(args[args.index("--max-tx") + 1])
    pools = []
    for f in [a for a in args if a.endswith(".txt")]:
        pools += [l.strip() for l in Path(f).read_text().splitlines()
                  if len(l.strip()) > 30]
    if not pools:
        c = db.conn()
        pools = [r["pool"] for r in c.execute("SELECT DISTINCT pool FROM snapshots")]
    pools = list(dict.fromkeys(pools))
    c = db.conn()
    done = {r["pool"] for r in c.execute("SELECT pool FROM coin_report")}
    pools = [p for p in pools if p not in done]
    print("待分析 %d 个 (已完成 %d)" % (len(pools), len(done)), flush=True)

    # 先量一遍规模,小的先跑
    print("量规模中...", flush=True)
    sized = []
    for i, p in enumerate(pools, 1):
        try:
            n = len(fx.get_signatures(p, cap=max_tx + 200))
        except Exception as e:
            print("  %s 量不了: %s" % (p[:10], type(e).__name__), flush=True)
            continue
        if 10 <= n <= max_tx:
            sized.append((n, p))
        if i % 40 == 0:
            print("  %d/%d  可用%d个" % (i, len(pools), len(sized)), flush=True)
    sized.sort()
    print("可分析 %d 个,按交易量从小到大跑" % len(sized), flush=True)

    ok = bad = 0
    t0 = time.time()
    for i, (n, p) in enumerate(sized, 1):
        try:
            rep, wallets, timeline = rp.analyze_coin(p, max_sigs=max_tx + 200)
        except Exception as e:
            bad += 1
            print("  [%d/%d] %s.. 出错 %s" % (i, len(sized), p[:10], type(e).__name__),
                  flush=True)
            continue
        if not rep:
            bad += 1
            print("  [%d/%d] %s.. 引擎拒绝" % (i, len(sized), p[:10]), flush=True)
            continue
        rp.save(rep, wallets, timeline)
        ok += 1
        print("  [%d/%d] %-16s %d笔 狗庄%s 鱼%s 盈亏%s %s (%.0f分钟)"
              % (i, len(sized), str(rep["name"])[:14], rep["n_tx"],
                 money(rep["op_cost_usd"]), money(rep["fish_in_usd"]),
                 money(rep["op_pnl_usd"]), rep["outcome"],
                 (time.time() - t0) / 60), flush=True)
    print("")
    print("完成 %d 个,失败 %d 个" % (ok, bad))
    summary()


if __name__ == "__main__":
    main()
