# -*- coding: utf-8 -*-
"""Operator-lab status snapshot. ASCII source, UTF-8 output."""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import lab_db as db, lab_forensics as fx
c = db.conn()
print("Helius:", fx.usage_report())
n_s = c.execute("SELECT COUNT(*) n FROM snapshots").fetchone()["n"]
n_p = c.execute("SELECT COUNT(DISTINCT pool) n FROM snapshots").fetchone()["n"]
print(f"snapshots {n_s} / pools {n_p}")
try:
    kept = c.execute("SELECT COUNT(*) n FROM watchlist WHERE dropped_at IS NULL").fetchone()["n"]
    drop = c.execute("SELECT COUNT(*) n FROM watchlist WHERE dropped_at IS NOT NULL").fetchone()["n"]
    print(f"watchlist kept {kept} / evicted {drop}")
    for r in c.execute("SELECT reason, COUNT(*) n FROM watchlist WHERE dropped_at IS NOT NULL "
                       "GROUP BY reason ORDER BY n DESC LIMIT 5"):
        print(f"   {r['n']:>3}  {r['reason']}")
except Exception as e:
    print("watchlist:", e)
print("--- paper ---")
for r in c.execute("SELECT * FROM paper_trades ORDER BY entry_ts"):
    st = (f"{r['pnl_pct']:+.1f}% [{r['exit_reason']}]" if r["exit_ts"] else "OPEN")
    print(f"  {(r['name'] or r['pool'][:10])[:18]:<20} op_cost=${r['entry_op_cost']:>8,.0f}  {st}")
