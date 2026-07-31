# -*- coding: utf-8 -*-
"""USOH 全量(不设cap)验证: 应看到诞生事件, 初始储备约$50000。"""
import sys
from concurrent.futures import ThreadPoolExecutor
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import lab_forensics as fx
P = "BFBM1NqjEvuxGcD6tvvHmUFYWqtQh3z1MeQNXKC5bbwa"
sigs = [s for s in fx.get_signatures(P) if not s["err"] and s.get("ts")]
print(f"全量签名 {len(sigs)} 笔, 开始解析...", flush=True)
txs = []
done = [0]
with ThreadPoolExecutor(max_workers=10) as ex:
    for t in ex.map(fx.parse_tx, sigs):
        done[0] += 1
        if t: txs.append(t)
        if done[0] % 1000 == 0: print(f"  {done[0]}/{len(sigs)}", flush=True)
m, wr = fx.analyze(P, txs, expected=len(txs))
if not m:
    print("仍被拦下")
else:
    print(f"USOH 全量: {m['n_tx']}笔/{m['n_wallet']}钱包  质量={m['data_quality']}")
    print(f"  见到诞生事件={'是' if m['has_birth'] else '否'}   初始储备 ${m['init_reserve_usd']:,.0f}")
    print(f"  狗庄成本 ${m['op_cost_usd']:,.0f}  取出 ${m['op_out_usd']:,.0f}  盈亏 ${m['op_pnl_usd']:+,.0f}")
    print(f"  鱼 {m['fish_n']}个 投入 ${m['fish_in_usd']:,.0f}")
    print(f"  峰值储备 ${m['peak_res_usd']:,.0f}  抽走 ${m['drained_usd']:,.0f}  结局 {m['outcome']}")
    print(f"  已知答案: 狗庄净赚约$17000-21000, 鱼亏约$20275, 初始储备672 SOL≈$50400")
