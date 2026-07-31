# -*- coding: utf-8 -*-
"""狗庄实验室 - 实时状态面板。2026-07-31建。

配合 lab_monitor.ps1 在VPS上开一个自动刷新的窗口,一眼看清:
  - 守望进程还活着吗? 卡住了没有?(靠心跳文件的时间戳判断,光看进程在不在
    不够——进程可能活着但卡在某个池子的RPC上不动)
  - 现在盯着哪些狗庄,他们各自沉了多少钱、钓到多少鱼、危险度多少
  - 纸盘持仓的实时盈亏和距离离场线还有多远
  - API额度烧到哪了

用法: python lab_monitor.py        打印一次
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import lab_db as db          # noqa: E402
import lab_forensics as fx   # noqa: E402

C = {"r": "\033[91m", "g": "\033[92m", "y": "\033[93m", "c": "\033[96m",
     "w": "\033[97m", "d": "\033[90m", "0": "\033[0m", "b": "\033[1m"}


def col(t, c):
    return f"{C[c]}{t}{C['0']}"


def ago(ts):
    if not ts:
        return "从未"
    s = time.time() - ts
    if s < 90:
        return f"{s:.0f}秒前"
    if s < 5400:
        return f"{s/60:.0f}分钟前"
    return f"{s/3600:.1f}小时前"


def main():
    print(col("=" * 78, "c"))
    print(col(f"  狗庄研究实验室   刷新: {datetime.now():%Y-%m-%d %H:%M:%S}", "b"))
    print(col("=" * 78, "c"))

    # ---- 心跳 ----
    hb_f = HERE / ".lab_heartbeat.json"
    hb = {}
    try:
        hb = json.loads(hb_f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    age = time.time() - hb.get("ts", 0) if hb else 9e9
    if not hb:
        state = col("没有心跳文件 —— 守望进程从未启动过", "r")
    elif age < 400:
        state = col(f"运行中 (PID {hb.get('pid')})", "g")
    elif age < 900:
        state = col(f"心跳偏慢 {ago(hb.get('ts'))} —— 可能卡在某个池子", "y")
    else:
        state = col(f"心跳停了 {ago(hb.get('ts'))} —— 进程可能已死", "r")
    print(f"\n{col('守望进程', 'w')}: {state}")
    if hb:
        stg = hb.get("stage", "?")
        if stg == "扫描中":
            print(f"  当前: 扫描第 {hb.get('i')}/{hb.get('n')} 个  →  {col(str(hb.get('pool')), 'c')}")
        else:
            print(f"  当前: {stg}  观察{hb.get('watching')}个  "
                  f"持仓{hb.get('open')}  已平{hb.get('done')}")
        print(f"  心跳: {ago(hb.get('ts'))}")
        print(f"  Helius额度: {hb.get('helius', fx.usage_report())}")

    c = db.conn()

    # ---- 观察名单 ----
    print(f"\n{col('--- 正在盯的狗庄 ---', 'w')}")
    rows = c.execute("""
        SELECT s.* FROM snapshots s
        JOIN (SELECT pool, MAX(ts) m FROM snapshots GROUP BY pool) x
          ON s.pool = x.pool AND s.ts = x.m
        JOIN watchlist w ON w.pool = s.pool AND w.dropped_at IS NULL
        ORDER BY s.op_cost_usd DESC LIMIT 14""").fetchall()
    if not rows:
        print(col("  (还没有快照)", "d"))
    else:
        print(col(f"  {'币':<16}{'交易':>6}{'钱包':>5}{'集中':>6}"
                  f"{'狗庄成本':>11}{'鱼的钱':>10}{'危险度':>8}  更新", "d"))
        for r in rows:
            nm = (c.execute("SELECT name FROM watchlist WHERE pool=?",
                            (r["pool"],)).fetchone() or {"name": None})["name"]
            nm = (nm or r["pool"][:10])[:15]
            dg = r["danger"] or 0
            dc = "r" if dg >= 0.30 else ("y" if dg >= 0.15 else "g")
            oc = r["op_cost_usd"] or 0
            print(f"  {nm:<16}{r['n_tx']:>6}{r['n_wallet']:>5}{r['top_share']:>5.0%}"
                  f"{'$'+format(oc, ',.0f'):>11}{'$'+format(r['fish_in_usd'] or 0, ',.0f'):>10}"
                  f"{col(format(dg, '>7.2f'), dc)}  {ago(r['ts'])}")

    # ---- 纸盘 ----
    print(f"\n{col('--- 纸盘持仓 ---', 'w')}")
    op = c.execute("SELECT * FROM paper_trades WHERE exit_ts IS NULL "
                   "ORDER BY entry_ts").fetchall()
    if not op:
        print(col("  (当前无持仓)", "d"))
    for r in op:
        snap = c.execute("SELECT * FROM snapshots WHERE pool=? ORDER BY ts DESC LIMIT 1",
                         (r["pool"],)).fetchone()
        px = snap["price"] if snap else None
        pnl = ((px / r["entry_price"] - 1) * 100
               if px and r["entry_price"] else None)
        dg = (snap["danger"] if snap else 0) or 0
        pc = "g" if (pnl or 0) >= 0 else "r"
        print(f"  {col((r['name'] or r['pool'][:10])[:15], 'c'):<24}"
              f"进场时狗庄成本 ${r['entry_op_cost']:,.0f}   持有 {ago(r['entry_ts'])}")
        print(f"    浮动盈亏 {col(format(pnl, '+.1f') + '%' if pnl is not None else '?', pc)}"
              f"   危险度 {dg:.2f}/0.30"
              f"   {col('接近离场线!', 'r') if dg >= 0.2 else ''}")

    done = c.execute("SELECT * FROM paper_trades WHERE exit_ts IS NOT NULL "
                     "ORDER BY exit_ts DESC LIMIT 6").fetchall()
    if done:
        print(f"\n{col('--- 最近平仓 ---', 'w')}")
        for r in done:
            pc = "g" if (r["pnl_pct"] or 0) >= 0 else "r"
            raw = f" (名义{r['pnl_pct_raw']:+.1f}%)" if r["pnl_pct_raw"] is not None else ""
            print(f"  {(r['name'] or r['pool'][:10])[:15]:<17}"
                  f"{col(format(r['pnl_pct'] or 0, '+7.1f') + '%', pc)}{raw:<18}"
                  f"{r['exit_reason']}   {ago(r['exit_ts'])}")
        pnls = [r["pnl_pct"] for r in c.execute(
            "SELECT pnl_pct FROM paper_trades WHERE exit_ts IS NOT NULL "
            "AND pnl_pct IS NOT NULL")]
        if pnls:
            win = sum(1 for x in pnls if x > 0)
            print(f"\n  累计 {len(pnls)} 笔   胜率 {win/len(pnls):.0%}   "
                  f"均值 {sum(pnls)/len(pnls):+.1f}%   "
                  f"最好 {max(pnls):+.1f}%  最差 {min(pnls):+.1f}%")

    # ---- 腾位统计: 看狗庄盘的稀有程度 ----
    ev = c.execute("SELECT reason, COUNT(*) n FROM watchlist "
                   "WHERE dropped_at IS NOT NULL GROUP BY reason "
                   "ORDER BY n DESC LIMIT 5").fetchall()
    if ev:
        tot = sum(r["n"] for r in ev)
        print(f"\n{col('--- 已腾位 ' + str(tot) + ' 个 ---', 'w')}")
        for r in ev:
            print(col(f"  {r['n']:>3}  {r['reason']}", "d"))

    print(col("\n" + "-" * 78, "d"))
    print(col("  每15秒自动刷新。关掉这个窗口不影响后台采集。", "d"))


if __name__ == "__main__":
    main()
