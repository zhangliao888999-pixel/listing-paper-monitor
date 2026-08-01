# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import lab_db as db
c = db.conn()
print("最近侦察到的币(评分从高到低):")
print("  %-18s%5s%12s%7s%7s%11s  %s" % ("币", "评分", "头2min", "同时", "同秒", "铺底", "池子地址"))
for r in c.execute("SELECT * FROM launch_fp ORDER BY checked_at DESC LIMIT 12"):
    print("  %-18s%4d/4%12s%7d%6.0f%%%11s  %s"
          % (str(r["name"])[:16], r["score"] or 0,
             "$" + format(r["cap_2min"] or 0, ",.0f"), r["burst_wallets"] or 0,
             (r["samesec_ratio"] or 0) * 100,
             "$" + format(r["seed_max"] or 0, ",.0f"), r["pool"]))
print("")
print("mint 地址:")
for r in c.execute("SELECT name, mint, pool FROM launch_fp WHERE score>=3 "
                   "ORDER BY checked_at DESC LIMIT 6"):
    print("  %-18s %s" % (str(r["name"])[:16], r["mint"]))
