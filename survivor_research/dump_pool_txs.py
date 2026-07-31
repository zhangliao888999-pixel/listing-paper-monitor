# -*- coding: utf-8 -*-
"""2026-07-31新建: 把一个池子从出生到死亡的**全部**链上交易拉下来。

为什么必须做这个: 用户质疑"前期哪来那么多鱼"。链上核对已证实砸盘时确实有
493 SOL离开池子(GT估值误差只有1%),所以钱是真的。但关键问题变成了——
**这493 SOL是谁存进去的?**

狗庄完全可以自己拿SOL反复对倒把价格拉高,SOL就沉在池子里,最后砸盘时连本
带利提走。如果是这样,他的真实利润 = 提走的SOL - 他自己存进去的SOL,可能
远小于$36K。GT只给最近300笔成交,覆盖不到撒饵阶段,所以只能拉全量链上数据。

做法:
  1. getSignaturesForAddress(池子地址) 分页拉全部签名(每页上限1000)
  2. 逐笔 getTransaction, 提取每个钱包的 SOL 净变化和代币净变化
  3. 落盘成 jsonl, 断点续传(公共RPC会限流,跑一半断了不用重来)

用法: python dump_pool_txs.py <pool_or_mint_addr> [--sigs-only]
输出: txs_<addr前8位>.jsonl
"""
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

HERE = Path(__file__).parent
RPCS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana",
]
WSOL = "So11111111111111111111111111111111111111112"

_lock = threading.Lock()
_rpc_i = 0
_sessions = {}


def _sess():
    tid = threading.get_ident()
    if tid not in _sessions:
        _sessions[tid] = requests.Session()
    return _sessions[tid]


def rpc(method, params, tries=6):
    """轮换多个公共RPC,被限流就换一个再退避重试。"""
    global _rpc_i
    for i in range(tries):
        with _lock:
            url = RPCS[_rpc_i % len(RPCS)]
            _rpc_i += 1
        try:
            r = _sess().post(url, json={"jsonrpc": "2.0", "id": 1,
                                        "method": method, "params": params}, timeout=30)
            if r.status_code == 200:
                j = r.json()
                if "result" in j:
                    return j["result"]
                # RPC层面的错误(比如节点没这笔历史),换个节点试
            elif r.status_code in (429, 503):
                time.sleep(0.6 * (i + 1))
                continue
        except requests.RequestException:
            pass
        time.sleep(0.4 * (i + 1))
    return None


def fetch_signatures(addr, out_f):
    """分页拉全部签名。返回按时间正序的列表。"""
    if out_f.exists():
        sigs = json.loads(out_f.read_text())
        print(f"复用已有签名列表: {len(sigs)}笔")
        return sigs
    sigs, before = [], None
    while True:
        p = {"limit": 1000}
        if before:
            p["before"] = before
        res = rpc("getSignaturesForAddress", [addr, p])
        if not res:
            break
        for s in res:
            sigs.append({"sig": s["signature"], "ts": s.get("blockTime"),
                         "err": s.get("err") is not None})
        print(f"  已拉 {len(sigs)} 笔签名...", flush=True)
        if len(res) < 1000:
            break
        before = res[-1]["signature"]
        time.sleep(0.2)
    sigs.reverse()          # 转成时间正序
    out_f.write_text(json.dumps(sigs))
    return sigs


def parse_tx(sig_rec):
    """拉一笔交易,提取: 每个账户的SOL变化 + 目标代币变化。"""
    res = rpc("getTransaction", [sig_rec["sig"],
                                 {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}])
    if not res:
        return None
    meta = res.get("meta") or {}
    if meta.get("err"):
        return None
    msg = (res.get("transaction") or {}).get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in (msg.get("accountKeys") or [])]
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    sol = {}
    for i, k in enumerate(keys):
        if i < len(pre) and i < len(post):
            d = (post[i] - pre[i]) / 1e9
            if abs(d) > 1e-9:
                sol[k] = round(d, 9)
    # 代币余额变化(按owner汇总)
    tok = {}
    for arr, sign in ((meta.get("preTokenBalances") or [], -1),
                      (meta.get("postTokenBalances") or [], 1)):
        for b in arr:
            o = b.get("owner")
            amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            if o:
                tok[(o, b.get("mint"))] = tok.get((o, b.get("mint")), 0.0) + sign * amt
    return {
        "sig": sig_rec["sig"],
        "ts": sig_rec["ts"],
        "signer": keys[0] if keys else None,
        "fee": (meta.get("fee") or 0) / 1e9,
        "sol": sol,
        "tok": {f"{o}|{m}": round(v, 6) for (o, m), v in tok.items() if abs(v) > 1e-9},
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    addr = sys.argv[1]
    tag = addr[:8]
    sig_f = HERE / f"sigs_{tag}.json"
    out_f = HERE / f"txs_{tag}.jsonl"

    print(f"目标: {addr}")
    print("第1步: 拉全部交易签名")
    sigs = fetch_signatures(addr, sig_f)
    ok = [s for s in sigs if not s["err"]]
    if sigs and sigs[0]["ts"] and sigs[-1]["ts"]:
        span = (sigs[-1]["ts"] - sigs[0]["ts"]) / 60
        print(f"  总签名 {len(sigs)} 笔(成功{len(ok)}),跨度 {span:.0f} 分钟")

    if "--sigs-only" in sys.argv:
        return

    done = set()
    if out_f.exists():
        with out_f.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["sig"])
                except (json.JSONDecodeError, KeyError):
                    pass
        print(f"  断点续传: 已有 {len(done)} 笔")

    todo = [s for s in ok if s["sig"] not in done]
    print(f"第2步: 拉 {len(todo)} 笔交易明细")
    n = [0]
    t0 = time.time()
    fh = out_f.open("a", encoding="utf-8")
    workers = int(os.environ.get("TX_WORKERS", "6"))

    def work(s):
        r = parse_tx(s)
        with _lock:
            n[0] += 1
            if r:
                fh.write(json.dumps(r) + "\n")
            if n[0] % 200 == 0:
                el = time.time() - t0
                rate = n[0] / max(el, 1)
                eta = (len(todo) - n[0]) / max(rate, 0.01) / 60
                print(f"  {n[0]}/{len(todo)}  {rate:.1f}笔/秒  剩余约{eta:.0f}分钟", flush=True)
                fh.flush()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, todo))
    fh.close()
    print(f"完成,输出 {out_f.name}")


if __name__ == "__main__":
    main()
