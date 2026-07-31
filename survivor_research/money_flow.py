# -*- coding: utf-8 -*-
"""2026-07-31新建: 用资金守恒算操盘方的真实利润。

用户提出的算法(比追钱包转账可靠得多): 鱼的净亏损 = 鱼买入总额 - 鱼卖出总额,
这笔钱扣掉手续费就是操盘方拿走的。因为钱不会凭空消失——池子里进来的钱,
要么被人卖出拿走,要么还锁在池子里。

为什么这个算法比GMGN的profit字段可靠:
  GMGN把钱包间转账按市价估值算成"收入",USOH的创建者钱包显示profit=$160万,
  而整个池子总成交额才$10.6万,数字完全失真。操盘方还故意用多钱包倒手
  (A买->转给B->C卖)把痕迹打散。而资金守恒不管钱在哪个钱包,只看总量。

费用口径(Solana/pump.fun实际):
  - pumpswap/pump.fun 交易手续费: 每笔约1%(买卖各收)
  - Solana基础gas: 每笔约0.000005 SOL,可忽略
  - 优先费: 抢跑时会给高优先费,砸盘那几笔通常给得很高

用法: python money_flow.py <pool_addr> [insider钱包前缀,逗号分隔]
"""
import sys
from collections import defaultdict

import cg_client as cg

SWAP_FEE_PCT = 1.0      # pump.fun/pumpswap 单边手续费约1%
SOL_GAS = 0.000005      # 每笔基础gas(SOL)


def main():
    pool = sys.argv[1] if len(sys.argv) > 1 else "BFBM1NqjEvuxGcD6tvvHmUFYWqtQh3z1MeQNXKC5bbwa"
    d = cg.get(f"networks/solana/pools/{pool}/trades", {"trade_volume_in_usd_greater_than": 0})
    rows = (d or {}).get("data", [])
    if not rows:
        print("拿不到成交数据")
        return

    trades = []
    for r in rows:
        a = r["attributes"]
        try:
            usd = float(a.get("volume_in_usd") or 0)
        except (TypeError, ValueError):
            continue
        trades.append({
            "ts": a.get("block_timestamp", ""),
            "kind": a.get("kind"),
            "usd": usd,
            "w": a.get("tx_from_address") or "",
        })
    trades.sort(key=lambda x: x["ts"])
    print(f"成交明细: {len(trades)}笔  {trades[0]['ts'][11:19]} -> {trades[-1]['ts'][11:19]}\n")

    # 按钱包汇总净流
    per = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "nbuy": 0, "nsell": 0})
    for t in trades:
        k = per[t["w"]]
        if t["kind"] == "buy":
            k["buy"] += t["usd"]; k["nbuy"] += 1
        else:
            k["sell"] += t["usd"]; k["nsell"] += 1

    # 识别"收割者": 卖出远大于买入的钱包(在这个窗口内净提款)
    winners, losers = [], []
    for w, k in per.items():
        net = k["sell"] - k["buy"]
        (winners if net > 0 else losers).append((net, w, k))
    winners.sort(reverse=True)
    losers.sort()

    tot_buy = sum(t["usd"] for t in trades if t["kind"] == "buy")
    tot_sell = sum(t["usd"] for t in trades if t["kind"] == "sell")
    n_buyers = len({t["w"] for t in trades if t["kind"] == "buy"})
    n_sellers = len({t["w"] for t in trades if t["kind"] == "sell"})

    print("=" * 60)
    print("资金总账(这个成交窗口内)")
    print("=" * 60)
    print(f"  买入: ${tot_buy:>11,.2f}   {sum(1 for t in trades if t['kind']=='buy'):>4}笔 / {n_buyers}个钱包")
    print(f"  卖出: ${tot_sell:>11,.2f}   {sum(1 for t in trades if t['kind']=='sell'):>4}笔 / {n_sellers}个钱包")
    print(f"  净流入池子: ${tot_buy - tot_sell:>11,.2f}  <- 这些钱进去了没出来")

    print()
    print("=" * 60)
    print(f"净提款的钱包(卖>买,共{len(winners)}个) —— 这些是把钱拿走的")
    print("=" * 60)
    tot_extracted = sum(n for n, _, _ in winners)
    for net, w, k in winners[:12]:
        print(f"  {w[:12]}..  买${k['buy']:>8,.0f}({k['nbuy']}笔) 卖${k['sell']:>9,.0f}({k['nsell']}笔)  净提${net:>+10,.0f}")
    print(f"  {'-'*56}")
    print(f"  合计提走: ${tot_extracted:>11,.2f}")

    print()
    print("=" * 60)
    print(f"净亏损的钱包(买>卖,共{len(losers)}个) —— 这些是鱼")
    print("=" * 60)
    tot_lost = sum(-n for n, _, _ in losers)
    print(f"  合计亏损: ${tot_lost:>11,.2f}")
    print(f"  人均亏损: ${tot_lost/max(len(losers),1):>11,.2f}")
    print(f"  最惨的几个:")
    for net, w, k in losers[:5]:
        print(f"    {w[:12]}..  买${k['buy']:>8,.0f} 卖${k['sell']:>7,.0f}  净亏${-net:>9,.0f}")

    # 手续费估算
    fee_swap = (tot_buy + tot_sell) * SWAP_FEE_PCT / 100
    n_tx = len(trades)
    print()
    print("=" * 60)
    print("费用")
    print("=" * 60)
    print(f"  交易手续费(双边各{SWAP_FEE_PCT}%): ${fee_swap:>10,.2f}")
    print(f"  基础gas({n_tx}笔 x {SOL_GAS} SOL): 约${n_tx*SOL_GAS*75:>8,.2f}")

    print()
    print("=" * 60)
    print("结论")
    print("=" * 60)
    print(f"  鱼的净亏损:        ${tot_lost:>11,.2f}")
    print(f"  收割方净提款:      ${tot_extracted:>11,.2f}")
    print(f"  减去交易手续费:    ${fee_swap:>11,.2f}")
    print(f"  收割方净收益(估):  ${tot_extracted - fee_swap:>11,.2f}")
    print()
    print(f"  注: 这只覆盖GT返回的最近{len(trades)}笔成交({trades[0]['ts'][11:19]}起),")
    print(f"      更早的撒饵阶段不在窗口内,所以是收网阶段的账,不是全生命周期。")


if __name__ == "__main__":
    main()
