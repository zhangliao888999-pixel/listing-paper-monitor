# -*- coding: utf-8 -*-
"""验证历史完整性检查: 已知样本必须仍然正确, 之前被误杀的必须复活。"""
import sys
from concurrent.futures import ThreadPoolExecutor
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import lab_forensics as fx

CASES = [
    ("BYJV6ia1Z1nYYY18id46Qrtkm55h35sUeqWv9WPcSy7R", "DISNEY  从零开始的Meteora池", "应通过, 初始储备≈0"),
    ("GywT8URw1N8V4qL6xNWYQraHSHmWfASMExvVscrRKeDR", "$GATE   pump.fun池", "应通过"),
    ("BFBM1NqjEvuxGcD6tvvHmUFYWqtQh3z1MeQNXKC5bbwa", "USOH    毕业迁移来的池", "应通过, 初始储备≈$50000"),
    ("6U4Pq9emEFYfm8XmbG5FJnbEwLNbnVjrkjWJPMj6cyqQ", "VPS样本1 之前被误杀", "应复活"),
]
for P, name, expect in CASES:
    try:
        sigs = [s for s in fx.get_signatures(P, cap=2600) if not s["err"] and s.get("ts")]
        if not sigs:
            print(f"{name:<34} 拿不到签名"); continue
        txs = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            for t in ex.map(fx.parse_tx, sigs):
                if t: txs.append(t)
        m, wr = fx.analyze(P, txs, expected=len(txs))
        if not m:
            print(f"{name:<34} 仍被拦下  ({len(txs)}/{len(sigs)}笔)   预期: {expect}")
        else:
            print(f"{name:<34} 通过  {m['n_tx']}笔/{m['n_wallet']}钱包")
            print(f"{'':<34}  见到诞生事件={'是' if m['has_birth'] else '否'}  "
                  f"初始储备 ${m['init_reserve_usd']:,.0f}")
            print(f"{'':<34}  狗庄${m['op_cost_usd']:,.0f}  鱼${m['fish_in_usd']:,.0f}  "
                  f"集中度{m['top_share']:.0%}  结局{m['outcome']}")
            print(f"{'':<34}  预期: {expect}")
    except Exception as e:
        print(f"{name:<34} 出错 {type(e).__name__}: {e}")
    print(flush=True)
