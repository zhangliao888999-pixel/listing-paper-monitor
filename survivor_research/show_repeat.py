# -*- coding: utf-8 -*-
"""列出跨币重复出现的作案钱包 —— 真正的团队标识。"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import lab_db as db
c = db.conn()
try:
    tot = c.execute("SELECT COUNT(DISTINCT addr) n FROM operator_wallets").fetchone()["n"]
    nfp = c.execute("SELECT COUNT(*) n FROM launch_fp").fetchone()["n"]
    nbig = c.execute("SELECT COUNT(*) n FROM launch_fp WHERE score>=3").fetchone()["n"]
except Exception as e:
    print("表还没建:", e); raise SystemExit
print("已指纹 %d 个币, 其中大网 %d 个, 作案钱包库 %d 个地址" % (nfp, nbig, tot))
print("")
rows = c.execute("""SELECT addr, COUNT(DISTINCT pool) np, SUM(n_tx) nt,
                           SUM(net_usd) net, MIN(first_sec) fs
                    FROM operator_wallets GROUP BY addr
                    HAVING np > 1 ORDER BY np DESC, nt DESC""").fetchall()
print("=== 跨币重复出现的地址 (%d 个) ===" % len(rows))
if not rows:
    print("  (还没有)")
else:
    print("  %-46s%7s%9s%13s%9s" % ("钱包", "币数", "总笔数", "累计净USD", "最早进场秒"))
    for r in rows[:30]:
        print("  %-46s%7d%9d%13s%9.0f"
              % (r["addr"], r["np"], r["nt"] or 0,
                 "$" + format(r["net"] or 0, ",.0f"), r["fs"] or 0))
    print("")
    print("  它们各自出现在哪些币上:")
    for r in rows[:8]:
        ps = c.execute("""SELECT f.name, f.score, f.cap_2min, o.n_tx, o.net_usd
                          FROM operator_wallets o LEFT JOIN launch_fp f ON f.pool=o.pool
                          WHERE o.addr=? ORDER BY f.checked_at DESC""", (r["addr"],)).fetchall()
        print("    %s" % r["addr"])
        for x in ps:
            print("       %-20s %s/4  头2min$%s   本币%d笔 净$%s"
                  % (str(x["name"])[:18], x["score"] or 0,
                     format(x["cap_2min"] or 0, ",.0f"), x["n_tx"] or 0,
                     format(x["net_usd"] or 0, ",.0f")))
print("")
try:
    al = c.execute("SELECT * FROM scout_alerts ORDER BY ts DESC LIMIT 15").fetchall()
    if al:
        print("=== 最近的告警 ===")
        for a in al:
            print("  %s  %-8s %-18s %s"
                  % (time.strftime("%m-%d %H:%M", time.localtime(a["ts"])),
                     a["kind"], str(a["name"])[:16], str(a["detail"])[:70]))
except Exception:
    pass
