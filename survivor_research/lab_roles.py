# -*- coding: utf-8 -*-
"""账户角色编目。2026-07-31建。

必须解决的问题: VPS采的"狗庄成本"可能是错的。如果一个币的交易是通过**托管
平台**走的(比如 BwWK17cbHxwW,余额46,752 SOL、每分钟319笔、7个签名人共用),
那么链上动钱的是平台的资金池,不是任何一个操盘方——我记的账根本不是他的。

要把池子里出现的账户分成五类,前四类都必须从"狗庄/鱼"的统计里剔除:

  protocol_fee  pump.fun协议费账户,每笔抽0.95%,只收不付
  creator_fee   每个币自己的创建者费账户,抽0.30%,只收不付且只服务一个币
  custodial     托管平台资金池: 余额巨大、多个签名人共用、同时操作很多币
  curve/vault   联合曲线或池子金库
  trader        真正的交易者(狗庄或鱼)

判据全部基于可观测的链上特征,不靠地址名单硬编码——名单会过期,特征不会。

用法:
  python lab_roles.py <pool> [更多pool...]    编目这些池子里的账户
  python lab_roles.py --trace <账户>          追这个账户的钱流向哪
"""
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import lab_forensics as fx   # noqa: E402

SOL_USD = 75.0
# 判定阈值
FEE_PAY_RATIO = 0.02      # 收入里付出占比低于这个 = 只收不付
# 费用账户不一定出现在每笔交易里(有的交易免费、有的走别的费率档),
# 第一版设0.25把 pump.fun 协议费账户漏掉了 —— 它只出现在 21% 的交易里。
FEE_MIN_HITS = 0.10
CUSTODIAL_SOL = 500.0     # 余额超过这个量级不可能是买微市值新币的散户
# 第一版要求"余额大 且 签名人>=3",但探测只采样120笔时经常看不到3个签名人,
# 结果把46,752 SOL的托管钱包判成了普通交易者。改成余额大 + 任一辅助特征。
CUSTODIAL_SIGNERS = 2
CUSTODIAL_MINTS = 5


def probe(addr, cap=300):
    """探测一个账户的特征: 余额、交易频率、涉及币种数、共用它的签名人数。"""
    info = fx.rpc("getAccountInfo", [addr, {"encoding": "jsonParsed"}])
    v = (info or {}).get("value") or {}
    bal = v.get("lamports", 0) / 1e9
    sigs = fx.get_signatures(addr, cap=cap)
    ok = [s for s in sigs if not s["err"] and s.get("ts")]
    if not ok:
        return {"bal": bal, "n_sig": len(sigs), "rate": 0, "mints": 0,
                "signers": 0, "err_rate": 0}
    span = max((ok[-1]["ts"] - ok[0]["ts"]) / 60, 0.01)
    mints, signers = set(), Counter()

    def one(s):
        r = fx.rpc("getTransaction", [s["sig"], {"maxSupportedTransactionVersion": 0,
                                                 "encoding": "jsonParsed"}])
        if not r:
            return None
        k = fx.account_keys(r)
        ms = {b.get("mint") for b in ((r.get("meta") or {}).get("postTokenBalances") or [])
              if b.get("mint") not in fx.QUOTES}
        return (k[0] if k else None), ms

    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(one, ok[-200:]):
            if not r:
                continue
            s, ms = r
            if s:
                signers[s] += 1
            mints |= ms
    return {"bal": bal, "n_sig": len(sigs), "rate": len(ok) / span,
            "mints": len(mints), "signers": len(signers),
            "err_rate": (len(sigs) - len(ok)) / max(len(sigs), 1)}


def classify(addr, pool, stat, feat):
    """stat = 池内的 {inn, out, hits}; feat = probe() 的结果。"""
    inn, out, hits, ntx = stat["inn"], stat["out"], stat["hits"], stat["ntx"]
    only_receives = out > 0 and inn < out * FEE_PAY_RATIO
    if addr == pool:
        return "curve/vault", "池子账户本身"
    if only_receives and hits >= ntx * FEE_MIN_HITS:
        if feat["mints"] > 3:
            return "protocol_fee", f"只收不付+跨{feat['mints']}个币,协议级"
        return "creator_fee", f"只收不付+仅服务本币,创建者费"
    if feat["bal"] >= CUSTODIAL_SOL and (feat["signers"] >= CUSTODIAL_SIGNERS
                                         or feat["mints"] >= CUSTODIAL_MINTS):
        return "custodial", (f"余额{feat['bal']:,.0f} SOL, {feat['signers']}个签名人共用, "
                             f"跨{feat['mints']}个币, {feat['rate']:.0f}笔/分钟")
    return "trader", ""


def do_pool(pool):
    sigs = [s for s in fx.get_signatures(pool, cap=1500)
            if not s["err"] and s.get("ts")]
    txs = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for t in ex.map(fx.parse_tx, sigs):
            if t:
                txs.append(t)
    if len(txs) < 10:
        print(f"  {pool[:12]}.. 数据不足")
        return
    stats = defaultdict(lambda: {"inn": 0.0, "out": 0.0, "hits": 0})
    for t in txs:
        for a, d in t["sol_delta"].items():
            if abs(d) < 1e-9:
                continue
            s = stats[a]
            s["hits"] += 1
            if d > 0:
                s["out"] += d
            else:
                s["inn"] += -d
    # 只探测有分量的账户,省RPC
    cands = [a for a, s in stats.items()
             if s["hits"] >= max(len(txs) * 0.05, 3) or (s["inn"] + s["out"]) > 0.05]
    print(f"\n{'='*76}")
    print(f"  {pool}")
    print(f"  {len(txs)}笔交易, {len(stats)}个账户有资金变动, 探测其中 {len(cands)} 个")
    print(f"{'='*76}")
    rows = []
    for a in cands:
        st = dict(stats[a], ntx=len(txs))
        feat = probe(a)
        role, why = classify(a, pool, st, feat)
        rows.append((role, a, st, feat, why))
    order = {"protocol_fee": 0, "creator_fee": 1, "custodial": 2,
             "curve/vault": 3, "trader": 4}
    rows.sort(key=lambda r: (order.get(r[0], 9), -(r[2]["out"] - r[2]["inn"])))
    print(f"  {'角色':<14}{'账户':<46}{'净SOL':>11}{'出现':>6}")
    tr_in = tr_out = 0.0
    for role, a, st, feat, why in rows:
        net = st["out"] - st["inn"]
        print(f"  {role:<14}{a:<46}{net:>+11.4f}{st['hits']:>6}")
        # 判定依据一律打印。第一版只在命中特殊角色时才打,导致误判时看不出
        # 是哪一条阈值没过,只能靠猜。
        print(f"  {'':<14}  余额{feat['bal']:>10,.1f}SOL 频率{feat['rate']:>5.0f}/分 "
              f"币种{feat['mints']:>3} 签名人{feat['signers']:>3} "
              f"收{st['out']:>8.4f} 付{st['inn']:>8.4f}"
              + (f"  <- {why}" if why else ""))
        if role == "trader":
            tr_in += st["inn"]; tr_out += st["out"]
    n_cust = sum(1 for r in rows if r[0] == "custodial")
    cust_vol = sum(r[2]["inn"] + r[2]["out"] for r in rows if r[0] == "custodial")
    tot_vol = sum(r[2]["inn"] + r[2]["out"] for r in rows)
    print(f"\n  真实交易者资金: 投入 {tr_in:.4f} SOL  取出 {tr_out:.4f} SOL")
    if n_cust:
        print(f"  !! {n_cust}个托管平台账户占了 {cust_vol/max(tot_vol,1e-9):.0%} 的资金流量")
        print(f"     这部分链上无法归属到具体的人 —— 这个池子的狗庄/鱼统计不可信")


def trace(addr):
    """追一个账户的钱最后流向哪里。"""
    print(f"追踪 {addr}")
    feat = probe(addr, cap=600)
    print(f"  余额 {feat['bal']:,.4f} SOL (${feat['bal']*SOL_USD:,.0f})")
    print(f"  签名数 {feat['n_sig']}  频率 {feat['rate']:.0f}笔/分钟  "
          f"涉及 {feat['mints']} 种币  {feat['signers']} 个签名人")
    sigs = [s for s in fx.get_signatures(addr, cap=400)
            if not s["err"] and s.get("ts")]
    out = defaultdict(float)
    def one(s):
        r = fx.rpc("getTransaction", [s["sig"], {"maxSupportedTransactionVersion": 0,
                                                 "encoding": "jsonParsed"}])
        if not r:
            return None
        keys = fx.account_keys(r)
        meta = r["meta"]
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if addr not in keys:
            return None
        i = keys.index(addr)
        if i >= len(pre):
            return None
        d = (post[i] - pre[i]) / 1e9
        if d >= -1e-7:          # 只关心它往外付钱的交易
            return None
        res = []
        for j, k in enumerate(keys):
            if j < len(pre) and j < len(post):
                v = (post[j] - pre[j]) / 1e9
                if v > 1e-7:
                    res.append((k, v))
        return res
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(one, sigs[-200:]):
            if r:
                for k, v in r:
                    out[k] += v
    if not out:
        print("  最近200笔里它没有往外付过钱(纯收款账户)")
        return
    print(f"\n  它付出去的钱流向(前8):")
    for k, v in sorted(out.items(), key=lambda kv: -kv[1])[:8]:
        print(f"    {v:>+12.5f} SOL (${v*SOL_USD:>9,.2f})  {k}")


def main():
    args = sys.argv[1:]
    if "--trace" in args:
        trace(args[args.index("--trace") + 1]); return
    pools = [a for a in args if len(a) > 30]
    if not pools:
        print(__doc__); return
    for p in pools:
        do_pool(p)


if __name__ == "__main__":
    main()
