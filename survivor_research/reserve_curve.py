# -*- coding: utf-8 -*-
"""2026-07-31新建: 快速画出池子的SOL储备曲线,验证"演了几小时没鱼上钩"。

用户的判断: 狗庄演了2.8小时才砸盘,恰恰说明前期没钓到鱼——真钓到了他早收
网了(用户自己实盘那几笔,狗庄5刀就砸了)。

这个假设有个非常干脆的可证伪预测:
  如果前期真有鱼 -> 池子SOL储备应该随时间稳步爬升
  如果前期没鱼   -> 储备长时间趴在低位,只在最后几分钟突然冲高

全量9623笔要跑1小时,但画曲线不需要全量。沿时间轴抽样200笔,读出池子金库
账户在那一刻的余额(postBalances),就能把曲线还原出来。

用法: python reserve_curve.py <sigs_xxx.json> [抽样数]
"""
import json
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent
RPCS = ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com",
        "https://rpc.ankr.com/solana"]
SOL_USD = 75.0
_lock = threading.Lock()
_i = [0]


def rpc(method, params, tries=5):
    for k in range(tries):
        with _lock:
            url = RPCS[_i[0] % len(RPCS)]; _i[0] += 1
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                         "method": method, "params": params}, timeout=25)
            if r.status_code == 200 and "result" in r.json():
                return r.json()["result"]
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.35 * (k + 1))
    return None


WSOL = "So11111111111111111111111111111111111111112"


def snapshot(sig_rec):
    """返回 (时间戳, {账户: 该刻SOL余额})

    注意: pumpswap这类AMM池子的SOL是以**WSOL代币**形式存放的,不在lamport
    余额里。第一版只看postBalances,结果找出来一个恒定1636 SOL的系统账户。
    所以这里两种都收: 原生lamport余额 + WSOL代币账户余额。
    """
    res = rpc("getTransaction", [sig_rec["sig"],
                                 {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}])
    if not res or (res.get("meta") or {}).get("err"):
        return None
    meta, msg = res["meta"], res["transaction"]["message"]
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in (msg.get("accountKeys") or [])]
    post = meta.get("postBalances") or []
    bal = {k: post[i] / 1e9 for i, k in enumerate(keys) if i < len(post)}
    for b in (meta.get("postTokenBalances") or []):
        if b.get("mint") != WSOL:
            continue
        idx = b.get("accountIndex")
        acct = keys[idx] if idx is not None and idx < len(keys) else b.get("owner")
        amt = (b.get("uiTokenAmount") or {}).get("uiAmount")
        if acct and amt:
            bal["WSOL:" + acct] = float(amt)
    return (sig_rec["ts"], bal)


def main():
    sf = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "sigs_BFBM1Nqj.json"
    n_sample = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    sigs = [s for s in json.loads(sf.read_text()) if s.get("ts") and not s["err"]]
    step = max(len(sigs) // n_sample, 1)
    picked = sigs[::step]
    print(f"总交易{len(sigs)}笔,抽样{len(picked)}笔还原储备曲线...", flush=True)

    snaps = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(snapshot, picked):
            if r:
                snaps.append(r)
    snaps.sort(key=lambda x: x[0])
    print(f"取到{len(snaps)}个快照\n")

    # 找金库: 必须是余额**在变动**的大账户。
    # 只按平均余额排会选中恒定不动的系统账户(第一版就踩了这个坑),所以按
    # 波动幅度(max-min)排序,金库随每笔买卖进出,波动必然最大。
    seen, lo, hi = defaultdict(int), {}, {}
    for _, bal in snaps:
        for a, v in bal.items():
            seen[a] += 1
            lo[a] = min(lo.get(a, v), v); hi[a] = max(hi.get(a, v), v)
    cands = [(hi[a] - lo[a], a) for a in seen if seen[a] > len(snaps) * 0.5]
    cands.sort(reverse=True)
    print("候选金库(按余额波动幅度):")
    for rng, a in cands[:5]:
        print(f"   {a[:50]:<52} 波动{rng:>9.1f} SOL  ({lo[a]:.1f} -> {hi[a]:.1f})")
    vault = cands[0][1]
    print(f"\n选定金库: {vault}\n")

    t0 = snaps[0][0]
    print("=" * 74)
    print("池子SOL储备随时间变化")
    print("=" * 74)
    series = [(ts, bal.get(vault, 0)) for ts, bal in snaps if vault in bal]
    peak = max(v for _, v in series)
    print(f"{'时刻':>7} {'距开盘':>7} {'池内SOL':>9} {'折USD':>9}  储备走势(峰值={peak:.0f} SOL)")
    for ts, v in series:
        hm = datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")
        bar = "#" * int(v / max(peak, 0.01) * 46)
        print(f"{hm:>9} {(ts-t0)/60:>6.0f}分 {v:>9.1f} {v*SOL_USD:>9,.0f}  {bar}")

    pk_ts, pk_v = max(series, key=lambda x: x[1])
    end_v = series[-1][1]
    print()
    print("=" * 74)
    print(f"  峰值储备 {pk_v:.1f} SOL (${pk_v*SOL_USD:,.0f}) 出现在第 {(pk_ts-t0)/60:.0f} 分钟")
    print(f"  最终储备 {end_v:.1f} SOL (${end_v*SOL_USD:,.0f})")
    print(f"  被抽走   {pk_v-end_v:.1f} SOL (${(pk_v-end_v)*SOL_USD:,.0f})")
    # 前80%时间的储备水平 vs 峰值
    cut = t0 + (series[-1][0] - t0) * 0.8
    early = [v for ts, v in series if ts <= cut]
    if early:
        print(f"\n  前80%时间的储备: 中位 {sorted(early)[len(early)//2]:.1f} SOL, "
              f"最高 {max(early):.1f} SOL")
        print(f"  = 峰值的 {max(early)/max(pk_v,0.01)*100:.0f}%")


if __name__ == "__main__":
    main()
