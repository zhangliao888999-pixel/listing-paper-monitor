# -*- coding: utf-8 -*-
"""诊断: 为什么观察中的池子不产生快照。逐道闸门检查。"""
import sys
from concurrent.futures import ThreadPoolExecutor
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import lab_db as db, lab_forensics as fx

c = db.conn()
pools = [r["pool"] for r in c.execute(
    "SELECT pool FROM watchlist WHERE dropped_at IS NULL LIMIT 6")]
print(f"抽查 {len(pools)} 个正在观察的池子\n")
for P in pools:
    n_snap = c.execute("SELECT COUNT(*) n FROM snapshots WHERE pool=?", (P,)).fetchone()["n"]
    sigs = fx.get_signatures(P, cap=2600)
    ok = [s for s in sigs if not s["err"] and s.get("ts")]
    txs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t in ex.map(fx.parse_tx, ok):
            if t: txs.append(t)
    print(f"{P[:14]}..  已有快照{n_snap}条")
    print(f"  签名{len(sigs)} 成功{len(ok)} 解析{len(txs)}")
    if len(sigs) > 2500:
        print("  -> refresh返回-1(池子太大),应被腾位"); print(); continue
    if len(txs) < 5:
        print("  -> 闸门: 交易数<5"); print(); continue
    m, wr = fx.analyze(P, txs, expected=len(txs))
    if m:
        print(f"  -> 正常出快照: 狗庄${m['op_cost_usd']:,.0f} 鱼${m['fish_in_usd']:,.0f} "
              f"钱包{m['n_wallet']} 集中{m['top_share']:.0%}")
    else:
        gi = sum(w["in_usd"] for w in wr) if wr else 0
        m2, _ = fx.analyze(P, txs)
        print(f"  -> analyze返回None。不带expected时: {'仍None' if not m2 else '正常'}")
        # 手动算守恒
        tot_in = tot_out = 0.0
        signers = {t['signer'] for t in txs if t['signer']}
        hits = {}
        for t in txs:
            for mint in {mm for (_, mm) in t["flow"] if mm in fx.QUOTES}:
                hits[mint] = hits.get(mint, 0) + 1
        quote = max(fx.QUOTES, key=lambda mm: hits.get(mm, 0))
        if hits.get(quote, 0) < len(txs) * 0.25: quote = fx.WSOL
        for t in txs:
            s = t["signer"]
            if not s: continue
            q2 = t["flow"].get((s, quote), 0.0)
            if quote == fx.WSOL and abs(q2) < 1e-9:
                q2 = t["sol_delta"].get(s, 0.0) + t["fee"]
            if q2 < 0: tot_in += -q2 * fx.QUOTES[quote][1]
            else: tot_out += q2 * fx.QUOTES[quote][1]
        print(f"     计价币{fx.QUOTES[quote][0]}  投入${tot_in:,.2f} 取出${tot_out:,.2f} "
              f"净${tot_in-tot_out:,.2f}  容差${-max(tot_in,1)*0.15:,.2f}")
        if tot_in - tot_out < -max(tot_in, 1.0) * 0.15:
            print("     -> 守恒闸门拦下")
    print()
