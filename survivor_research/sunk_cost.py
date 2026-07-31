# -*- coding: utf-8 -*-
"""实时沉没成本查询器。2026-07-31建。

策略核心传感器。用户提出的进场规则:
  "一定要等他成本付出大于我们买入的金额时,才是相对安全的"
  "我买的金额小到他根本看不上,不会砸,砸了他这几个小时鱼儿白钓了"

逻辑是: 狗庄砸盘是有代价的——砸下去他手里那几亿个币就一文不值,几小时的
撒饵全白费。所以他只在"吃到的鱼 > 放弃这个盘的损失"时才收网。我们的仓位
只要远小于他的沉没成本,他砸我们就是自己认赔离场,不划算。

所以进场前必须知道两个数: **他已经沉了多少钱** 和 **我们进多少**。

为什么不能用GT的 reserve_in_usd 当沉没成本:
  DISNEY 的 reserve_in_usd 是 $25,062,但池子里真实的USDC只有 $2,556。
  GT把曲线上没卖出去的标的币也按市价算进"流动性"了,虚高10倍。所以必须
  去链上读计价币金库的真实余额。

速度设计(要能实时用):
  1次GT调用拿最近300笔成交 -> 算钱包集中度、最近买单大小
  2次RPC拿金库真实余额     -> 精确的沉没成本
  全程约3秒,不用跑全量取证。

用法:
  python sunk_cost.py <pool>              查一个
  python sunk_cost.py <pool1> <pool2> ..  批量
"""
import sys
import time
from collections import defaultdict

import requests

import cg_client as cg
import lab_forensics as fx
from concurrent.futures import ThreadPoolExecutor

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTES = {WSOL: ("SOL", 75.0), USDC: ("USDC", 1.0)}
# 安全仓位系数: 仓位占沉没成本多少以内,狗庄砸我们就不划算。
# 先用保守的2%,等实验室跑出 trigger_ratio 分布后用真实数据校准。
SAFE_FRAC = 0.02
SCAN_CAP = 2500      # 实时查询最多扫这么多笔,再多就太慢,标记为截断


# 复用 lab_forensics 的多节点轮换+退避。单节点会被打爆:后台同时跑着全量
# 下载和盯盘器时,单点 getTransaction 直接返回空,金库就认不出来了。
from lab_forensics import rpc  # noqa: E402


def account_keys(res):
    """完整账户列表 = 静态地址 + 地址查找表(writable + readonly)。

    版本化交易只用 message.accountKeys 会跟 postTokenBalances 的
    accountIndex 错位,金库就认不出来(第一版就是这么失败的)。
    """
    msg = (res.get("transaction") or {}).get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in (msg.get("accountKeys") or [])]
    loaded = (res.get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])


def find_vault(pool):
    """从最近几笔成交里认出计价币金库。

    金库的特征: 非签名账户、持有计价币、且它的计价币余额在买入时增加。
    取余额最大的那个,因为池子金库必然远大于路由中转账户。
    """
    sigs = rpc("getSignaturesForAddress", [pool, {"limit": 15}]) or []
    best = None
    for s in sigs[:8]:
        if s.get("err"):
            continue
        res = rpc("getTransaction", [s["signature"],
                                     {"maxSupportedTransactionVersion": 0,
                                      "encoding": "jsonParsed"}])
        if not res:
            continue
        keys = account_keys(res)
        signer = keys[0] if keys else None
        for b in ((res.get("meta") or {}).get("postTokenBalances") or []):
            mint, idx, owner = b.get("mint"), b.get("accountIndex"), b.get("owner")
            if mint not in QUOTES or owner == signer:
                continue
            v = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            acct = keys[idx] if idx is not None and idx < len(keys) else None
            if acct and v > 0 and (best is None or v * QUOTES[mint][1] > best[1] * QUOTES[best[2]][1]):
                best = (acct, v, mint)
        if best:
            return best
    return best


def check(pool, verbose=True):
    info = cg.get(f"networks/solana/pools/{pool}")
    if not info:
        print(f"{pool}: 查不到"); return None
    a = info["data"]["attributes"]
    name = a.get("name")
    tx24 = (a.get("transactions") or {}).get("h24") or {}
    created = a.get("pool_created_at") or ""
    price = float(a.get("base_token_price_usd") or 0)

    trades = cg.get(f"networks/solana/pools/{pool}/trades", {"trade_volume_in_usd_greater_than": 0})
    rows = (trades or {}).get("data", [])
    per = defaultdict(lambda: [0, 0.0, 0.0])      # 钱包 -> [笔数, 买额, 卖额]
    for r in rows:
        t = r["attributes"]
        w = t.get("tx_from_address") or "?"
        v = float(t.get("volume_in_usd") or 0)
        per[w][0] += 1
        if t.get("kind") == "buy":
            per[w][1] += v
        else:
            per[w][2] += v
    n_tr = sum(x[0] for x in per.values()) or 1
    ranked = sorted(per.items(), key=lambda kv: -kv[1][0])
    top_w, top_st = (ranked[0] if ranked else ("?", [0, 0, 0]))
    top_share = top_st[0] / n_tr

    # 金库余额**不能**当沉没成本用: Meteora DBC 的计价币金库是同一套配置下
    # 多个池子共用的。DISNEY 的金库读出来 $10,390,而全量取证算出这个盘实际
    # 只有 $2,556 进去过——余额里混着别的盘的钱。所以走资金守恒: 逐笔加总
    # 所有钱包的计价币净流出,那才是这个池子真正收到的钱。
    vault_bal = None
    vault = find_vault(pool)
    if vault:
        b = rpc("getTokenAccountBalance", [vault[0]])
        if b:
            vault_bal = float((b.get("value") or {}).get("uiAmount") or 0) * QUOTES[vault[2]][1]

    sigs = fx.get_signatures(pool, cap=SCAN_CAP)
    ok = [s for s in sigs if not s["err"] and s.get("ts")]
    truncated = len(ok) >= SCAN_CAP
    txs = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for t in ex.map(fx.parse_tx, ok):
            if t:
                txs.append(t)
    m, _ = fx.analyze(pool, txs)
    if not m:
        print(f"{pool}: 取证失败"); return None
    sunk = m["peak_res_usd"]

    # 取证引擎已经用并查集把同伙并好了,直接用它的口径,不再自己按份额估。
    buyers = int(tx24.get("buyers") or 0)
    op_wallets = [m["op_addr"]]
    op_share = m["top_share"]
    op_sunk = m["op_cost_usd"]
    fish_money = m["fish_in_usd"]
    # 用户的核心判据: "亏本的买卖没人做,狗庄没赚到钱不会收网,这是人性"。
    # 鱼的钱超过他的成本 = 他已经能盈利离场 = 随时可能收网。
    danger = (fish_money / op_sunk) if op_sunk > 1 else (999.0 if fish_money > 1 else 0.0)

    if verbose:
        print("=" * 72)
        print(f"{name}   {pool}")
        print("=" * 72)
        print(f"  建池 {created}   价格 ${price:.10f}")
        print(f"  24h  买{tx24.get('buys')}笔/{buyers}人   卖{tx24.get('sells')}笔/{tx24.get('sellers')}人")
        print(f"  最近{n_tr}笔成交里,最活跃钱包 {top_w[:12]}.. 占 {top_share:.1%}"
              f"  (买${top_st[1]:,.0f} 卖${top_st[2]:,.0f})")
        print(f"  参与钱包数(近300笔): {len(per)}")
        print(f"  全量取证: {m['n_tx']}笔/{m['n_wallet']}个签名钱包"
              f"{'  [!] 只扫了最近' + str(SCAN_CAP) + '笔,数值偏低' if truncated else ''}")
        print()
        if sunk is None:
            print("  !! 认不出计价币金库,无法给出沉没成本")
        else:
            print(f"  池子累计净流入:      ${sunk:>12,.2f}   <- 资金守恒逐笔加总")
            print(f"  金库账户余额:        ${vault_bal or 0:>12,.2f}   (Meteora多池共用,仅供参考)")
            print(f"  GT显示的流动性:      ${float(a.get('reserve_in_usd') or 0):>12,.2f}   "
                  f"(含未卖出的币,虚高,别用)")
            print()
            print(f"  其中狗庄的钱:        ${op_sunk:>12,.2f}   ({len(op_wallets)}个主力钱包)")
            print(f"  其中鱼的钱:          ${fish_money:>12,.2f}")
            print()
            print(f"  >> 危险度 = 鱼的钱/他的成本 = {danger:>6.2f}"
                  f"   ({'他还在亏,不会收网' if danger < 1 else '他已能盈利离场'})")
            safe = op_sunk * SAFE_FRAC
            print(f"  >> 安全仓位上限:     ${safe:>12,.2f}   (沉没成本的{SAFE_FRAC:.0%})")
            print()
            if op_sunk < 200:
                print(f"  【危险·不可进】他才沉了${op_sunk:,.0f}。砸盘对他没损失,"
                      f"机器人见到任何买单都会全抛(就是实盘那5刀的情形)。")
            elif danger >= 1.0:
                print(f"  【危险·随时收网】鱼的钱(${fish_money:,.0f})已经超过他的成本"
                      f"(${op_sunk:,.0f})。他现在砸盘就是净赚离场,没有任何理由再等。")
            elif danger >= 0.6:
                print(f"  【警戒】鱼的钱已到他成本的{danger:.0%},接近打平点,"
                      f"距离他收网不远了。")
            elif top_share > 0.5:
                print(f"  【相对安全】{top_share:.0%}的交易还是他自己在做,他沉了${op_sunk:,.0f}"
                      f"却只钓到${fish_money:,.0f},还在亏。为吃${safe:,.0f}砸盘等于自己认赔。")
            else:
                print(f"  【形态不明】集中度只有{top_share:.0%},"
                      f"可能已经不是钓鱼盘,需要全量取证定性。")
    return {"pool": pool, "name": name, "sunk": sunk, "op_sunk": op_sunk,
            "top_share": top_share, "buyers": buyers, "n_wallets": len(per)}


def main():
    pools = [p for p in sys.argv[1:] if not p.startswith("-")]
    if not pools:
        print(__doc__); return
    for p in pools:
        check(p)
        print()


if __name__ == "__main__":
    main()
