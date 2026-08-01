# -*- coding: utf-8 -*-
"""回溯: 用开盘指纹重跑已采集的池子,把作案钱包全部入库。

VPS上刚部署的惯犯监控模块现在库里是0个地址,无案可查。回跑历史数据能立刻
把库建起来 —— 而且有个额外收获: 能看出哪些地址在多个币上重复出现,那才是
真正值得长期盯的团队。

只花RPC不花CoinGecko额度(除了每个池子1次拿基本信息)。
"""
import sys, time
from collections import Counter
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import lab_db as db
import lab_launch as ll

def main():
    db.init(); ll.init()
    c = db.conn()
    pools = []
    for f in [a for a in sys.argv[1:] if a.endswith(".txt")]:
        pools += [l.strip() for l in Path(f).read_text().splitlines()
                  if len(l.strip()) > 30]
    if not pools:
        pools = [r["pool"] for r in c.execute("SELECT DISTINCT pool FROM snapshots")]
    pools = list(dict.fromkeys(pools))
    done = {r["pool"] for r in c.execute("SELECT pool FROM launch_fp")}
    todo = [p for p in pools if p not in done]
    print("待回跑 %d 个 (已有指纹 %d)" % (len(todo), len(done)), flush=True)
    ok = skip = big = 0
    t0 = time.time()
    for i, p in enumerate(todo, 1):
        try:
            out = ll.fingerprint(p, verbose=False)
        except Exception as e:
            skip += 1
            print("  [%d/%d] %s.. 出错 %s" % (i, len(todo), p[:10], type(e).__name__), flush=True)
            continue
        if not out:
            skip += 1
            continue
        res, bots, tt0, qpx = out
        ll.save(res, bots, tt0, qpx)
        ok += 1
        if res["score"] >= 3:
            big += 1
            print("  [%d/%d] %-18s %d/4 %s  头2min$%s 同时%d个 铺底$%s  (%d个钱包入库)"
                  % (i, len(todo), str(res["name"])[:16], res["score"], res["verdict"],
                     format(res["cap_2min"], ",.0f"), res["burst_wallets"],
                     format(res["seed_max"], ",.0f"), len(bots)), flush=True)
        elif i % 20 == 0:
            print("  [%d/%d] 已完成%d 大网%d 跳过%d  (%.0f分钟)"
                  % (i, len(todo), ok, big, skip, (time.time()-t0)/60), flush=True)
    print("")
    print("回跑完成: 出指纹 %d 个, 其中大网 %d 个, 跳过 %d" % (ok, big, skip))
    print("")
    n = c.execute("SELECT COUNT(DISTINCT addr) n FROM operator_wallets").fetchone()["n"]
    print("作案钱包库: %d 个地址" % n)
    print("")
    print("在多个币上重复出现的地址(真正的团队):")
    print("  %-46s%8s%14s" % ("钱包", "币数", "累计净USD"))
    for r in c.execute("SELECT addr, COUNT(*) n, SUM(net_usd) s FROM operator_wallets "
                       "GROUP BY addr HAVING n > 1 ORDER BY n DESC, s DESC LIMIT 25"):
        print("  %-46s%8d%14s" % (r["addr"], r["n"], "$" + format(r["s"] or 0, ",.0f")))

if __name__ == "__main__":
    main()
