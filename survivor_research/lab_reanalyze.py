# -*- coding: utf-8 -*-
"""用当前引擎重算库里已有的池子。2026-07-31建。

引擎在一晚上里改了五处会影响数字的地方:
  1. 守恒闸门 -> 历史完整性检查(旧闸门系统性误杀毕业迁移来的池子)
  2. money-mover 归属(旧的按签名人记账,被"热钱包签名+另账户出资"绕过)
  3. 池子金库按出现频率排除(否则金库被当成"鱼",凭空多出几千刀)
  4. 建池注资不计入买卖(DISNEY 那笔 $7,510 是注入流动性不是买入)
  5. 簇识别加入"卖出币量远超买入"(USOH 那12个砸盘钱包靠转账拿币,
     转账不经过池子,并查集看不到)

所以旧口径采的 metrics 和 dump_events 全部要重算,不能和新数据混在一起。
"""
import sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import lab_db as db, lab_forensics as fx, lab_registry as reg

reg.init()
def lookup(a, pool, inn, out, hits, ntx):
    return reg.role_of(a, pool=pool, inn=inn, out=out, hits=hits, ntx=ntx,
                       allow_probe=True)

def main():
    c = db.conn()
    pools = [r["pool"] for r in c.execute(
        "SELECT DISTINCT pool FROM snapshots UNION SELECT pool FROM dump_events")]
    print(f"待重算 {len(pools)} 个池子", flush=True)
    ok = skip = 0
    for i, P in enumerate(pools, 1):
        try:
            sigs = [s for s in fx.get_signatures(P, cap=3000)
                    if not s["err"] and s.get("ts")]
            if len(sigs) < 8:
                skip += 1; continue
            txs = []
            with ThreadPoolExecutor(max_workers=10) as ex:
                for t in ex.map(fx.parse_tx, sigs):
                    if t: txs.append(t)
            m, wr = fx.analyze(P, txs, expected=len(txs), role_lookup=lookup)
            if not m:
                skip += 1
                print(f"  [{i}/{len(pools)}] {P[:10]}.. 拿不到有效结果", flush=True)
                continue
            db.save_metrics(P, m)
            db.save_wallets(P, wr)
            ok += 1
            print(f"  [{i}/{len(pools)}] {P[:10]}.. 狗庄${m['op_cost_usd']:>9,.0f} "
                  f"鱼${m['fish_in_usd']:>9,.0f} 质量={m['data_quality']} "
                  f"可信={'是' if m.get('attribution_ok') else '否'} {m['outcome']}",
                  flush=True)
        except Exception as e:
            skip += 1
            print(f"  [{i}/{len(pools)}] {P[:10]}.. 出错 {type(e).__name__}: {e}", flush=True)
    print(f"\n完成: 重算 {ok} 个, 跳过 {skip} 个")
    print("注册表:", reg.stats())

if __name__ == "__main__":
    main()
