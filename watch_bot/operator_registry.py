# -*- coding: utf-8 -*-
"""2026-07-28新增: "找人不找币"的候选来源——收集反复发币的操盘方钱包,但只留
账户整体是赚钱的那批,不是光看发币数量。

用户提出的思路: 与其等一个新币攒够历史数据、通过我们那套过滤器才发现,不如直接
盯着"已知的操盘方钱包"，他们一发新币就第一时间跟进。但今晚同一个晚上查到的
bored100x(发119个币,账户净赚$3,979)和fatnesss(发50个币,账户净亏$24,407)
说明"发币次数多"不等于"这个人靠谱"——两个都是批量发币老手,一个赚一个亏得很惨。
所以入库标准不能只看creator_created_count,必须同时看这个钱包自己账户整体的
realized_profit是不是正的。

数据来源:
  - RugCheck(api.rugcheck.xyz): 给一个mint地址,查creator(创建者钱包)
  - GMGN wallet_stat: 给一个钱包地址,查creator_created_count(发过几个币)和
    realized_profit(账户整体历史盈亏,跨所有代币,不只是发币这一项)

持久化文件: operators_watchlist.json —— 长期滚动积累,不是每次重新生成。
"""
import json
import time
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from check_coin import GMGN_S, get

HERE = Path(__file__).parent
WATCHLIST_F = HERE / "operators_watchlist.json"

MIN_CREATED_COUNT = 3       # 至少发过这么多个币,才有"反复操作"的统计意义,偶然发一次不算
MIN_REALIZED_PROFIT_USD = 500.0  # 账户整体历史盈亏(跨所有代币)必须是正的且有意义的量级,
                                   # 太小的正数可能只是噪音,不足以说明这人真的会玩


def rugcheck_creator(mint):
    """用完整版report接口(不是summary版)——summary版不带creator字段,今晚分析
    meme/Kvro、LOODY、DINO这几个都是用完整版接口才拿到creator的。"""
    try:
        r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report",
                         timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        return r.json().get("creator")
    except requests.RequestException:
        return None


def gmgn_wallet_stat(wallet):
    d = get(GMGN_S, f"https://gmgn.ai/api/v1/wallet_stat/sol/{wallet}/all")
    if not d or d.get("code") != 0:
        return None
    return d.get("data", {})


def load_watchlist():
    if WATCHLIST_F.exists():
        return json.loads(WATCHLIST_F.read_text(encoding="utf-8"))
    return {}


def save_watchlist(wl):
    WATCHLIST_F.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")


def consider_creator(wallet, watchlist, source_mint=None):
    """给一个创建者钱包地址,查它的历史战绩,决定要不要收进观察名单。
    返回True表示已经是/新收录为"值得跟"的操盘方,False表示查过但不达标或数据不够。"""
    if wallet in watchlist and watchlist[wallet].get("last_checked", 0) > time.time() - 3600:
        return watchlist[wallet].get("qualified", False)  # 1小时内查过,不重复查

    stat = gmgn_wallet_stat(wallet)
    if not stat:
        return False
    created_count = stat.get("creator_created_count") or 0
    realized = stat.get("realized_profit") or 0
    total_volume = stat.get("total_volume") or 0
    pnl = stat.get("total_profit_pnl")
    # 2026-07-28修复: 6Wg4aeZ29W这个钱包暴露的漏洞——total_volume只有$45.87却显示
    # realized_profit=$52,400,数学上不可能(交易量比利润还小几千倍),total_profit_pnl
    # 还是-189%(跟正的realized_profit直接矛盾),tags里带fresh_wallet,大概率是GMGN
    # 对新钱包的数据还没同步好/脏数据。原来的判断只查realized_profit一个字段,没有
    # 交叉校验,把这种脏数据当真实战绩收录进去了。现在加两条基本的自洽性检查:
    # 利润不能超过交易量(不然数学上不成立),而且总盈亏比例也必须是正的。
    sane = total_volume >= realized and (pnl is None or pnl > 0)
    qualified = created_count >= MIN_CREATED_COUNT and realized >= MIN_REALIZED_PROFIT_USD and sane

    entry = watchlist.get(wallet, {"first_seen_mint": source_mint, "seen_tokens": []})
    entry.update({
        "creator_created_count": created_count,
        "realized_profit": realized,
        "total_profit_pnl": stat.get("total_profit_pnl"),
        "total_volume": total_volume,
        "tags": stat.get("tags"),
        "qualified": qualified,
        "last_checked": time.time(),
    })
    if source_mint and source_mint not in entry["seen_tokens"]:
        entry["seen_tokens"].append(source_mint)
    watchlist[wallet] = entry
    return qualified


def scan_from_candidates_file(path):
    """从screener.py的候选文件里挖创建者钱包,批量喂给consider_creator。
    候选文件里已经有mint字段,不用额外请求。"""
    if not Path(path).exists():
        print(f"找不到 {path}")
        return
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cands = data.get("candidates", [])
    watchlist = load_watchlist()
    n_qualified_new = 0
    for c in cands:
        mint = c.get("mint")
        if not mint:
            continue
        creator = rugcheck_creator(mint)
        if not creator:
            continue
        was_qualified = watchlist.get(creator, {}).get("qualified", False)
        qualified = consider_creator(creator, watchlist, source_mint=mint)
        if qualified and not was_qualified:
            n_qualified_new += 1
            print(f"新收录操盘方: {creator[:10]}... "
                 f"(发过{watchlist[creator]['creator_created_count']}个币,"
                 f"账户历史盈亏+${watchlist[creator]['realized_profit']:,.0f}) "
                 f"—— 来自候选 {c.get('name')}")
        time.sleep(0.3)
    save_watchlist(watchlist)
    n_total_qualified = sum(1 for v in watchlist.values() if v.get("qualified"))
    print(f"\n本轮新增达标操盘方: {n_qualified_new}  当前观察名单里达标总数: {n_total_qualified}  (总收录{len(watchlist)}个钱包)")


def matches_pump_signature(attrs):
    """判断这个池子像不像今晚TNOS这种"操盘方对倒拉升,真买家逐步跟进"的走势——
    不要求通过我们那套严格的交易过滤器(那是给"能不能买"用的),这里只是想找到
    "创建者操盘手法值得记录"的样本,门槛要松一些: 锁仓100%(至少不是随时能被
    抽干的类型) + 有实质涨幅(说明真的拉起来过,不是发出来就死) + 流动性不算
    太小(说明真的有资金规模,不是DINO/Look!那种没人理的量级)。"""
    try:
        locked = float(attrs.get("locked_liquidity_percentage") or 0)
        liq = float(attrs.get("reserve_in_usd") or 0)
        h6 = float((attrs.get("price_change_percentage") or {}).get("h6") or 0)
    except (TypeError, ValueError):
        return False
    return locked >= 90 and liq >= 20000 and h6 >= 50


def matches_early_signature(attrs, age_minutes):
    """2026-07-29新增: matches_pump_signature要求h6涨幅>=50%,对刚发出来1-30分钟
    的新币根本用不上(池子太年轻,GeckoTerminal的h1/h6/h24这些窗口还没积累够数据,
    会直接跟m5/m15/m30的值一样)。用户明确要求"不用每秒盯,1-30分钟内查一次能
    发现大多数"——今晚查过的TNOS/GDWR/CXMT,创建后几分钟内bundler买单就已经在
    动了,不是等半小时才启动,所以这个早期窗口本身就能捕捉到大部分案例,漏掉
    "故意延迟启动"的操盘方可以接受(先解决大多数,不追求100%)。
    早期判断门槛(比后期版本更松,因为量还没起来): 锁仓100% + 已经有实质涨幅
    (用m30,这是1-30分钟窗口里最可能有真实数据的字段) + 流动性哪怕还小也要
    有个下限,排除掉像CXMT那种量小到几乎没人的池子。"""
    if not (1 <= age_minutes <= 30):
        return False
    try:
        locked = float(attrs.get("locked_liquidity_percentage") or 0)
        liq = float(attrs.get("reserve_in_usd") or 0)
        m30 = float((attrs.get("price_change_percentage") or {}).get("m30") or 0)
    except (TypeError, ValueError):
        return False
    return locked >= 90 and liq >= 5000 and m30 >= 30


def scan_from_tracked_state(state_path, max_scan=300, max_age_hours=48):
    """直接扫screener.py已经在追踪的池子(screener_state_local.json),不用额外
    重新去GeckoTerminal搜——这批池子本来就是screener持续在跑的,不用重复造轮子。
    对符合matches_pump_signature的池子才去查creator(省着点用RugCheck/GMGN调用),
    每个池子先查一次pools接口拿mint+涨跌幅+锁仓+流动性,这4个字段本来就在同一个
    响应里,一次请求搞定。"""
    if not Path(state_path).exists():
        print(f"找不到 {state_path}")
        return
    from check_coin import GT_BASE, S, check_pool_and_mint

    data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    tracked = data.get("tracked", {})
    now = time.time()
    # 太新的池子还没走出完整的"拉升"轨迹,没意义;太老的可能已经死透很久,优先看
    # 还在合理时间窗口内、有机会正在发生这套操作的池子
    candidates = [(addr, w) for addr, w in tracked.items()
                 if 0 < (now - w.get("created", now)) / 3600 <= max_age_hours]
    print(f"追踪池子总数: {len(tracked)}  时间窗口内({max_age_hours}h内): {len(candidates)}  本轮最多扫{max_scan}个")

    watchlist = load_watchlist()
    n_matched = 0
    n_qualified_new = 0
    for i, (addr, w) in enumerate(candidates[:max_scan]):
        attrs, mint = check_pool_and_mint(addr)
        time.sleep(0.2)  # 不管匹不匹配都停一下,1682个池子如果不间隔很容易把GT接口打到限流
        if not attrs or not mint:
            continue
        if not matches_pump_signature(attrs):
            continue
        n_matched += 1
        h6 = (attrs.get("price_change_percentage") or {}).get("h6")
        creator = rugcheck_creator(mint)
        if not creator:
            continue
        was_qualified = watchlist.get(creator, {}).get("qualified", False)
        qualified = consider_creator(creator, watchlist, source_mint=mint)
        tag = "*** 新收录 ***" if (qualified and not was_qualified) else ("已收录" if qualified else "")
        if qualified:
            print(f"{tag} {w.get('name')} (h6={h6}%) 创建者{creator[:10]}... "
                 f"发过{watchlist[creator]['creator_created_count']}个币,历史盈亏+${watchlist[creator]['realized_profit']:,.0f}")
            if not was_qualified:
                n_qualified_new += 1
        time.sleep(0.3)
        if (i + 1) % 50 == 0:
            save_watchlist(watchlist)  # 中途定期落盘,防止跑到一半中断丢进度
            print(f"  ...已扫{i+1}/{min(max_scan,len(candidates))},匹配走势特征{n_matched}个")

    save_watchlist(watchlist)
    n_total_qualified = sum(1 for v in watchlist.values() if v.get("qualified"))
    print(f"\n本轮完成: 匹配走势特征{n_matched}个池子  新增达标操盘方{n_qualified_new}  "
         f"观察名单达标总数{n_total_qualified}(总收录{len(watchlist)}个钱包)")


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "--tracked":
        state_path = _sys.argv[2] if len(_sys.argv) > 2 else str(HERE.parent / "screener_state_local.json")
        scan_from_tracked_state(state_path)
    else:
        candidates_path = _sys.argv[1] if len(_sys.argv) > 1 else str(HERE.parent / "screener_candidates_local.json")
        scan_from_candidates_file(candidates_path)
