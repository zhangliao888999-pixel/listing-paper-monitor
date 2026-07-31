# -*- coding: utf-8 -*-
"""2026-07-31新建: 查清楚池子开盘那672 SOL($50K)是谁存进去的。

储备曲线显示: AMM池子在00:17诞生时就已经有672 SOL,146分钟只净流入274 SOL,
最后1分钟被抽走792 SOL。所以"操盘方赚了多少"完全取决于那672 SOL的来源:
  - 如果是狗庄自己铺的底 -> 792里有672是本金回流,真实利润约$20K
  - 如果是pump.fun联合曲线毕业迁移过来的(即前期真有人买) -> 那才是鱼的钱

pump.fun的币走两个阶段: 先在bonding curve上交易,募满后"毕业"迁移到AMM池子。
所以要看的是**毕业前**那一段——bonding curve账户的历史,以及迁移那笔交易。

用法: python pool_origin.py
"""
import json
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
RPCS = ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"]
WSOL = "So11111111111111111111111111111111111111112"
MINT = "B2H2TaQoDgQvNsnRd5X4p3hW819dXVL3RGaSpZBdpump"
_i = [0]


def rpc(method, params, tries=5):
    for k in range(tries):
        url = RPCS[_i[0] % len(RPCS)]; _i[0] += 1
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                         "method": method, "params": params}, timeout=25)
            if r.status_code == 200 and "result" in r.json():
                return r.json()["result"]
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.4 * (k + 1))
    return None


def show(sig, label):
    res = rpc("getTransaction", [sig, {"maxSupportedTransactionVersion": 0,
                                       "encoding": "jsonParsed"}])
    if not res:
        print(f"  {label}: 拉不到")
        return
    meta, msg = res["meta"], res["transaction"]["message"]
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
    print(f"\n  {label}  sig={sig[:20]}..")
    print(f"    发起人(signer): {keys[0] if keys else '?'}")
    # 调用了哪些程序
    progs = []
    for ins in (msg.get("instructions") or []):
        p = ins.get("program") or ins.get("programId")
        if p and p not in progs:
            progs.append(p)
    print(f"    调用程序: {', '.join(str(p)[:44] for p in progs[:4])}")
    # 日志里的指令名
    logs = [l for l in (meta.get("logMessages") or []) if "Instruction:" in l]
    if logs:
        print(f"    指令: {' | '.join(l.split('Instruction:')[-1].strip() for l in logs[:6])}")
    # 大额SOL/WSOL变动
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    moves = []
    for i, k in enumerate(keys):
        if i < len(pre) and i < len(post):
            d = (post[i] - pre[i]) / 1e9
            if abs(d) > 0.5:
                moves.append((d, k))
    for b_pre, b_post in [((meta.get("preTokenBalances") or []), (meta.get("postTokenBalances") or []))]:
        pm = {b.get("accountIndex"): float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
              for b in b_pre if b.get("mint") == WSOL}
        for b in b_post:
            if b.get("mint") != WSOL:
                continue
            idx = b.get("accountIndex")
            d = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0) - pm.get(idx, 0)
            if abs(d) > 0.5:
                moves.append((d, "WSOL:" + (keys[idx] if idx < len(keys) else "?")))
    if moves:
        print("    大额SOL流动:")
        for d, k in sorted(moves, reverse=True):
            print(f"      {d:>+10.2f} SOL   {k[:52]}")


def main():
    sigs = json.loads((HERE / "sigs_BFBM1Nqj.json").read_text())
    ok = [s for s in sigs if not s["err"]]
    print(f"池子共{len(ok)}笔成功交易,看最早的3笔(池子是怎么诞生的):")
    for s in ok[:3]:
        show(s["sig"], f"第{ok.index(s)+1}笔 ts={s['ts']}")

    # bonding curve阶段: 直接查mint的历史,毕业前的交易不经过AMM池子
    print("\n" + "=" * 70)
    print("bonding curve阶段(毕业前) —— 看币刚发出来时有没有人真买")
    print("=" * 70)
    msigs = rpc("getSignaturesForAddress", [MINT, {"limit": 1000}])
    if msigs:
        ts = [m["blockTime"] for m in msigs if m.get("blockTime")]
        print(f"  mint地址上的交易: {len(msigs)}笔")
        if ts:
            print(f"  时间跨度: {min(ts)} -> {max(ts)}  ({(max(ts)-min(ts))/60:.0f}分钟)")
            print(f"  池子诞生于: {ok[0]['ts']}  (mint最早交易早了 {(ok[0]['ts']-min(ts))/60:.0f} 分钟)")
        show(msigs[-1]["signature"], "mint最早的一笔(发币)")


if __name__ == "__main__":
    main()
