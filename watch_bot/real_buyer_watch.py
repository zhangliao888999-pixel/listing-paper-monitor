# -*- coding: utf-8 -*-
"""2026-07-28新增: 盯着一个"机器人对倒拉升、还没被真买家发现"的池子,实时检测"疑似
真买家入场"信号——一旦出现不在已知操盘方钱包名单里、金额明显偏大的买入,立刻报警,
用户自己决定是否按当时价格立即卖出。不设固定止盈止损,思路是"能跑就是赢,赚多赚少
无所谓"。

已知操盘方钱包名单的识别方法,复用今晚分析TNOS时验证过的思路: GMGN头部交易者里带
bundler/transfer_in/creator/dev_team这几个标签的,大概率是同一伙人控制的钱包群
(TNOS那次实测32个transfer_in钱包成本变异系数只有3.7%,几乎不可能是独立真实买家)。

用法: python real_buyer_watch.py <池子地址或GeckoTerminal链接>

局限性(用户已明确认可这个风险,不要过度设计):
- 不保证能抢在操盘方砸盘之前跑掉——如果操盘方自己也有监控真买家信号的机器人、
  同一区块内就动手砸盘,这个工具一样来不及。反应速度受限于GeckoTerminal接口本身
  的延迟(不是链上实时订阅)加上轮询间隔,是秒级不是区块级。
- 已知操盘方钱包名单只在启动时拉一次快照,如果操盘方后续换新钱包继续买卖,
  可能不会被识别为"已知",从而被误判成"真买家"信号(误报,不是漏报,相对安全的
  误差方向)。
"""
import re
import sys
import time
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from check_coin import GT_BASE, S, GMGN_S, get, check_pool_and_mint

MIN_REAL_BUYER_USD = 50.0   # 单笔买入超过这个金额,且不在已知操盘方名单里,才算"疑似真买家"
POLL_SEC = 10                # 轮询间隔——不保证抢在砸盘前反应,只是尽量缩短感知延迟
MAX_ROUNDS = 200             # 约33分钟(200*10秒),避免无限占用后台进程

HERE = Path(__file__).parent
LOG_F = HERE / "real_buyer_watch.log"


def extract_addr(arg):
    m = re.search(r"/pools/([A-Za-z0-9]+)", arg)
    return m.group(1) if m else arg


def log(msg):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with LOG_F.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_known_insiders(mint):
    """拉一次头部交易者快照,把bundler/transfer_in/creator/dev_team标签的钱包
    都算作"已知操盘方",这些标签本身就是GMGN自己检测出的协同控盘迹象。"""
    d = get(GMGN_S, f"https://gmgn.ai/vas/api/v1/token_traders/sol/{mint}", {"limit": 40})
    rows = (d or {}).get("data", {}).get("list", [])
    insiders = set()
    for r in rows:
        tags = r.get("maker_token_tags") or []
        if any(t in tags for t in ("bundler", "transfer_in", "creator", "dev_team")):
            insiders.add(r["address"])
    return insiders


def main():
    if len(sys.argv) < 2:
        print("用法: python real_buyer_watch.py <池子地址或GeckoTerminal链接>")
        return
    addr = extract_addr(sys.argv[1])
    attrs, mint = check_pool_and_mint(addr)
    if not attrs:
        log("查不到这个池子,地址可能不对")
        return

    log(f"=== 开始监控 {attrs.get('name')} ({addr[:10]}...) ===")
    insiders = load_known_insiders(mint)
    log(f"已知操盘方钱包(bundler/transfer_in/creator/dev_team标签): {len(insiders)}个")
    log(f"检测条件: 单笔买入>=${MIN_REAL_BUYER_USD:.0f} 且钱包不在上面这个名单里 -> 判定疑似真买家")
    log(f"每{POLL_SEC}秒查一次最新成交,最长跑{MAX_ROUNDS*POLL_SEC//60}分钟")
    log("提醒: 报警不代表来得及反应,只是尽量缩短感知延迟,自己判断要不要动作")

    seen_tx = set()
    first_pass = True
    n_alerts = 0
    for round_i in range(MAX_ROUNDS):
        try:
            d = get(S, f"{GT_BASE}/networks/solana/pools/{addr}/trades", {"trade_volume_in_usd_greater_than": 0})
            rows = (d or {}).get("data", [])
        except Exception as e:
            log(f"查询失败: {e}")
            time.sleep(POLL_SEC)
            continue
        rows.sort(key=lambda r: r["attributes"]["block_timestamp"])
        new_rows = [r for r in rows if r["attributes"]["tx_hash"] not in seen_tx]
        for row in new_rows:
            a = row["attributes"]
            seen_tx.add(a["tx_hash"])
            if first_pass:
                continue  # 第一轮只建立基线(哪些成交已经存在),不对历史成交报警
            if a["kind"] != "buy":
                continue
            wallet = a.get("tx_from_address")
            usd = float(a.get("volume_in_usd") or 0)
            if wallet and wallet not in insiders and usd >= MIN_REAL_BUYER_USD:
                n_alerts += 1
                log(f"*** 疑似真买家信号#{n_alerts} *** 钱包{wallet[:10]}... 买入${usd:,.2f} "
                    f"(不在已知操盘方名单里,tx={a['tx_hash'][:12]}...)")
        first_pass = False
        time.sleep(POLL_SEC)

    log(f"=== 监控窗口结束,共{n_alerts}次疑似真买家信号 ===")


if __name__ == "__main__":
    main()
