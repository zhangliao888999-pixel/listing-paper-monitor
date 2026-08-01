# -*- coding: utf-8 -*-
"""按新判据重算已有指纹的评分。四项原始测量都存在库里,不用重新拉链上。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import lab_db as db, lab_launch as ll
c = db.conn()
rows = c.execute("SELECT * FROM launch_fp").fetchall()
chg = 0
for r in rows:
    cap2 = r["cap_2min"] or 0
    gate = cap2 >= ll.MIN_CAP2
    sc = ((1 if gate else 0) + (1 if (r["burst_wallets"] or 0) >= ll.MIN_BURST else 0)
          + (1 if (r["samesec_ratio"] or 0) >= ll.MIN_SAMESEC else 0)
          + (1 if (r["n_seed"] or 0) > 0 else 0))
    if not gate:
        v = "假铺底(钱没留下)" if (r["n_seed"] or 0) > 0 else "无本钱"
        sc = min(sc, 2)
    else:
        v = {4: "大网(四条全中)", 3: "疑似大网", 2: "中等"}.get(sc, "弱")
    if sc != r["score"] or v != r["verdict"]:
        chg += 1
        c.execute("UPDATE launch_fp SET score=?, verdict=? WHERE pool=?", (sc, v, r["pool"]))
c.commit()
print("重算 %d 个,改变 %d 个" % (len(rows), chg))
print("")
print("  新判据下的大网(>=3分):")
print("  %-20s%6s%13s%8s%8s%12s  %s" % ("币","评分","头2min","同时","同秒","铺底","判定"))
for r in c.execute("SELECT * FROM launch_fp WHERE score>=3 ORDER BY cap_2min DESC"):
    print("  %-20s%5d/4%13s%8d%7.0f%%%12s  %s"
          % (str(r["name"])[:18], r["score"], "$"+format(r["cap_2min"] or 0,",.0f"),
             r["burst_wallets"] or 0, (r["samesec_ratio"] or 0)*100,
             "$"+format(r["seed_max"] or 0,",.0f"), r["verdict"]))
print("")
n = c.execute("SELECT COUNT(*) n FROM launch_fp WHERE score>=3").fetchone()["n"]
t = c.execute("SELECT COUNT(*) n FROM launch_fp").fetchone()["n"]
print("  %d/%d 个达标 (%.0f%%)" % (n, t, n/max(t,1)*100))
