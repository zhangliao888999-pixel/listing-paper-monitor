# -*- coding: utf-8 -*-
"""研究"高频刷量机器人"到底是什么策略、赚不赚钱。

用户看到OPENAI/SOL上一个钱包10秒钟一个来回反复买卖，问：这些机器人的策略是什么，
我们能不能抄一个，它们整体赚钱比例如何。

做法:
  1. 在当前候选币里用check_scalping()找机器人钱包(复用screener已有逻辑，
     GeckoTerminal接口，不占GMGN配额)
  2. 对找到的每个机器人钱包，直接用它在这个池子里能看到的逐笔买卖价格，算一下
     "如果就看这些交易，买卖价差本身是赚是亏"(不看链上gas/优先费，只看价差)
  3. 查GMGN的wallet_stat，看这个钱包的历史全局战绩(realized_profit/total_volume)

结论用于回答"这个策略能不能抄"——如果观测窗口内价差都薄到覆不住gas costs，
或者需要秒级反应速度，人工操作基本没法复制；这类策略的护城河大概率是延迟/
基础设施而不是信息优势。
"""
import json
import statistics
import sys
import time
import datetime as dt
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from check_coin import S, GT_BASE  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "bot_strategy_analysis.jsonl"

GMGN_S = requests.Session()
GMGN_S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                       "Referer": "https://gmgn.ai/", "Accept": "application/json"})


def gmgn_get(url, params=None, tries=2):
    for i in range(tries):
        try:
            r = GMGN_S.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (403, 429):
                time.sleep(6 * (i + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(3)
    return None


def gt_get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(3 * (i + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1.5)
    return None


def load_candidates():
    cands, seen = [], set()
    for name in ("screener_candidates.json", "screener_candidates_local.json"):
        f = HERE.parent / name
        if not f.exists():
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        for c in d.get("candidates", []):
            if c["addr"] not in seen:
                seen.add(c["addr"])
                cands.append(c)
    return cands


def find_bot_wallets(addr):
    """在这个池子的逐笔成交里找高频反复买卖的钱包，返回其全部交易明细(不只是flag)"""
    d = gt_get(f"{GT_BASE}/networks/solana/pools/{addr}/trades")
    rows = (d or {}).get("data", [])
    if len(rows) < 10:
        return []
    by_wallet = {}
    for row in rows:
        a = row["attributes"]
        w = a.get("tx_from_address")
        if w:
            by_wallet.setdefault(w, []).append(a)

    bots = []
    for w, trades in by_wallet.items():
        if len(trades) < 8:
            continue
        kinds = {t["kind"] for t in trades}
        if not ("buy" in kinds and "sell" in kinds):
            continue
        ts = sorted(dt.datetime.fromisoformat(t["block_timestamp"].replace("Z", "+00:00")).timestamp() for t in trades)
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        if statistics.median(gaps) < 60:
            bots.append((w, trades))
    return bots


def roundtrip_pnl(trades):
    """粗算这些交易的买卖价差盈亏(只看价格,不算gas/优先费/滑点)。
    按买卖两边分别累计花掉的钱和收到的钱，因为SOL计价的token交易，
    这里统一用美元volume_in_usd。"""
    buy_usd = sum(float(t["volume_in_usd"]) for t in trades if t["kind"] == "buy")
    sell_usd = sum(float(t["volume_in_usd"]) for t in trades if t["kind"] == "sell")
    n_buy = sum(1 for t in trades if t["kind"] == "buy")
    n_sell = sum(1 for t in trades if t["kind"] == "sell")
    return {"buy_usd": round(buy_usd), "sell_usd": round(sell_usd),
           "pnl_usd": round(sell_usd - buy_usd), "n_buy": n_buy, "n_sell": n_sell}


def wallet_global_stat(wallet):
    d = gmgn_get(f"https://gmgn.ai/api/v1/wallet_stat/sol/{wallet}/all")
    if not d or d.get("code") != 0:
        return None
    data = d.get("data") or {}
    return {"realized_profit": data.get("realized_profit"), "total_volume": data.get("total_volume"),
           "winrate": data.get("winrate"), "token_num": data.get("token_num")}


def main():
    cands = load_candidates()
    print(f"扫描候选币: {len(cands)}个")
    found = {}  # wallet -> list of (coin_name, trades)
    for c in cands:
        bots = find_bot_wallets(c["addr"])
        for w, trades in bots:
            found.setdefault(w, []).append((c["name"], trades))
        time.sleep(0.4)

    print(f"\n发现 {len(found)} 个机器人钱包(跨{len(cands)}个候选币)")
    results = []
    with OUT.open("w", encoding="utf-8") as f:
        for w, occurrences in found.items():
            for coin_name, trades in occurrences:
                pnl = roundtrip_pnl(trades)
                gstat = wallet_global_stat(w)
                time.sleep(1.5)
                row = {"wallet": w, "coin": coin_name, "n_trades_seen": len(trades), **pnl, "global_stat": gstat}
                results.append(row)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                print(f"  {w[:10]}... on {coin_name}: 观测窗口内买${pnl['buy_usd']} 卖${pnl['sell_usd']} "
                     f"净={pnl['pnl_usd']:+d}usd  全局历史盈利={gstat.get('realized_profit') if gstat else '查不到'}")

    print(f"\n=== 汇总: {len(results)} 条 机器人x币 观测记录 ===")
    pnls = [r["pnl_usd"] for r in results]
    if pnls:
        n_profit = sum(1 for p in pnls if p > 0)
        print(f"观测窗口内净赚(价差为正)的比例: {n_profit}/{len(pnls)} = {n_profit/len(pnls)*100:.0f}%")
        print(f"净盈亏中位数: ${statistics.median(pnls):+.0f}  均值: ${statistics.mean(pnls):+.0f}")
    global_profits = [r["global_stat"]["realized_profit"] for r in results
                      if r.get("global_stat") and r["global_stat"].get("realized_profit") is not None]
    if global_profits:
        print(f"这些钱包的GMGN全局历史已实现盈利中位数: ${statistics.median(global_profits):,.0f}")


if __name__ == "__main__":
    main()
