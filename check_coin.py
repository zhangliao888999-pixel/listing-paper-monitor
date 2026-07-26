# -*- coding: utf-8 -*-
"""入场前尽调工具:给一个池子地址,检查是不是庄家脚本化拉盘/多钱包控盘。

不做买卖判断，只把证据摆出来，买不买用户自己看着办。

用法: python check_coin.py <池子地址或GeckoTerminal链接>

检查两类红旗:
  1. K线形状: 拉升阶段是不是"楼梯状"——连续多根K线单调上涨且每根成交量几乎相等，
     真实散户买盘的成交量应该有明显波动，几乎不波动的staircase是脚本下单的指纹
  2. 钱包画像(GMGN): 头部买家里有多少被标为is_suspicious，买入量是否高度一致(同一操盘方
     分散到多个钱包的迹象)，创建者钱包是否带rat_trader/transfer_in标签(资金内部转入而非
     市场买入)，有没有真实卖出记录(全程0卖出=庄家自己也还没出货)，以及头部钱包的
     sell_amount_percentage(已卖出比例)——判断主力是还在场内还是已经基本清仓离场

注意: 这些都是"当下快照"事实，不是预测。用户在Phase 10已经明确过一次结论——
新币的买卖时机本质是庄家/散户心理博弈，无法被可靠量化，此前专门做的钱包标签关联、
开发者退出时机等信号研究在n=27-41样本下都没能找到稳健的预测力(dev退出时机甚至和
见顶时机完全脱节)。所以这里只报告事实，不产出买/卖评分或"是否建议买入"的结论——
避免重蹈同一个覆辙,把一个此前已经验证不可靠的信号套上"模型"的外壳当成可信依据。
"""
import re
import sys
import statistics
import datetime as dt

import requests

GT_BASE = "https://api.geckoterminal.com/api/v2"
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                  "Accept": "application/json;version=20230302"})

GMGN_S = requests.Session()
GMGN_S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                       "Referer": "https://gmgn.ai/", "Accept": "application/json"})


def get(session, url, params=None, tries=3):
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                import time
                time.sleep(3 * (i + 1))
                continue
            return None
        except requests.RequestException:
            import time
            time.sleep(1.5 * (i + 1))
    return None


def extract_addr(arg):
    m = re.search(r"/pools/([A-Za-z0-9]+)", arg)
    return m.group(1) if m else arg


def check_pool_and_mint(addr):
    d = get(S, f"{GT_BASE}/networks/solana/pools/{addr}", {"include": "base_token"})
    if not d:
        return None, None
    attrs = d["data"]["attributes"]
    mint = None
    for inc in d.get("included", []):
        if inc.get("type") == "token":
            mint = inc["attributes"].get("address")
    return attrs, mint


def check_staircase(addr):
    d = get(S, f"{GT_BASE}/networks/solana/pools/{addr}/ohlcv/minute", {"aggregate": 5, "limit": 100})
    bars = (d or {}).get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    bars = sorted(bars, key=lambda b: b[0])
    if len(bars) < 4:
        return {"n_bars": len(bars), "verdict": "数据不够,跳过K线形状检查"}
    up_bars = sum(1 for b in bars if b[4] >= b[1])
    up_ratio = up_bars / len(bars)
    vols = [b[5] for b in bars if b[5] > 0]
    vol_cv = (statistics.stdev(vols) / statistics.mean(vols)) if len(vols) >= 3 and statistics.mean(vols) > 0 else None
    # 真实拉盘(哪怕是狂热买盘)中途也会有获利了结造成的回调K线；
    # 连续多根K线一根不跌，比成交量是否均匀更能区分"脚本连续下单"和"真实买盘"
    flag = len(bars) >= 5 and up_ratio >= 0.9
    return {
        "n_bars": len(bars), "up_ratio": up_ratio, "vol_cv": vol_cv,
        "flag": flag,
        "verdict": "疑似脚本化拉盘(连续K线几乎不出现回调)" if flag else "K线形态未见明显脚本化特征",
    }


def check_wallets(mint):
    if not mint:
        return {"verdict": "拿不到mint地址,跳过钱包检查"}
    d = get(GMGN_S, f"https://gmgn.ai/vas/api/v1/token_traders/sol/{mint}", {"limit": 40})
    if not d or d.get("code") != 0:
        return {"verdict": "GMGN查询失败,跳过钱包检查"}
    data = d.get("data")
    rows = data.get("list", []) if isinstance(data, dict) else (data or [])
    if not rows:
        return {"verdict": "GMGN无数据"}

    n = len(rows)
    n_suspicious = sum(1 for r in rows if r.get("is_suspicious"))
    buys = [r.get("current_buy_amount") or r.get("buy_amount") for r in rows]
    buys = [b for b in buys if b]
    buy_cv = (statistics.stdev(buys) / statistics.mean(buys)) if len(buys) >= 3 and statistics.mean(buys) > 0 else None
    n_sold = sum(1 for r in rows if (r.get("current_sell_amount") or r.get("sell_amount")))
    creator = next((r for r in rows if "creator" in (r.get("maker_token_tags") or [])), None)
    creator_tags = creator.get("maker_token_tags") if creator else None
    internal_funded = creator_tags is not None and "transfer_in" in creator_tags

    # 主力持仓状态: sell_amount_percentage是"这个钱包买过的量里已经卖掉的比例"，
    # 不是"是否卖过"，能看出头部钱包是清仓了还是还捂着大部分仓位
    sell_pcts = [r.get("sell_amount_percentage") for r in rows if r.get("sell_amount_percentage") is not None]
    n_exited = sum(1 for p in sell_pcts if p >= 0.7)     # 卖掉70%+算基本清仓
    n_holding = sum(1 for p in sell_pcts if p < 0.3)      # 卖掉不到30%算基本还拿着
    usd_still_held = sum(r.get("usd_value") or 0 for r in rows)
    usd_ever_invested = sum(r.get("total_cost") or 0 for r in rows)
    # 分母太小时(部分币的total_cost字段缺失/退化)除出来的比例会离谱地爆炸,不如直接判无效
    exit_ratio = (1 - usd_still_held / usd_ever_invested) if usd_ever_invested >= 50 else None

    flags = []
    if n and n_suspicious / n >= 0.5:
        flags.append(f"{n_suspicious}/{n}个头部钱包被GMGN标为可疑")
    if buy_cv is not None and buy_cv < 0.25:
        flags.append(f"买入量高度一致(变异系数{buy_cv:.2f})，疑似同一操盘方分仓")
    if n_sold == 0 and n >= 5:
        flags.append("头部钱包里没有一笔卖出记录，庄家/早期买家还未出货")
    if internal_funded:
        flags.append("创建者钱包标记为transfer_in(资金内部转入，不是市场买入)")
    if sell_pcts and n_exited / len(sell_pcts) >= 0.8:
        flags.append(f"{n_exited}/{len(sell_pcts)}个头部钱包已卖出70%+仓位，主力大概率已基本出货完毕")
    elif sell_pcts and n_holding / len(sell_pcts) >= 0.8:
        flags.append(f"{n_holding}/{len(sell_pcts)}个头部钱包卖出不到30%，主力大部分仓位还在场内")

    return {
        "n_traders": n, "n_suspicious": n_suspicious, "buy_cv": buy_cv,
        "n_sold": n_sold, "creator_tags": creator_tags,
        "n_exited_70pct": n_exited, "n_holding_70pct": n_holding,
        "usd_still_held": round(usd_still_held), "usd_ever_invested": round(usd_ever_invested),
        "exit_ratio": exit_ratio, "flags": flags,
        "verdict": " / ".join(flags) if flags else "钱包画像未见明显控盘迹象",
    }


def main():
    if len(sys.argv) < 2:
        print("用法: python check_coin.py <池子地址或GeckoTerminal链接>")
        return
    addr = extract_addr(sys.argv[1])
    attrs, mint = check_pool_and_mint(addr)
    if not attrs:
        print("查不到这个池子,地址可能不对")
        return

    print(f"=== {attrs.get('name')} ===")
    created = attrs.get("pool_created_at")
    if created:
        age_h = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 3600
        print(f"创建: {created}  (库龄 {age_h:.1f} 小时)")
    print(f"价格: {attrs.get('base_token_price_usd')}  流动性: ${float(attrs.get('reserve_in_usd') or 0):,.0f}")
    tx = attrs.get("transactions") or {}
    for w in ("m5", "m15", "h1", "h6", "h24"):
        t = tx.get(w) or {}
        print(f"  近{w}: 买{t.get('buys',0)} 卖{t.get('sells',0)}  买家{t.get('buyers',0)} 卖家{t.get('sellers',0)}")

    print("\n--- K线形态检查 ---")
    stair = check_staircase(addr)
    for k, v in stair.items():
        if k != "flag":
            print(f"  {k}: {v}")

    print("\n--- 钱包画像检查(GMGN) ---")
    wallets = check_wallets(mint)
    for k, v in wallets.items():
        if k not in ("flags", "verdict"):
            print(f"  {k}: {v}")
    print(f"  结论: {wallets.get('verdict')}")

    print(f"\n=== 综合: K线-{stair.get('verdict')} | 钱包-{wallets.get('verdict')} ===")
    print("(仅供参考,买卖决定自己判断)")


if __name__ == "__main__":
    main()
