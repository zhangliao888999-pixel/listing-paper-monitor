# -*- coding: utf-8 -*-
"""DISNEY 收网后的最终资金账。"""
import sys, time
from concurrent.futures import ThreadPoolExecutor
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import cg_client as cg, lab_forensics as fx, lab_registry as reg
reg.init()
P = "BYJV6ia1Z1nYYY18id46Qrtkm55h35sUeqWv9WPcSy7R"
a = cg.get(f"networks/solana/pools/{P}")["data"]["attributes"]
tx = (a.get("transactions") or {}).get("h24") or {}
print("=" * 76)
print(f"  {a.get('name')}  收网后")
print("=" * 76)
print(f"  价格 ${float(a.get('base_token_price_usd') or 0):.10f}   FDV ${float(a.get('fdv_usd') or 0):,.0f}")
print(f"  GT流动性 ${float(a.get('reserve_in_usd') or 0):,.0f}   24h量 ${float((a.get('volume_usd') or {}).get('h24') or 0):,.0f}")
print(f"  涨跌 {a.get('price_change_percentage')}")
print(f"  24h 买{tx.get('buys')}笔/{tx.get('buyers')}人  卖{tx.get('sells')}笔/{tx.get('sellers')}人", flush=True)
sigs = [s for s in fx.get_signatures(P) if not s["err"] and s.get("ts")]
print(f"\n  全量签名 {len(sigs)} 笔, 解析中...", flush=True)
txs = []
with ThreadPoolExecutor(max_workers=10) as ex:
    for t in ex.map(fx.parse_tx, sigs):
        if t: txs.append(t)
def lookup(x, pool, inn, out, hits, ntx):
    return reg.role_of(x, pool=pool, inn=inn, out=out, hits=hits, ntx=ntx, allow_probe=True)
m, wr = fx.analyze(P, txs, expected=len(txs), role_lookup=lookup)
if not m:
    print("  分析失败"); raise SystemExit
print(f"  解析 {len(txs)}/{len(sigs)}   质量={m['data_quality']}   存活 {m['life_min']/60:.1f}小时")
print()
print("  === 狗庄的账 ===")
print(f"    砸进池子买货   ${m['op_cost_usd']:>12,.2f}")
print(f"    gas+优先费     ${m['op_gas_usd']:>12,.2f}")
print(f"    ---------------------------------")
print(f"    合计投入       ${m['op_cost_usd']+m['op_gas_usd']:>12,.2f}")
print(f"    砸盘拿回       ${m['op_out_usd']:>12,.2f}")
print(f"    ---------------------------------")
print(f"    净盈亏         ${m['op_pnl_usd']:>+12,.2f}")
print(f"    手里还压着     {m['op_tok_held']:>12,.0f} 个币")
print()
print(f"  === 鱼 ===")
print(f"    {m['fish_n']}个钱包   投入 ${m['fish_in_usd']:,.2f}   取出 ${m['fish_out_usd']:,.2f}")
print()
print(f"  === 池子 ===")
print(f"    峰值储备 ${m['peak_res_usd']:,.2f}   现在 ${m['end_res_usd']:,.2f}   抽走 ${m['drained_usd']:,.2f}")
print(f"    砸盘持续 {m['dump_sec']}秒   撒饵速度 ${m['ratchet_usd_min']:,.2f}/分钟")
print(f"    结局 {m['outcome']}")
print()
print("  === 参与者明细 ===")
for w in sorted(wr, key=lambda x: -(x["in_usd"] + x["out_usd"]))[:10]:
    print(f"    {w['addr'][:14]}.. {w['role']:<9}{w['n_tx']:>5}笔 {w['n_buy']:>4}买{w['n_sell']:>4}卖 "
          f"投${w['in_usd']:>9,.2f} 取${w['out_usd']:>9,.2f} gas${w['gas_usd']:>6,.2f}")
