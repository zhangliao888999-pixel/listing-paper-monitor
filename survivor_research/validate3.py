# -*- coding: utf-8 -*-
"""money-mover 归属 A/B 验证。
DISNEY: 签名人就是出钱人, 两种口径应基本一致(回归保护)。
$GATE : 出钱账户不是签名人, 新口径应把 BwWK17cbHxwW 抓出来。"""
import sys
from concurrent.futures import ThreadPoolExecutor
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import lab_forensics as fx, lab_registry as reg
reg.init()

def lookup(a, pool, inn, out, hits, ntx):
    return reg.role_of(a, pool=pool, inn=inn, out=out, hits=hits, ntx=ntx,
                       allow_probe=True)

for P, name in [("BYJV6ia1Z1nYYY18id46Qrtkm55h35sUeqWv9WPcSy7R", "DISNEY 回归保护"),
                ("GywT8URw1N8V4qL6xNWYQraHSHmWfASMExvVscrRKeDR", "$GATE  关键用例")]:
    sigs = [s for s in fx.get_signatures(P, cap=1500) if not s["err"] and s.get("ts")]
    txs = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for t in ex.map(fx.parse_tx, sigs):
            if t: txs.append(t)
    print("=" * 74)
    print(f"  {name}   {len(txs)}笔")
    print("=" * 74)
    for lbl, mover in (("按签名人(旧)", False), ("按出钱账户(新)", True)):
        m, wr = fx.analyze(P, txs, expected=len(txs), use_mover=mover)
        if not m:
            print(f"  {lbl:<16} 被拦下"); continue
        print(f"  {lbl:<16} 钱包{m['n_wallet']:>4} 集中{m['top_share']:>5.0%} "
              f"狗庄${m['op_cost_usd']:>9,.0f} 鱼${m['fish_in_usd']:>9,.0f} "
              f"储备${m['peak_res_usd']:>9,.0f} 质量={m['data_quality']}")
        if mover:
            top = sorted(wr, key=lambda w: -(w["in_usd"] + w["out_usd"]))[:4]
            for w in top:
                print(f"      {w['addr'][:14]}.. {w['role']:<9} 投${w['in_usd']:>8,.2f} "
                      f"取${w['out_usd']:>8,.2f} {w['n_tx']}笔")
    # 加角色过滤
    m3, wr3 = fx.analyze(P, txs, expected=len(txs), use_mover=True, role_lookup=lookup)
    if m3:
        print(f"  {'+角色过滤':<16} 钱包{m3['n_wallet']:>4} 集中{m3['top_share']:>5.0%} "
              f"狗庄${m3['op_cost_usd']:>9,.0f} 鱼${m3['fish_in_usd']:>9,.0f} "
              f"托管占比{m3.get('custodial_share',0):.0%} 可信={'是' if m3.get('attribution_ok') else '否'}")
    print(flush=True)
