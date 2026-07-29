# -*- coding: utf-8 -*-
"""监控PIPEDOG/WETH(robinhood网络,非Solana)——这条链没有RugCheck/GMGN这类
专门服务,只能靠GT的逐笔成交做钱包聚类分析(买卖都做的疑似刷量钱包 vs 只买不卖
的钱包,以及买入金额的变异系数),跟今晚验证过的操盘方指纹(cost variance 3-4%、
bundler零卖出)做对比,看这个池子是继续保持"广泛参与、金额分散"的真实样貌,
还是慢慢也显出集中控盘的迹象。"""
import sys
import time
import datetime as dt
import statistics
from pathlib import Path

import requests

NETWORK = "robinhood"
ADDR = "0x1e8fd16c27c1ea8449254af8ca9a768dd3537df5"
GT_BASE = "https://api.geckoterminal.com/api/v2"
LOG_F = Path(__file__).parent / "pipedog_monitor.log"

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                  "Accept": "application/json;version=20230302"})


def log(msg):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        pass
    with LOG_F.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_pool():
    r = S.get(f"{GT_BASE}/networks/{NETWORK}/pools/{ADDR}", timeout=20)
    a = r.json()["data"]["attributes"]
    return {
        "price": float(a["base_token_price_usd"]),
        "liq": float(a["reserve_in_usd"]),
        "fdv": float(a.get("fdv_usd") or 0),
    }


def get_wallet_stats():
    r = S.get(f"{GT_BASE}/networks/{NETWORK}/pools/{ADDR}/trades", timeout=20)
    rows = r.json().get("data", [])
    by_wallet = {}
    for row in rows:
        a = row["attributes"]
        w = a.get("tx_from_address")
        by_wallet.setdefault(w, []).append(a)

    def parse_ts(a):
        return dt.datetime.fromisoformat(a["block_timestamp"].replace("Z", "+00:00")).timestamp()

    bot_like, buy_only = 0, 0
    for w, trades in by_wallet.items():
        kinds = {t["kind"] for t in trades}
        if len(trades) >= 3 and "buy" in kinds and "sell" in kinds:
            bot_like += 1
        elif kinds == {"buy"}:
            buy_only += 1
    buy_amounts = [float(t["volume_in_usd"]) for trades in by_wallet.values() for t in trades if t["kind"] == "buy"]
    cv = (statistics.stdev(buy_amounts) / statistics.mean(buy_amounts)) if len(buy_amounts) >= 5 and statistics.mean(buy_amounts) > 0 else None
    return {"n_trades": len(rows), "n_wallets": len(by_wallet), "bot_like": bot_like, "buy_only": buy_only, "buy_cv": cv}


log(f"=== 开始监控 PIPEDOG/WETH ({NETWORK}) ===")
baseline_pool = get_pool()
baseline_w = get_wallet_stats()
log(f"基线: 价格${baseline_pool['price']:.8f} 流动性${baseline_pool['liq']:,.0f} "
   f"钱包数{baseline_w['n_wallets']} 疑似刷量{baseline_w['bot_like']} 只买不卖{baseline_w['buy_only']} "
   f"买入金额变异系数{baseline_w['buy_cv']}")

ROUNDS = 30
INTERVAL_SEC = 40
for i in range(ROUNDS):
    time.sleep(INTERVAL_SEC)
    try:
        pool = get_pool()
        w = get_wallet_stats()
    except Exception as e:
        log(f"第{i+1}轮查询失败: {e}")
        continue
    price_chg = (pool["price"] / baseline_pool["price"] - 1) * 100 if baseline_pool["price"] else 0
    liq_chg = (pool["liq"] / baseline_pool["liq"] - 1) * 100 if baseline_pool["liq"] else 0
    log(f"第{i+1}轮: 价格${pool['price']:.8f}({price_chg:+.1f}%) 流动性${pool['liq']:,.0f}({liq_chg:+.1f}%) "
       f"钱包数{w['n_wallets']} 疑似刷量{w['bot_like']} 只买不卖{w['buy_only']} 买入CV={w['buy_cv']}")
    if liq_chg <= -30:
        log("*** 流动性大幅下滑,疑似开始砸盘/抽干 ***")

log(f"=== 监控窗口结束({ROUNDS}轮*{INTERVAL_SEC}秒≈{ROUNDS*INTERVAL_SEC//60}分钟) ===")
