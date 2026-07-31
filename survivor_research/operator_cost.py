# -*- coding: utf-8 -*-
"""2026-07-31新建: 算清操盘方的真实成本和现在被套住的仓位。

为什么单独写这个: bait_autopsy 按"代币持有人"汇总资金流,结果把池子PDA、
路由账户、聚合器的中转账户全算成了"钱包",出现 -7,490 USDC 这种莫名其妙的
条目。真正的交易参与者只有**签过名的钱包**——PDA不会签名。

所以这里只认签名钱包,对每一个算:
  - 计价币(USDC/SOL)净流出入 = 他真金白银投了多少
  - 原生SOL余额首末差 = gas + Jito小费的真实消耗(meta.fee抓不到Jito小费,
    那是普通转账,GbTRN4aKUdaA 的 meta.fee 只有0.0024 SOL,实际烧了1.055)
  - 现在还持有多少标的币 = 被套的仓位

用法: python operator_cost.py <pool_addr>
"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PX = {WSOL: 75.0, USDC: 1.0}
NAME = {WSOL: "SOL", USDC: "USDC"}
SOL_USD = 75.0
RPC = "https://api.mainnet-beta.solana.com"


def rpc(method, params):
    try:
        r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1,
                                     "method": method, "params": params}, timeout=25)
        return r.json().get("result") if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def hhmm(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M")


def main():
    pool = sys.argv[1]
    tag = pool[:8]
    txs = []
    with (HERE / f"autopsy_{tag}.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            try:
                t = json.loads(line)
                if not t.get("err") and t.get("ts"):
                    txs.append(t)
            except json.JSONDecodeError:
                pass
    txs.sort(key=lambda x: x["ts"])
    signers = {t["signer"] for t in txs if t["signer"]}
    print(f"全量 {len(txs)} 笔交易, 签名钱包 {len(signers)} 个\n")

    # 认计价币和标的币
    hits = defaultdict(int)
    for t in txs:
        for k in t["tok"]:
            hits[k.split("|")[1]] += 1
    quote = max(PX, key=lambda m: hits.get(m, 0))
    base = max((m for m in hits if m not in PX), key=lambda m: hits[m], default=None)
    qn, px = NAME[quote], PX[quote]
    print(f"计价币 {qn}   标的币 {base}\n")

    print("=" * 86)
    print("只看签名钱包 —— 这些才是真实的交易参与者")
    print("=" * 86)
    print(f"  {'钱包':<14}{'笔数':>5}{'买':>5}{'卖':>5}{'净'+qn:>12}{'折USD':>10}"
          f"{'SOL净烧':>10}{'烧$':>7}  {'活动时段':>24}")
    rows = []
    for s in sorted(signers):
        mine = [t for t in txs if t["signer"] == s]
        q = sum(v for t in mine for k, v in t["tok"].items()
                if k == f"{s}|{quote}")
        b = sum(1 for t in mine if t["tok"].get(f"{s}|{quote}", 0) < 0)
        sl = sum(1 for t in mine if t["tok"].get(f"{s}|{quote}", 0) > 0)
        bals = [t["bal"][s] for t in mine if s in t["bal"]]
        burn = (bals[0] - bals[-1]) if len(bals) >= 2 else 0.0
        tok_now = 0.0
        for t in reversed(mine):
            hit = [v for k, v in t["tokbal"].items() if k.endswith("|" + str(base))]
            if hit:
                tok_now = max(hit)
                break
        rows.append((s, len(mine), b, sl, q, burn, tok_now, mine[0]["ts"], mine[-1]["ts"]))
        print(f"  {s[:12]:<14}{len(mine):>5}{b:>5}{sl:>5}{q:>12.2f}{q*px:>10,.0f}"
              f"{burn:>10.4f}{burn*SOL_USD:>7,.0f}  {hhmm(mine[0]['ts'])} -> {hhmm(mine[-1]['ts'])}")

    op = min(rows, key=lambda r: r[4])
    s, n, b, sl, q, burn, tok_now, t_a, t_b = op

    # 他现在手里的标的币余额和当前市价
    bal = rpc("getTokenAccountsByOwner", [s, {"mint": base}, {"encoding": "jsonParsed"}])
    held = 0.0
    for acc in (bal or {}).get("value", []):
        held += float(acc["account"]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount") or 0)

    print()
    print("=" * 86)
    print("操盘方成本核算")
    print("=" * 86)
    print(f"  钱包 {s}")
    print(f"  {n} 笔交易 ({b}买 / {sl}卖), 时段 {hhmm(t_a)} -> {hhmm(t_b)}")
    print()
    cost_q = -q * px
    cost_gas = burn * SOL_USD
    print(f"    砸进池子买货     ${cost_q:>10,.2f}   ({-q:.2f} {qn})")
    print(f"    gas + Jito小费   ${cost_gas:>10,.2f}   ({burn:.4f} SOL, {n}笔)")
    print(f"    ------------------------------")
    print(f"    合计已投入       ${cost_q+cost_gas:>10,.2f}")
    print()
    print(f"  现在手里压着 {held:,.0f} 个币")

    # 外部真实买家
    fish = [r for r in rows if r[0] != s]
    fin = sum(-r[4] * px for r in fish if r[4] < 0)
    fout = sum(r[4] * px for r in fish if r[4] > 0)
    print()
    print(f"  外部钱包 {len(fish)} 个:  投进来 ${fin:,.2f}   拿走 ${fout:,.2f}")
    print(f"  = 4.5小时里,他花 ${cost_q+cost_gas:,.0f} 拉盘,总共只钓到 ${fin:,.2f}")


if __name__ == "__main__":
    main()
