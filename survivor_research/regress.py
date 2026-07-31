# -*- coding: utf-8 -*-
"""角色过滤前后对比。验证剔除费用账户和托管平台后,已知样本的数字怎么变。"""
import sys
from concurrent.futures import ThreadPoolExecutor
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import lab_forensics as fx, lab_registry as reg
reg.init()

def lookup(a, pool, inn, out, hits, ntx):
    return reg.role_of(a, pool=pool, inn=inn, out=out, hits=hits, ntx=ntx,
                       allow_probe=True)

for P, name, expect in [
    ("BYJV6ia1Z1nYYY18id46Qrtkm55h35sUeqWv9WPcSy7R", "DISNEY (USDC)", "狗庄约$5000 鱼$0"),
    ("GywT8URw1N8V4qL6xNWYQraHSHmWfASMExvVscrRKeDR", "$GATE (SOL)", "应剔除2个托管账户"),
]:
    sigs = [s for s in fx.get_signatures(P, cap=1200) if not s["err"] and s.get("ts")]
    txs = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for t in ex.map(fx.parse_tx, sigs):
            if t: txs.append(t)
    old, _ = fx.analyze(P, txs, expected=len(sigs))
    print(f"[{name}] 不过滤跑完", flush=True)
    new, wr = fx.analyze(P, txs, expected=len(sigs), role_lookup=lookup)
    print("=" * 70)
    print(f"  {name}   (预期: {expect})")
    print("=" * 70)
    for lbl, m in (("不过滤", old), ("角色过滤后", new)):
        if not m:
            print(f"  {lbl}: 被闸门拦下"); continue
        print(f"  {lbl:<12} 钱包{m['n_wallet']:>4}  集中度{m['top_share']:>5.0%}  "
              f"狗庄${m['op_cost_usd']:>9,.0f}  鱼${m['fish_in_usd']:>9,.0f}  "
              f"盈亏${m['op_pnl_usd']:>+9,.0f}")
    if new:
        print(f"  托管平台占资金流量 {new.get('custodial_share',0):.0%}   "
              f"归属可信 {'是' if new.get('attribution_ok') else '否'}")
    print(flush=True)
print("注册表:", reg.stats())
