# -*- coding: utf-8 -*-
"""2026-07-31新建: 持续盯一个钓鱼盘,等它的结局。

DISNEY(BYJV6ia1..)这个盘的状态: 操盘方一个人打了4.5小时,236笔买入/2笔卖出,
花了$2,244,外部钱包投进来 $0.00,手里压着5.946亿个币(占供应59.5%)。
他不动在烧gas,动了就是砸自己的池子,只能等鱼。

值得盯的原因: 这是一个正在进行中的、已知全部底牌的样本。我们能亲眼看到
  - 鱼到底会不会来? 来了多大?
  - 他等多久放弃? 放弃时是砸盘还是直接弃盘?
  - 从"有人上钩"到"砸盘"隔多少秒?(实盘那几笔是1秒,这里能验证)
这些是回测数据里看不出来的,只能实时抓。

关键告警: **出现新的签名钱包并且真的买了** —— 那就是鱼上钩的瞬间。
之后每一笔都要记下来,尤其是操盘方的第一笔卖出。

用法: python track_bait.py <pool> [间隔秒,默认300]
输出: track_<tag>.jsonl 逐次快照;控制台只在有变化时打印
"""
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent
RPCS = ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"]
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
_i = [0]


def rpc(method, params, tries=4):
    for k in range(tries):
        url = RPCS[_i[0] % len(RPCS)]; _i[0] += 1
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                         "method": method, "params": params}, timeout=25)
            if r.status_code == 200 and "result" in r.json():
                return r.json()["result"]
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.5 * (k + 1))
    return None


def now():
    return datetime.now(timezone.utc).strftime("%m-%d %H:%M:%S")


def new_sigs(pool, until_sig):
    """拉取比 until_sig 更新的签名(时间正序)。"""
    out, before = [], None
    while True:
        p = {"limit": 1000}
        if before:
            p["before"] = before
        res = rpc("getSignaturesForAddress", [pool, p])
        if not res:
            break
        hit = False
        for s in res:
            if s["signature"] == until_sig:
                hit = True
                break
            out.append({"sig": s["signature"], "ts": s.get("blockTime"),
                        "err": s.get("err") is not None})
        if hit or len(res) < 1000:
            break
        before = res[-1]["signature"]
    out.reverse()
    return out


def detail(sig):
    return rpc("getTransaction", [sig, {"maxSupportedTransactionVersion": 0,
                                        "encoding": "jsonParsed"}])


def digest(res):
    """一笔交易 -> (signer, {mint: 该signer的净变化}, 原生SOL变化)"""
    meta = res.get("meta") or {}
    msg = (res.get("transaction") or {}).get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in (msg.get("accountKeys") or [])]
    signer = keys[0] if keys else None
    pre = {b.get("accountIndex"): float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
           for b in (meta.get("preTokenBalances") or [])}
    flows = defaultdict(float)
    for b in (meta.get("postTokenBalances") or []):
        if b.get("owner") != signer:
            continue
        v = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        flows[b.get("mint")] += v - pre.get(b.get("accountIndex"), 0.0)
    sol = 0.0
    p0, p1 = meta.get("preBalances") or [], meta.get("postBalances") or []
    if p0 and p1:
        sol = (p1[0] - p0[0]) / 1e9
    return signer, dict(flows), sol


def main():
    pool = sys.argv[1] if len(sys.argv) > 1 else "BYJV6ia1Z1nYYY18id46Qrtkm55h35sUeqWv9WPcSy7R"
    gap = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    tag = pool[:8]
    out_f = HERE / f"track_{tag}.jsonl"

    # 用尸检时拉到的全量数据做基线,知道哪些钱包是"老面孔"
    known = set()
    last_sig = None
    base_f = HERE / f"autopsy_{tag}.jsonl"
    if base_f.exists():
        rows = []
        with base_f.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        rows.sort(key=lambda x: x.get("ts") or 0)
        known = {r["signer"] for r in rows if r.get("signer")}
        last_sig = rows[-1]["sig"] if rows else None
    # 没有基线时第一轮只做种子,不告警。否则盯一个有2000+买家的池子会把
    # 每个钱包都当成"新面孔"刷屏,真正的信号反而被淹掉。
    seeding = not known
    print(f"盯盘启动 {pool}")
    print(f"  已知钱包 {len(known)} 个, 每 {gap}s 检查一次"
          f"{'  (第一轮建基线,不告警)' if seeding else ''}")
    print(f"  告警: 新钱包真实买入 / 大额卖出 / 盘停\n", flush=True)

    tick = 0
    while True:
        tick += 1
        try:
            fresh = new_sigs(pool, last_sig)
        except Exception as e:                      # 网络抖动不该让盯盘停掉
            print(f"[{now()}] 拉签名失败: {e}", flush=True)
            time.sleep(gap)
            continue
        fresh = [s for s in fresh if not s["err"]]
        if fresh:
            last_sig = fresh[-1]["sig"]
        newcomers, op_sells, buys_usd = [], [], 0.0
        for s in fresh:
            res = detail(s["sig"])
            if not res or (res.get("meta") or {}).get("err"):
                continue
            signer, flows, sol = digest(res)
            q = flows.get(USDC, 0.0) or flows.get(WSOL, 0.0) * 75
            if signer and signer not in known:
                known.add(signer)
                if abs(q) > 0.01 and not seeding:
                    newcomers.append((signer, q, s["ts"]))
            if q < 0:
                buys_usd += -q
            elif q > 50.0 and not seeding:
                op_sells.append((signer, q, s["ts"]))
        if seeding:
            print(f"[{now()}] 基线建立: {len(fresh)}笔, {len(known)}个钱包, "
                  f"下一轮开始告警", flush=True)
            seeding = False
            time.sleep(gap)
            continue

        rec = {"t": now(), "new_tx": len(fresh), "buys_usd": round(buys_usd, 2),
               "newcomers": [[w, round(v, 2)] for w, v, _ in newcomers],
               "sells": [[w[:12], round(v, 2)] for w, v, _ in op_sells],
               "wallets": len(known)}
        with out_f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

        if newcomers:
            print(f"\n{'*'*66}")
            print(f"[{now()}] *** 鱼上钩了 *** 出现 {len(newcomers)} 个新钱包")
            for w, v, ts in newcomers:
                act = "买入" if v < 0 else "卖出"
                print(f"    {w}  {act} ${abs(v):,.2f}")
            print(f"{'*'*66}\n", flush=True)
        if op_sells:
            print(f"[{now()}] !! 有卖出 {len(op_sells)}笔 共 ${sum(v for _,v,_ in op_sells):,.2f}", flush=True)
        if fresh and not newcomers and not op_sells:
            print(f"[{now()}] +{len(fresh)}笔 买入${buys_usd:,.2f} "
                  f"(还是他一个人在撒饵, 累计钱包{len(known)}个)", flush=True)
        elif not fresh:
            print(f"[{now()}] 无新交易 —— 盘已停", flush=True)
        time.sleep(gap)


if __name__ == "__main__":
    main()
