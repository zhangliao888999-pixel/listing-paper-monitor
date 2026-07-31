# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import lab_db as db
c = db.conn()
def q(sql, *a):
    try: return c.execute(sql, a).fetchall()
    except Exception as e: return [{"err": str(e)}]
print("dump_events 表:")
r = q("SELECT COUNT(*) n FROM dump_events")
print("  ", dict(r[0]) if r else "无")
print("false_alarms 表:")
r = q("SELECT COUNT(*) n FROM false_alarms")
print("  ", dict(r[0]) if r else "无")
print()
print("最近的快照里,符合进场条件的有几个:")
rows = q("""SELECT s.pool, s.op_cost_usd, s.top_share, s.fish_in_usd, s.danger, s.n_tx
            FROM snapshots s JOIN (SELECT pool, MAX(ts) m FROM snapshots GROUP BY pool) x
            ON s.pool=x.pool AND s.ts=x.m
            JOIN watchlist w ON w.pool=s.pool AND w.dropped_at IS NULL""")
ok = 0
print(f"  当前观察中的池子: {len(rows)}")
for r in rows:
    if "err" in r.keys(): print("  ", r["err"]); break
    cond = (r["op_cost_usd"] or 0) >= 100 and (r["top_share"] or 0) >= 0.60 \
           and (r["fish_in_usd"] or 0) <= 20 and (r["danger"] or 0) < 0.5
    if cond: ok += 1
print(f"  满足进场条件(成本>=100 集中>=60% 鱼<=20 危险<0.5): {ok}")
print()
print("  成本最高的8个:")
for r in sorted(rows, key=lambda x: -(x["op_cost_usd"] or 0))[:8]:
    if "err" in r.keys(): break
    print(f"    成本${r['op_cost_usd'] or 0:>8,.0f} 集中{r['top_share'] or 0:>4.0%} "
          f"鱼${r['fish_in_usd'] or 0:>7,.0f} 危险{r['danger'] or 0:>5.2f} {r['n_tx']:>4}笔")
