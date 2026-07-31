# -*- coding: utf-8 -*-
"""赚钱的狗庄有什么共同特点。2026-07-31建。

用户的问题: 钓没钓到鱼,除了运气,是不是跟"饵撒得足不足、能不能迅速把币价
拉起来"有关?

目前三个完整样本的对比已经很暗示了:
  USOH   投入$47,971  钓到$19,202  净 +$18,279
  DISNEY 投入$16,260  钓到$175     净 +$67
  $GATE  投入$557     钓到$78      净 -$6
投入量差了两个数量级,结果也差两个数量级。但 n=3 说明不了因果,可能只是
"大盘子本来就容易吸引人"。要用足够多的样本把特征拆开看。

对每个池子算这些特征,再按狗庄赚/亏分组比较:
  op_cost        铺底+撒饵的总投入
  ratchet        撒饵速度($/分钟)
  price_mult     价格从最低拉到最高的倍数
  pump_speed     拉盘速度(倍数/小时)
  life_min       存活时长
  top_share      集中度
  n_wallet       参与钱包数
  fish_in        钓到的鱼

结果增量写 winners_rows.csv,跑一半也能看。

用法: python lab_winners.py <池子清单.txt> [起始序号]
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import lab_db as db          # noqa: E402
import lab_forensics as fx   # noqa: E402
import lab_registry as reg   # noqa: E402

OUT = HERE / "winners_rows.csv"
MAX_SIGS = 4000
COLS = ("pool,op_cost,op_out,op_pnl,op_gas,fish_in,fish_n,n_wallet,n_tx,"
        "top_share,life_min,ratchet,init_reserve,peak_res,drained,"
        "dump_sec,quality,attrib_ok,outcome")


def lookup(a, pool, inn, out, hits, ntx):
    return reg.role_of(a, pool=pool, inn=inn, out=out, hits=hits, ntx=ntx,
                       allow_probe=True)


def one_pool(P):
    sigs = [s for s in fx.get_signatures(P, cap=MAX_SIGS + 100)
            if not s["err"] and s.get("ts")]
    if len(sigs) < 10 or len(sigs) > MAX_SIGS:
        return None, f"交易数{len(sigs)}"
    txs = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for t in ex.map(fx.parse_tx, sigs):
            if t:
                txs.append(t)
    if len(txs) < len(sigs) * 0.9:
        return None, f"覆盖率{len(txs)/max(len(sigs),1):.0%}"
    m, wr = fx.analyze(P, txs, expected=len(txs), role_lookup=lookup)
    if not m:
        return None, "引擎拒绝"
    return m, None


def main():
    args = [a for a in sys.argv[1:]]
    files = [a for a in args if a.endswith(".txt")]
    start = 0
    for a in args:
        if a.isdigit():
            start = int(a)
    pools = []
    for f in files:
        pools += [l.strip() for l in Path(f).read_text().splitlines()
                  if len(l.strip()) > 30]
    if not pools:
        c = db.conn()
        pools = [r["pool"] for r in c.execute("SELECT DISTINCT pool FROM snapshots")]
    pools = list(dict.fromkeys(pools))[start:]
    reg.init()
    if not OUT.exists():
        OUT.write_text(COLS + "\n", encoding="utf-8")
    done = set()
    for l in OUT.read_text(encoding="utf-8").splitlines()[1:]:
        done.add(l.split(",")[0])
    todo = [p for p in pools if p not in done]
    print(f"待分析 {len(todo)} 个 (已完成 {len(done)})", flush=True)

    ok = bad = 0
    t0 = time.time()
    for i, P in enumerate(todo, 1):
        try:
            m, err = one_pool(P)
        except Exception as e:
            m, err = None, f"{type(e).__name__}"
        if not m:
            bad += 1
            print(f"  [{i}/{len(todo)}] {P[:10]}.. 跳过: {err}", flush=True)
            continue
        ok += 1
        row = [P, m["op_cost_usd"], m["op_out_usd"], m["op_pnl_usd"], m["op_gas_usd"],
               m["fish_in_usd"], m["fish_n"], m["n_wallet"], m["n_tx"],
               m["top_share"], m["life_min"], m["ratchet_usd_min"],
               m.get("init_reserve_usd", 0), m["peak_res_usd"], m["drained_usd"],
               m["dump_sec"] or 0, m.get("data_quality", "?"),
               m.get("attribution_ok", 1), m["outcome"]]
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(",".join(str(x) for x in row) + "\n")
        el = (time.time() - t0) / 60
        print(f"  [{i}/{len(todo)}] {P[:10]}.. 狗庄${m['op_cost_usd']:>9,.0f} "
              f"鱼${m['fish_in_usd']:>9,.0f} 盈亏${m['op_pnl_usd']:>+9,.0f} "
              f"{m['outcome']:<10} ({el:.0f}分钟)", flush=True)
    print(f"\n完成 {ok} 个, 跳过 {bad} 个")


if __name__ == "__main__":
    main()
