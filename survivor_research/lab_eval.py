# -*- coding: utf-8 -*-
"""策略验收报告。2026-07-31建。

回答三个问题,全部用采到的真实数据,不用假设:

  1. **这套策略能不能执行** —— 危险度信号在砸盘之前多久亮起来?
     如果提前量的中位数小于一个扫描周期,信号再准也来不及跑,策略作废。

  2. **离场线该设多少** —— 收网时刻的危险度分布是多少?
     USOH那个样本是0.41,现在暂定0.30。样本够了就用分位数重新定。

  3. **成功失败比率** —— 三种结局各占多少:
       跑掉了   信号提前亮,我们能出
       被砸了   信号没亮或亮得太晚
       假警报   信号亮了但他一直没砸,我们白跑一趟,损失了后续涨幅

    只统计前两种会得出虚高的胜率。假警报的代价必须计入。

用法: python lab_eval.py
"""
import statistics as st
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import lab_db as db      # noqa: E402
import lab_dump as ld    # noqa: E402

EXIT = 0.30


def pct(a, b):
    return f"{a/b*100:.0f}%" if b else "-"


def q(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    i = max(0, min(len(vals) - 1, int(round(p * (len(vals) - 1)))))
    return vals[i]


def show(title):
    print()
    print("=" * 74)
    print(f"  {title}")
    print("=" * 74)


def main():
    c = db.conn()
    c.executescript(ld.DDL)
    c.commit()
    n_fa = ld.sweep_false_alarms(c, EXIT)

    dumps = c.execute("SELECT * FROM dump_events").fetchall()
    alarms = c.execute("SELECT * FROM false_alarms").fetchall()

    show("样本量")
    print(f"  已记录收网事件   {len(dumps)} 次")
    print(f"  假警报(越线未砸) {len(alarms)} 个  (本次新增 {n_fa})")
    if len(dumps) < 5:
        print()
        print("  样本太少,下面的数字只能看趋势不能下结论。")
        print("  收网事件要攒到 30 次以上,分位数才稳定。")
    if not dumps and not alarms:
        print("\n  还没有数据。守望进程需要再跑一段时间。")
        return

    # ---- 1. 能不能执行: 信号提前量 ----
    if dumps:
        show("1. 信号提前量 —— 这套策略能不能执行")
        leads = [d["lead_sec"] for d in dumps if d["lead_sec"] is not None]
        esc = sum(1 for d in dumps if d["escaped"])
        print(f"  砸盘前信号就亮了: {esc}/{len(dumps)}  ({pct(esc, len(dumps))})")
        if leads:
            pos = [x for x in leads if x > 0]
            print(f"  提前量(秒): 中位 {q(pos,0.5) or 0:,.0f}   "
                  f"25分位 {q(pos,0.25) or 0:,.0f}   75分位 {q(pos,0.75) or 0:,.0f}")
            if pos:
                print(f"            = 中位提前 {(q(pos,0.5) or 0)/60:.1f} 分钟")
            print(f"  扫描周期 180 秒。提前量小于这个数就等于来不及。")
            enough = sum(1 for x in pos if x > 180)
            print(f"  提前量够一个扫描周期的: {enough}/{len(dumps)}  ({pct(enough, len(dumps))})")
        secs = [d["dump_sec"] for d in dumps if d["dump_sec"]]
        if secs:
            print(f"\n  砸盘本身持续: 中位 {q(secs,0.5):,.0f} 秒  最快 {min(secs):,.0f} 秒")
            print(f"  (砸盘一旦开始,这就是全部反应时间——基本等于跑不掉)")

        # ---- 2. 离场线校准 ----
        show("2. 收网时刻的危险度 —— 离场线该设多少")
        dg = [d["danger_at_dump"] for d in dumps if d["danger_at_dump"] is not None]
        if dg:
            print(f"  中位 {q(dg,0.5):.2f}   10分位 {q(dg,0.10):.2f}   "
                  f"25分位 {q(dg,0.25):.2f}   最低 {min(dg):.2f}")
            print(f"  当前离场线 {EXIT:.2f}")
            below = sum(1 for x in dg if x < EXIT)
            print(f"  收网时危险度**低于**离场线的: {below}/{len(dg)} ({pct(below,len(dg))})"
                  f"  <- 这些是信号根本来不及亮的")
            if len(dg) >= 10:
                sug = q(dg, 0.10)
                print(f"\n  建议离场线 -> {sug:.2f} (10分位,能覆盖90%的收网)")

        # ---- 3. 触发阈值: 多大的买单会招来收网 ----
        show("3. 多大的买单会招来收网 —— 安全仓位上限")
        trg = [(d["trigger_buy_usd"], d["op_cost_usd"]) for d in dumps
               if d["trigger_buy_usd"] and d["op_cost_usd"]]
        if trg:
            ratios = [t / o for t, o in trg if o > 0]
            print(f"  触发买单/他的成本: 中位 {q(ratios,0.5):.3f}   "
                  f"10分位 {q(ratios,0.10):.3f}")
            print(f"  即: 仓位小于他成本的 {q(ratios,0.10)*100:.1f}% 时,历史上90%没触发收网")
        ign = [d["max_fish_ignored"] for d in dumps if d["max_fish_ignored"]]
        if ign:
            print(f"  他明确没理会的最大买单: 中位 ${q(ign,0.5):,.0f}  最大 ${max(ign):,.0f}")
        oc = [d["op_cost_usd"] for d in dumps if d["op_cost_usd"]]
        if oc:
            print(f"\n  收网时他的成本: 中位 ${q(oc,0.5):,.0f}   "
                  f"最低 ${min(oc):,.0f}   最高 ${max(oc):,.0f}")
            cheap = sum(1 for x in oc if x < 50)
            print(f"  成本低于$50就砸的: {cheap}/{len(oc)} ({pct(cheap,len(oc))})"
                  f"  <- 进场门槛设$50挡掉的就是这些")

        show("4. 没跑掉的话亏多少")
        cr = [d["crash_pct"] for d in dumps if d["crash_pct"] is not None]
        if cr:
            print(f"  从峰值到砸盘后: 中位 {q(cr,0.5):+.1f}%   最惨 {min(cr):+.1f}%")
        lm = [d["life_min"] for d in dumps if d["life_min"]]
        if lm:
            print(f"  开盘到收网: 中位 {q(lm,0.5):.0f} 分钟 ({q(lm,0.5)/60:.1f} 小时)")

    # ---- 5. 假警报的代价 ----
    if alarms:
        show("5. 假警报的代价 —— 早跑放弃了多少")
        miss = [a["missed_gain_pct"] for a in alarms if a["missed_gain_pct"] is not None]
        if miss:
            print(f"  越线后价格还涨了: 中位 {q(miss,0.5):+.1f}%   "
                  f"最多 {max(miss):+.1f}%")
            good = sum(1 for x in miss if x > 5)
            print(f"  其中还涨超5%的: {good}/{len(miss)} ({pct(good,len(miss))})"
                  f"  <- 这些是早跑亏掉的机会")

    # ---- 6. 总结 ----
    show("6. 三种结局的比率")
    esc = sum(1 for d in dumps if d["escaped"])
    caught = len(dumps) - esc
    tot = len(dumps) + len(alarms)
    if tot:
        print(f"  跑掉了(信号提前亮)   {esc:>4}   {pct(esc, tot)}")
        print(f"  被砸了(信号没来得及) {caught:>4}   {pct(caught, tot)}")
        print(f"  假警报(白跑一趟)     {len(alarms):>4}   {pct(len(alarms), tot)}")

    # ---- 纸盘实际战绩 ----
    pt = c.execute("SELECT pnl_pct, pnl_pct_raw, exit_reason FROM paper_trades "
                   "WHERE exit_ts IS NOT NULL AND pnl_pct IS NOT NULL").fetchall()
    if pt:
        show("7. 纸盘实际战绩(已扣池深滑点)")
        v = [r["pnl_pct"] for r in pt]
        raw = [r["pnl_pct_raw"] for r in pt if r["pnl_pct_raw"] is not None]
        win = sum(1 for x in v if x > 0)
        print(f"  {len(v)} 笔   胜率 {pct(win, len(v))}   "
              f"均值 {st.mean(v):+.1f}%   中位 {q(v,0.5):+.1f}%")
        if raw:
            print(f"  名义收益均值 {st.mean(raw):+.1f}%  ->  扣滑点后 {st.mean(v):+.1f}%"
                  f"   (差 {st.mean(v)-st.mean(raw):+.1f} 个百分点)")
        print("\n  按离场原因:")
        for r in c.execute("SELECT exit_reason, COUNT(*) n, AVG(pnl_pct) a "
                           "FROM paper_trades WHERE exit_ts IS NOT NULL "
                           "AND pnl_pct IS NOT NULL GROUP BY exit_reason "
                           "ORDER BY n DESC"):
            print(f"    {r['exit_reason']:<28} {r['n']:>3}笔  均值 {r['a']:+.1f}%")


if __name__ == "__main__":
    main()
