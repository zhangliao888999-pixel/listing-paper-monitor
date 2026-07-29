# -*- coding: utf-8 -*-
"""2026-07-29新增: "操盘方拉升生命周期"数据库——用户提出的下一步重点:早期K线
形态和"已经跑了几小时"都不能预判会不会崩(USOS爬了5小时、涨2.2倍之后才在第6
小时单小时归零,过程中跟TNOS/GDWR长得一模一样),真正能不能提前埋伏/提前跑掉,
得靠扎实的数据统计,不是靠猜。

但GMGN的钱包数据(卖出比例、成本)只给"当前快照",没有历史时间序列——没法回头
把TNOS/GDWR过去几小时的卖出比例找出来,只能从现在开始持续记录,攒够跨度之后
才能算出真正的统计规律(比如"卖出比例到多少、真买家信号攒到多少,平均还有多久
崩盘")。这个脚本需要反复运行(建议每15-30分钟跑一次,类似screener.py那样挂
定时任务)才能积累出有意义的时间序列,不是跑一次就有用的。

持久化文件: pump_lifecycle.json —— 每个匹配"操盘方拉升"特征的池子,记录完整的
历史快照序列(价格/流动性/操盘方卖出比例/真买家信号量),直到判定死亡为止。

用法: python lifecycle_logger.py [screener_state_local.json路径]
"""
import json
import sys
import time
import statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from check_coin import GT_BASE, S, GMGN_S, get, check_pool_and_mint
from operator_registry import matches_pump_signature, matches_early_signature, matches_origin_mcap_signature, rugcheck_creator

HERE = Path(__file__).parent
LIFECYCLE_F = HERE / "pump_lifecycle.json"

DEATH_LIQ_USD = 2000.0        # 流动性跌破这个数,判定已经死透
DEATH_DRAWDOWN_PCT = 0.95     # 或者价格从历史最高点跌了95%以上,也判定死透


def load_lifecycle():
    if LIFECYCLE_F.exists():
        return json.loads(LIFECYCLE_F.read_text(encoding="utf-8"))
    return {}


def save_lifecycle(db):
    LIFECYCLE_F.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")


def get_insider_sell_pct(mint):
    """复用今晚验证过的方法: bundler/transfer_in标记的钱包,合计投入vs合计已卖出。
    排除掉那种total_cost远大于total_volume的脏数据钱包(TNOS的creator钱包就踩过
    这个坑),避免个别异常值把统计拉偏。"""
    d = get(GMGN_S, f"https://gmgn.ai/vas/api/v1/token_traders/sol/{mint}", {"limit": 40})
    rows = (d or {}).get("data", {}).get("list", [])
    insiders = [r for r in rows if any(t in (r.get("maker_token_tags") or [])
               for t in ("bundler", "transfer_in", "creator", "dev_team"))]
    # 只用total_cost在合理范围内的(< $1M,过滤掉刷量刷出来的天文数字)
    clean = [r for r in insiders if 0 < (r.get("total_cost") or 0) < 1_000_000]
    if not clean:
        return None, 0
    total_cost = sum(r.get("total_cost") or 0 for r in clean)
    total_sold = sum((r.get("total_cost") or 0) * (r.get("sell_amount_percentage") or 0) for r in clean)
    return (total_sold / total_cost if total_cost else None), len(clean)


def snapshot_pool(addr, mint):
    attrs, _ = check_pool_and_mint(addr)
    if not attrs:
        return None
    try:
        price = float(attrs.get("base_token_price_usd") or 0)
        liq = float(attrs.get("reserve_in_usd") or 0)
    except (TypeError, ValueError):
        return None
    sell_pct, n_insiders = get_insider_sell_pct(mint)
    return {
        "ts": time.time(), "price": price, "liq": liq,
        "insider_sell_pct": sell_pct, "n_insiders": n_insiders,
        "locked_liq_pct": attrs.get("locked_liquidity_percentage"),
        "h1_chg": (attrs.get("price_change_percentage") or {}).get("h1"),
    }


def update_death_status(entry):
    hist = entry["history"]
    if not hist:
        return
    peak_price = max(h["price"] for h in hist if h.get("price"))
    cur = hist[-1]
    if cur.get("liq", 0) < DEATH_LIQ_USD or (peak_price > 0 and cur.get("price", 0) < peak_price * (1 - DEATH_DRAWDOWN_PCT)):
        if entry["status"] != "dead":
            entry["status"] = "dead"
            entry["died_at"] = cur["ts"]
            entry["peak_price"] = peak_price
            entry["hours_alive"] = (cur["ts"] - entry["first_seen"]) / 3600


def scan_and_log(state_path, max_new_scan=300):
    """两步走: 1) 从追踪池子里找新的匹配"操盘方拉升"特征的池子,加入lifecycle库
    2) 给库里所有还"活着"的池子记一条新快照,顺便判断有没有刚刚死亡"""
    db = load_lifecycle()

    # 第一步: 发现新样本
    if Path(state_path).exists():
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
        tracked = data.get("tracked", {})
        n_new, n_new_early, n_new_mcap = 0, 0, 0
        for i, (addr, w) in enumerate(list(tracked.items())[:max_new_scan]):
            if addr in db:
                continue
            attrs, mint = check_pool_and_mint(addr)
            time.sleep(0.2)
            if not attrs or not mint:
                continue
            age_minutes = (time.time() - w.get("created", time.time())) / 60
            # 2026-07-29新增: 后期版本(要求h6>=50%)对刚发出来1-30分钟的新币用不上,
            # 这些窗口还没积累够数据。用户明确要求不用每秒盯,1-30分钟查一次能发现
            # 大多数案例(今晚TNOS/GDWR/CXMT都是几分钟内bundler买单就动了,不是延迟
            # 半小时才启动),漏掉故意延迟启动的操盘方可以接受。
            # 2026-07-29新增: 用户提出"起点MCAP应该都很高"这个思路,拿DINO/Look!
            # (死币,起点MCAP$2000级别) vs GDWR/TNOS(活下来的,起点MCAP$60万-$860万
            # 级别)验证后区分度极其干净,而且开盘头10分钟就能查,比等15-90分钟的
            # 涨幅百分比快得多,优先判断。
            is_mcap = matches_origin_mcap_signature(attrs, age_minutes)
            is_early = matches_early_signature(attrs, age_minutes)
            is_late = matches_pump_signature(attrs)
            if not (is_mcap or is_early or is_late):
                continue
            found_via = "origin_mcap" if is_mcap else ("early" if is_early else "late")
            db[addr] = {"name": w.get("name"), "mint": mint, "first_seen": time.time(),
                       "status": "alive", "history": [], "found_via": found_via}
            n_new += 1
            if is_mcap:
                n_new_mcap += 1
            elif is_early:
                n_new_early += 1
        print(f"新发现符合特征的池子: {n_new}个(起点MCAP发现{n_new_mcap}个,早期1-90分钟发现{n_new_early}个)")

    # 第二步: 给所有还活着的池子记一条新快照
    n_updated, n_died_this_round = 0, 0
    for addr, entry in db.items():
        if entry["status"] == "dead":
            continue
        snap = snapshot_pool(addr, entry["mint"])
        time.sleep(0.3)
        if not snap:
            continue
        entry["history"].append(snap)
        was_alive = entry["status"] == "alive"
        update_death_status(entry)
        n_updated += 1
        if was_alive and entry["status"] == "dead":
            n_died_this_round += 1
            print(f"*** {entry['name']} 判定死亡: 存活{entry['hours_alive']:.1f}小时, "
                 f"峰值${entry['peak_price']:.10g} ***")

    save_lifecycle(db)
    n_alive = sum(1 for v in db.values() if v["status"] == "alive")
    n_dead = sum(1 for v in db.values() if v["status"] == "dead")
    print(f"本轮更新{n_updated}个池子的快照,本轮新增死亡{n_died_this_round}个")
    print(f"库存总数: {len(db)}  存活{n_alive}  已死亡{n_dead}")


if __name__ == "__main__":
    state_path = sys.argv[1] if len(sys.argv) > 1 else str(HERE.parent / "screener_state_local.json")
    scan_and_log(state_path)
