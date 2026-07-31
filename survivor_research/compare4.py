# -*- coding: utf-8 -*-
"""用修好的引擎跑用户给的四个池子,做横向对比。后台跑,结果写文件。"""
import calendar, sys, time
from concurrent.futures import ThreadPoolExecutor
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import cg_client as cg, lab_forensics as fx

POOLS = ["DpvEaZZzoZ74tY9dFRniBiFiLDPGQvEQcvU9A5iK53Ph",
         "EhAdoHrWGkE19rGSCKt1WzHxPuKwkwXN3cHu1Zyn6F6K",
         "BYJV6ia1Z1nYYY18id46Qrtkm55h35sUeqWv9WPcSy7R",
         "Enh6fbguFqMSxEX1oVWbvYAhHPvWnUkVtzJxuHwxBSoU"]
res = []
for p in POOLS:
    info = cg.get(f"networks/solana/pools/{p}")
    a = info["data"]["attributes"]
    tx = (a.get("transactions") or {}).get("h24") or {}
    sigs = fx.get_signatures(p)
    ok = [s for s in sigs if not s["err"] and s.get("ts")]
    print(f"{a.get('name')}: {len(sigs)}签名 成功{len(ok)} 开始解析...", flush=True)
    txs = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for t in ex.map(fx.parse_tx, ok):
            if t: txs.append(t)
    m, wr = fx.analyze(p, txs, expected=len(ok))
    res.append((a, tx, m, wr, len(sigs), len(ok), len(txs)))
    print(f"  完成 {len(txs)}/{len(ok)}", flush=True)

print()
print("=" * 100)
print(f"{'币':<15}{'年龄h':>6}{'成功交易':>8}{'钱包':>6}{'集中':>6}{'狗庄成本':>11}{'鱼投入':>11}{'危险度':>8}{'他的盈亏':>11}{'结局':>10}")
print("=" * 100)
for a, tx, m, wr, nsig, nok, ntx in res:
    name = str(a.get("name"))[:14]
    if not m:
        print(f"{name:<15}  >>> 数据不可信(覆盖率或守恒未通过) {ntx}/{nok}")
        continue
    born = calendar.timegm(time.strptime((a.get("pool_created_at") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    op, fish = m["op_cost_usd"], m["fish_in_usd"]
    print(f"{name:<15}{(time.time()-born)/3600:>6.1f}{nok:>8}{m['n_wallet']:>6}{m['top_share']:>5.0%}"
          f"{op:>11,.0f}{fish:>11,.0f}{fish/max(op,1):>8.2f}{m['op_pnl_usd']:>+11,.0f}{m['outcome']:>10}")
print("=" * 100)
for a, tx, m, wr, nsig, nok, ntx in res:
    if not m: continue
    print(f"\n{a.get('name')}  ({nsig}个签名, 其中{nsig-nok}笔失败)")
    print(f"  24h 买{tx.get('buys')}笔/{tx.get('buyers')}人  卖{tx.get('sells')}笔/{tx.get('sellers')}人"
          f"  流动性${float(a.get('reserve_in_usd') or 0):,.0f}")
    print(f"  撒饵速度 ${m['ratchet_usd_min']:,.2f}/分钟   存活{m['life_min']/60:.1f}小时   静止{m['idle_min']:.0f}分钟")
    fish_w = sorted((w for w in wr if w["role"] == "fish"), key=lambda w: -w["in_usd"])[:3]
    if fish_w:
        print("  最大的几条鱼:", "  ".join(f"${w['in_usd']:,.0f}" for w in fish_w))
