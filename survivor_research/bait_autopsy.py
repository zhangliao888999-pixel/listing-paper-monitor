# -*- coding: utf-8 -*-
"""2026-07-31新建: 钓鱼币尸检——一条命令算清一个池子里狗庄的完整资金账。

背景: 分析USOH时发现,光看GT的成交额会得出"狗庄赚$36,655"的错误结论。真相是
他在毕业那一刻自己打了600 SOL($45,000)进池子铺底,砸盘抽走的792 SOL里绝大
部分是自己的本金回流。判断一个钓鱼盘赚没赚,必须分清:

  A. 狗庄自己注入的  —— 他的本金/风险敞口
  B. 外人真正投进来的 —— 鱼的钱
  C. 他抽走的        —— 出金
  真实盈亏 = C − A − 手续费

踩过的两个坑,都写进代码里了:
  1. 金库不能按"余额波动最大"认——那会选中操盘方自己的钱包(它每笔都在花钱)。
     金库是被动收钱的,永远不是交易发起人,用这个排除。
  2. 计价币不一定是SOL。DISNEY/USDC 这种 Meteora 池子以USDC计价,只抓WSOL的
     话算出来"狗庄只花了$79",那其实全是gas,真实买卖一笔没抓到。所以不预设
     计价币,把所有mint的变化都收下来,再判断哪个是计价币。

用法: python bait_autopsy.py <pool_addr> [--sample] [--refetch]
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
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_PRICE = {WSOL: 75.0, USDC: 1.0, USDT: 1.0}   # 计价币的USD单价
QUOTE_NAME = {WSOL: "SOL", USDC: "USDC", USDT: "USDT"}
SOL_USD = 75.0
FULL_MAX = 3000
_lock = threading.Lock()
_i = [0]


def rpc(method, params, tries=6):
    for k in range(tries):
        with _lock:
            url = RPCS[_i[0] % len(RPCS)]; _i[0] += 1
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                         "method": method, "params": params}, timeout=30)
            if r.status_code == 200:
                j = r.json()
                if "result" in j:
                    return j["result"]
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.4 * (k + 1))
    return None


def hhmm(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M")


def get_sigs(addr, cache):
    if cache.exists():
        return json.loads(cache.read_text())
    sigs, before = [], None
    while True:
        p = {"limit": 1000}
        if before:
            p["before"] = before
        res = rpc("getSignaturesForAddress", [addr, p])
        if not res:
            break
        sigs += [{"sig": s["signature"], "ts": s.get("blockTime"),
                  "err": s.get("err") is not None} for s in res]
        print(f"    已拉 {len(sigs)} 笔签名...", flush=True)
        if len(res) < 1000:
            break
        before = res[-1]["signature"]
        time.sleep(0.15)
    sigs.reverse()
    cache.write_text(json.dumps(sigs))
    return sigs


def parse(sig_rec):
    """一笔交易 -> 原生SOL变化 + 所有代币的持有人级变化。"""
    res = rpc("getTransaction", [sig_rec["sig"],
                                 {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}])
    if not res:
        return None
    meta = res.get("meta") or {}
    msg = (res.get("transaction") or {}).get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in (msg.get("accountKeys") or [])]
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    bal, delta = {}, {}
    for i, k in enumerate(keys):
        if i < len(pre) and i < len(post):
            bal[k] = post[i] / 1e9
            d = (post[i] - pre[i]) / 1e9
            if abs(d) > 1e-9:
                delta[k] = d
    pm = {b.get("accountIndex"): float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
          for b in (meta.get("preTokenBalances") or [])}
    tok, tokbal = {}, {}
    for b in (meta.get("postTokenBalances") or []):
        idx, mint, owner = b.get("accountIndex"), b.get("mint"), b.get("owner")
        v = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        acct = keys[idx] if idx is not None and idx < len(keys) else str(owner)
        tokbal[f"{acct}|{mint}"] = v
        d = v - pm.get(idx, 0.0)
        if abs(d) > 1e-9 and owner:
            k = f"{owner}|{mint}"
            tok[k] = tok.get(k, 0.0) + d
    return {"sig": sig_rec["sig"], "ts": sig_rec["ts"], "signer": keys[0] if keys else None,
            "fee": (meta.get("fee") or 0) / 1e9, "bal": bal, "delta": delta,
            "tok": tok, "tokbal": tokbal,
            "ins": [l.split("Instruction:")[-1].strip()
                    for l in (meta.get("logMessages") or []) if "Instruction:" in l],
            "err": bool(meta.get("err"))}


def load_txs(todo, cache, refetch):
    """带落盘缓存的批量拉取,避免每次调参都重拉一遍。"""
    have = {}
    if cache.exists() and not refetch:
        with cache.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    t = json.loads(line)
                    have[t["sig"]] = t
                except (json.JSONDecodeError, KeyError):
                    pass
        print(f"    缓存命中 {len(have)} 笔")
    need = [s for s in todo if s["sig"] not in have]
    if need:
        print(f"    需拉取 {len(need)} 笔", flush=True)
        n = [0]
        fh = cache.open("a", encoding="utf-8")
        def work(s):
            r = parse(s)
            with _lock:
                n[0] += 1
                if r:
                    have[r["sig"]] = r
                    fh.write(json.dumps(r) + "\n")
                if n[0] % 100 == 0:
                    print(f"    {n[0]}/{len(need)}", flush=True)
            return r
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(work, need))
        fh.close()
    out = [have[s["sig"]] for s in todo if s["sig"] in have and not have[s["sig"]].get("err")]
    out.sort(key=lambda x: x["ts"])
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    pool = sys.argv[1]
    tag = pool[:8]
    refetch = "--refetch" in sys.argv

    print("=" * 78)
    print(f"钓鱼币尸检: {pool}")
    print("=" * 78)

    print("\n[1/4] 拉取链上签名")
    sigs = get_sigs(pool, HERE / f"sigs_{tag}.json")
    ok = [s for s in sigs if not s["err"] and s.get("ts")]
    if not ok:
        print("  拿不到交易"); return
    life = (ok[-1]["ts"] - ok[0]["ts"]) / 60
    idle = (time.time() - ok[-1]["ts"]) / 60
    print(f"  成功交易 {len(ok)} 笔 (含失败共{len(sigs)}笔)")
    print(f"  诞生 {hhmm(ok[0]['ts'])}   最后一笔 {hhmm(ok[-1]['ts'])}")
    print(f"  存续 {life:.0f}分钟({life/60:.1f}小时)   距今静止 {idle:.0f}分钟")

    full = len(ok) <= FULL_MAX and "--sample" not in sys.argv
    todo = ok if full else ok[::max(len(ok)//150, 1)] + [ok[-1]]
    print(f"\n[2/4] 拉取{'全量' if full else '抽样'} {len(todo)} 笔明细")
    txs = load_txs(todo, HERE / f"autopsy_{tag}.jsonl", refetch)
    print(f"  可用 {len(txs)} 笔")
    if not txs:
        return

    # ---- 认计价币: 不预设SOL,看哪个已知计价币出现最多 ----
    mint_hits = defaultdict(int)
    for t in txs:
        for k in t["tok"]:
            mint_hits[k.split("|")[1]] += 1
    quote = max(QUOTE_PRICE, key=lambda m: mint_hits.get(m, 0))
    if mint_hits.get(quote, 0) < len(txs) * 0.2:
        quote = WSOL     # 没有代币计价痕迹,按原生SOL走
    px, qn = QUOTE_PRICE[quote], QUOTE_NAME[quote]
    base = max((m for m in mint_hits if m not in QUOTE_PRICE),
               key=lambda m: mint_hits[m], default="?")
    print(f"\n  计价币: {qn} ({quote[:12]}..)  出现在 {mint_hits.get(quote,0)}/{len(txs)} 笔")
    print(f"  标的币: {base}")

    signers = {t["signer"] for t in txs if t["signer"]}

    # ---- [3/4] 池子计价币储备曲线 ----
    print(f"\n[3/4] 池子{qn}储备曲线")
    lo, hi, seen = {}, {}, defaultdict(int)
    for t in txs:
        for k, v in t["tokbal"].items():
            acct, m = k.split("|")
            if m != quote or acct in signers:
                continue
            seen[acct] += 1
            lo[acct] = min(lo.get(acct, v), v); hi[acct] = max(hi.get(acct, v), v)
    cands = sorted(((hi[a] - lo[a], a) for a in seen if seen[a] > len(txs) * 0.5), reverse=True)
    series = []
    if cands:
        vault = cands[0][1]
        print(f"  金库 {vault}  ({lo[vault]:.2f} -> {hi[vault]:.2f} {qn})")
        series = [(t["ts"], t["tokbal"][f"{vault}|{quote}"])
                  for t in txs if f"{vault}|{quote}" in t["tokbal"]]
    if series:
        t0, peak = series[0][0], max(v for _, v in series)
        show = series if len(series) <= 45 else series[::len(series)//40]
        print(f"  {'时刻':>12} {'距开盘':>7} {'池内':>11} {'折USD':>10}  走势")
        for ts, v in show:
            print(f"  {hhmm(ts):>12} {(ts-t0)/60:>6.0f}分 {v:>9.2f}{qn:>2} {v*px:>10,.0f}  "
                  f"{'#'*int(v/max(peak,.001)*38)}")
        pk_ts, pk_v = max(series, key=lambda x: x[1])
        end_v = series[-1][1]
    else:
        print("  认不出金库(池子可能用虚拟储备)")
        pk_v = end_v = pk_ts = 0

    # ---- [4/4] 钱包级资金账(计价币口径 + gas) ----
    print(f"\n[4/4] 资金账 —— 每个钱包真金白银进出多少")
    print("=" * 78)
    qflow, gas = defaultdict(float), defaultdict(float)
    nb, ns = defaultdict(int), defaultdict(int)
    first, last = {}, {}
    for t in txs:
        s = t["signer"]
        if s:
            first.setdefault(s, t["ts"]); last[s] = t["ts"]
            gas[s] += t["fee"]
        for k, v in t["tok"].items():
            owner, m = k.split("|")
            if m != quote:
                continue
            qflow[owner] += v
            if v < 0:
                nb[owner] += 1
            else:
                ns[owner] += 1
    if quote == WSOL:      # 原生SOL计价时,交易额体现在lamport变化上
        for t in txs:
            s = t["signer"]
            if s and not t["tok"]:
                qflow[s] += t["delta"].get(s, 0.0) + t["fee"]

    inflow = sum(-v for v in qflow.values() if v < 0)
    outflow = sum(v for v in qflow.values() if v > 0)
    print(f"  参与钱包 {len(qflow)} 个")
    print(f"    净投入 {inflow:>10.2f} {qn}  (${inflow*px:>10,.0f})")
    print(f"    净提走 {outflow:>10.2f} {qn}  (${outflow*px:>10,.0f})")
    print(f"\n  {'钱包':<14}{'净'+qn:>11}{'折USD':>10}{'买':>5}{'卖':>5}{'gas(SOL)':>10}"
          f"  {'首次':>12}{'末次':>12}")
    for v, k in sorted((v, k) for k, v in qflow.items()):
        print(f"  {k[:12]:<14}{v:>11.3f}{v*px:>10,.0f}{nb[k]:>5}{ns[k]:>5}{gas.get(k,0):>10.4f}"
              f"  {hhmm(first.get(k,txs[0]['ts'])):>12}{hhmm(last.get(k,txs[-1]['ts'])):>12}")

    # ---- 结论: 操盘方 = 交易最多且净投入最大的钱包 ----
    op = min(qflow, key=lambda k: qflow[k])
    cost_q = -qflow[op]
    cost_gas = gas.get(op, 0.0)
    print()
    print("=" * 78)
    print("结论")
    print("=" * 78)
    print(f"  操盘方钱包: {op}")
    print(f"    砸进池子   {cost_q:>9.2f} {qn}   (${cost_q*px:>9,.0f})   买{nb[op]}笔 卖{ns[op]}笔")
    print(f"    gas/优先费 {cost_gas:>9.4f} SOL  (${cost_gas*SOL_USD:>9,.0f})")
    print(f"    合计成本                       ${cost_q*px + cost_gas*SOL_USD:>9,.0f}")
    others = [(v, k) for k, v in qflow.items() if k != op]
    fish_in = sum(-v for v, _ in others if v < 0)
    fish_out = sum(v for v, _ in others if v > 0)
    print(f"\n  其他{len(others)}个钱包(潜在的鱼):")
    print(f"    投进来 ${fish_in*px:>9,.0f}    拿走 ${fish_out*px:>9,.0f}")
    if pk_v:
        print(f"\n  池子峰值储备 {pk_v:.2f} {qn} (${pk_v*px:,.0f}) 在第{(pk_ts-series[0][0])/60:.0f}分钟")
        print(f"  池子当前储备 {end_v:.2f} {qn} (${end_v*px:,.0f})")
    print(f"  存续 {life/60:.1f}小时, 已静止 {idle:.0f}分钟")
    print()
    net = fish_in - cost_q                     # 鱼投进来的 减去 他自己砸的
    if net * px < 50:
        print(f"  *** 没钓到鱼 ***")
        print(f"  他花了 ${cost_q*px + cost_gas*SOL_USD:,.0f} 抬价,外面只进来 ${fish_in*px:,.0f}。")
        print(f"  想收网必须自己砸自己的池子,砸出来的还是自己的本金,还要再付一次手续费。")
    else:
        print(f"  鱼的净投入减去他的成本: ${net*px:,.0f}")


if __name__ == "__main__":
    main()
