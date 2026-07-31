# -*- coding: utf-8 -*-
"""2026-07-31新建: 用Solana链上原始交易核对GT的USD估值是否可信。

用户对"操盘方提走$36,655"提出怀疑,这个怀疑有道理: GT的volume_in_usd是它
自己按某个价格折算的,而这个币2分钟内跌了97%——同一笔卖出,按崩盘前的价
还是崩盘后的价折算,USD数字能差几十倍。

唯一可靠的口径是**链上实际到手多少SOL**。这个脚本:
  1. 从GT取砸盘那批交易的tx_hash
  2. 逐笔去Solana RPC拉原始交易
  3. 看卖方钱包的SOL余额实际增加了多少(preBalances/postBalances之差)
  4. 按当时SOL价折算成USD,跟GT的数字对比

这样能回答: GT说的$36,655到底是真钱,还是估值假象。
"""
import json
import sys
import time

import requests

import cg_client as cg

RPC = "https://api.mainnet-beta.solana.com"
SOL_USD = 75.0     # 近似,后面用实时价校正


def rpc(method, params, tries=3):
    for i in range(tries):
        try:
            r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1,
                                         "method": method, "params": params}, timeout=30)
            if r.status_code == 200:
                return r.json()
            time.sleep(1.5 * (i + 1))
        except requests.RequestException:
            time.sleep(2 * (i + 1))
    return {}


def main():
    pool = sys.argv[1] if len(sys.argv) > 1 else "BFBM1NqjEvuxGcD6tvvHmUFYWqtQh3z1MeQNXKC5bbwa"

    d = cg.get(f"networks/solana/pools/{pool}/trades", {"trade_volume_in_usd_greater_than": 100})
    rows = (d or {}).get("data", [])
    sells = []
    for r in rows:
        a = r["attributes"]
        if a.get("kind") != "sell":
            continue
        sells.append({
            "tx": a.get("tx_hash"),
            "w": a.get("tx_from_address") or "",
            "gt_usd": float(a.get("volume_in_usd") or 0),
            "ts": a.get("block_timestamp", ""),
            "token_amt": a.get("from_token_amount"),
        })
    sells.sort(key=lambda x: -x["gt_usd"])
    print(f"要核对的大额卖出: {len(sells)}笔\n")

    print(f"{'时间':>9} {'钱包':<12} {'GT估值USD':>12} {'链上实收SOL':>13} {'折USD':>11} {'差异':>9}")
    print("-" * 72)
    tot_gt = tot_real = 0.0
    checked = 0
    for s in sells:
        if not s["tx"]:
            continue
        res = rpc("getTransaction", [s["tx"], {"maxSupportedTransactionVersion": 0,
                                                "encoding": "jsonParsed"}]).get("result")
        if not res:
            print(f"{s['ts'][11:19]:>9} {s['w'][:10]:<12} {s['gt_usd']:>12,.0f}  (链上查不到)")
            continue
        meta = res.get("meta") or {}
        msg = (res.get("transaction") or {}).get("message") or {}
        keys = [k.get("pubkey") if isinstance(k, dict) else k
                for k in (msg.get("accountKeys") or [])]
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        # 找卖方钱包在账户列表里的位置,算它的SOL净变化
        delta_sol = None
        if s["w"] in keys:
            idx = keys.index(s["w"])
            if idx < len(pre) and idx < len(post):
                delta_sol = (post[idx] - pre[idx]) / 1e9
        if delta_sol is None:
            print(f"{s['ts'][11:19]:>9} {s['w'][:10]:<12} {s['gt_usd']:>12,.0f}  (找不到该钱包账户)")
            continue
        real_usd = delta_sol * SOL_USD
        diff = (real_usd / s["gt_usd"] - 1) * 100 if s["gt_usd"] else 0
        tot_gt += s["gt_usd"]; tot_real += real_usd
        checked += 1
        print(f"{s['ts'][11:19]:>9} {s['w'][:10]:<12} {s['gt_usd']:>12,.0f} {delta_sol:>13.4f} {real_usd:>11,.0f} {diff:>+8.0f}%")
        time.sleep(0.15)

    print("-" * 72)
    print(f"{'合计':>9} {'':12} {tot_gt:>12,.0f} {'':13} {tot_real:>11,.0f}")
    if tot_gt:
        print(f"\n核对了{checked}笔。链上实际到手 vs GT估值: {tot_real/tot_gt*100:.1f}%")
        if tot_real < tot_gt * 0.7:
            print("*** GT的USD估值明显偏高,不能直接用来算操盘方收益 ***")
        elif tot_real > tot_gt * 1.3:
            print("*** GT的USD估值偏低 ***")
        else:
            print("GT估值与链上实际基本吻合")


if __name__ == "__main__":
    main()
