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


if __name__ == "__main__":
    import sys as _sys
    candidates_path = _sys.argv[1] if len(_sys.argv) > 1 else str(HERE.parent / "screener_candidates_local.json")
    scan_from_candidates_file(candidates_path)
