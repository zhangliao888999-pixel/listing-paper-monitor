# -*- coding: utf-8 -*-
"""CONTRA 收网后的资金拆分。用金库净额口径(今天验证过的唯一正确口径)。"""
import sys, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import cg_client as cg
import lab_forensics as fx
import lab_launch as ll

P = "Rnazy9ZTv44v3QR6kkPZu5JQUpT7u1JTCdJ4AddYs5Q"
SOL = 75.0

a = cg.get("networks/solana/pools/" + P)["data"]["attributes"]
tx = (a.get("transactions") or {}).get("h24") or {}
print("=" * 76)
print("  %s   %s" % (a.get("name"), P))
print("=" * 76)
print("  建池 %s   现价 $%s" % (a.get("pool_created_at"), a.get("base_token_price_usd")[:12]))
print("  流动性 $%s   24h量 $%s   FDV $%s"
      % (format(float(a.get("reserve_in_usd") or 0), ",.0f"),
         format(float((a.get("volume_usd") or {}).get("h24") or 0), ",.0f"),
         format(float(a.get("fdv_usd") or 0), ",.0f")))
print("  24h 买%s笔/%s人  卖%s笔/%s人" % (tx.get("buys"), tx.get("buyers"),
                                        tx.get("sells"), tx.get("sellers")))
print("  涨跌 %s" % a.get("price_change_percentage"))

sigs = [s for s in fx.get_signatures(P) if not s["err"] and s.get("ts")]
print("")
print("  链上 %d 笔成功交易, 最新距今 %.0f 分钟, 存活 %.1f 小时"
      % (len(sigs), (time.time() - sigs[-1]["ts"]) / 60,
         (sigs[-1]["ts"] - sigs[0]["ts"]) / 3600), flush=True)
txs = []
done = [0]
def one(s):
    t = fx.parse_tx(s)
    done[0] += 1
    if done[0] % 3000 == 0:
        print("    %d/%d" % (done[0], len(sigs)), flush=True)
    return t
with ThreadPoolExecutor(max_workers=10) as ex:
    for t in ex.map(one, sigs):
        if t:
            txs.append(t)
txs.sort(key=lambda x: x["ts"])
print("  解析 %d 笔" % len(txs))
quote = ll.detect_quote(txs)
qpx = fx.QUOTES[quote][1]
vault = ll.find_vault(txs, P, quote)
print("  计价币 %s   金库 %s" % (fx.QUOTES[quote][0], vault))

t0 = txs[0]["ts"]
per = defaultdict(lambda: {"in": 0.0, "out": 0.0, "n": 0, "first": None})
vin = vout = 0.0
for t in txs:
    vd = ll.qdelta(t, vault, quote)
    if abs(vd) < 1e-9:
        continue
    src = (t["sol_delta"].items() if quote == fx.WSOL
           else [(o, v) for (o, m), v in t["flow"].items() if m == quote])
    best, bd = None, 0.0
    for x, d in src:
        if x in (vault, P):
            continue
        if vd > 0 and d < 0 and abs(d) > abs(bd):
            best, bd = x, d
        elif vd < 0 and d > 0 and abs(d) > abs(bd):
            best, bd = x, d
    if not best:
        continue
    p = per[best]
    p["n"] += 1
    if p["first"] is None:
        p["first"] = t["ts"]
    if vd > 0:
        p["in"] += vd; vin += vd
    else:
        p["out"] += -vd; vout += -vd
print("")
print("  金库累计流入 %.2f (%s)   流出 %.2f   净 %.2f = $%s"
      % (vin, fx.QUOTES[quote][0], vout, vin - vout,
         format((vin - vout) * qpx, ",.0f")))

bots = {x for x, v in per.items()
        if v["first"] and v["first"] - t0 <= 180 and v["n"] >= 10}
fish = {x: v for x, v in per.items() if x not in bots}
b_in = sum(per[x]["in"] for x in bots); b_out = sum(per[x]["out"] for x in bots)
f_in = sum(v["in"] for v in fish.values()); f_out = sum(v["out"] for v in fish.values())
print("")
print("  === 狗庄组 (%d个钱包, 开盘3分钟内进场且>=10笔) ===" % len(bots))
print("    投入 %s   取出 %s   净 %s"
      % ("$" + format(b_in * qpx, ",.0f"), "$" + format(b_out * qpx, ",.0f"),
         "$" + format((b_in - b_out) * qpx, ",.0f")))
print("")
print("  === 其余 %d 个钱包 ===" % len(fish))
print("    投入 %s   取出 %s   净 %s"
      % ("$" + format(f_in * qpx, ",.0f"), "$" + format(f_out * qpx, ",.0f"),
         "$" + format((f_in - f_out) * qpx, ",.0f")))
big = sorted(fish.items(), key=lambda kv: -kv[1]["in"])[:12]
print("")
print("    投入最多的外部钱包:")
print("    %-14s%7s%12s%12s%12s%9s" % ("钱包", "笔数", "投入", "取出", "净", "进场分"))
for x, v in big:
    if v["in"] * qpx < 1:
        continue
    print("    %-14s%7d%12s%12s%12s%9.0f"
          % (x[:12], v["n"], "$" + format(v["in"] * qpx, ",.0f"),
             "$" + format(v["out"] * qpx, ",.0f"),
             "$" + format((v["in"] - v["out"]) * qpx, ",.0f"),
             (v["first"] - t0) / 60))
sizes = sorted(v["in"] * qpx for v in fish.values())
print("")
print("    外部钱包投入分档:")
for lo, hi, nm in [(0, 1, "<$1(粉尘)"), (1, 10, "$1-10"), (10, 100, "$10-100"),
                   (100, 1000, "$100-1k"), (1000, 1e12, ">$1k")]:
    g = [x for x in sizes if lo <= x < hi]
    print("      %-14s%6d个   合计 $%s" % (nm, len(g), format(sum(g), ",.0f")))
