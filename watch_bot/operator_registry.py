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
    # 2026-07-29放宽(用户明确要求"纸盘可以再大胆一些"): 门槛从liq>=20000/h6>=50
    # 降到liq>=10000/h6>=30——纸盘不动真钱,宁可多抓一些弱样本(哪怕像TIKTOK那种
    # 单钱包小体量的)进来跑,数据比精度更重要,弱样本本身也是有价值的对照组。
    return locked >= 90 and liq >= 10000 and h6 >= 30


def matches_early_signature(attrs, age_minutes):
    """2026-07-29新增,同日晚些时候修正: 一开始设的1-30分钟+m30>=30%门槛,用TNOS
    自己的真实早期数据回溯验证后发现是错的——TNOS的bundler买盘创世后2分钟就已经
    在动(涨幅曲线一路平滑爬升,没有断档),但爬升速率是温和的(约每分钟0.4-0.5个
    百分点,不是爆发式拉升),累计涨幅要到创世后52分钟才第一次突破30%,30分钟时
    只有20.5%。原来的窗口设计只能抓"爆发型"早期拉升,像TNOS这种"慢慢阴柔式"的
    完全抓不到——用户亲自要求验证并放宽,这不是猜测,是用真实历史K线倒推算出来的。
    修正后: 窗口放宽到1-90分钟;30分钟以内仍用m30字段但门槛降到15%(实测TNOS
    30分钟时20.5%,能稳定命中);超过30分钟改用h1字段(对不到90分钟大的池子,
    h1基本等价于"接近自创世以来"的涨幅),门槛25%(实测TNOS 52分钟31%、90分钟
    47%,能稳定命中)。"""
    if not (1 <= age_minutes <= 90):
        return False
    try:
        locked = float(attrs.get("locked_liquidity_percentage") or 0)
        liq = float(attrs.get("reserve_in_usd") or 0)
    except (TypeError, ValueError):
        return False
    if locked < 90 or liq < 5000:
        return False
    pct = attrs.get("price_change_percentage") or {}
    try:
        if age_minutes <= 30:
            return float(pct.get("m30") or 0) >= 15
        else:
            return float(pct.get("h1") or 0) >= 25
    except (TypeError, ValueError):
        return False


def matches_origin_mcap_signature(attrs, age_minutes, min_mcap=50000):
    """2026-07-29新增: 用户提出"这些币前期MCAP应该都很高"这个思路,拿4个真实
    案例验证后发现区分度极其干净,而且比等涨幅百分比快得多——开盘第一分钟就能查:
      DINO(没人理,死了)        起点MCAP $2,075
      Look!(没人理,死了)       起点MCAP $2,064
      GDWR(跑了11小时,真实崩盘) 起点MCAP $597,630  (死币的~290倍)
      TNOS(跑了9.5小时+,还活着) 起点MCAP $8,624,530 (死币的~4,000倍)
    逻辑: 操盘方一开局砸的真金白银越多,起点MCAP越高,说明这不是随手一发的
    小打小闹,越有实力/动机把"表演"撑久去吸引真买家。样本量只有4个,门槛先
    定保守一点($50000,死币的~25倍、比GDWR低一个数量级),后续攒够更多样本
    再精调。这个检查不看涨幅,只看age早期阶段(<=10分钟)的fdv_usd,比
    matches_early_signature的15-90分钟等待窗口快得多,可以更早触发。"""
    if age_minutes > 10:
        return False
    try:
        fdv = float(attrs.get("fdv_usd") or 0)
        locked = float(attrs.get("locked_liquidity_percentage") or 0)
    except (TypeError, ValueError):
        return False
    return locked >= 90 and fdv >= min_mcap


def matches_pregrad_ramp_signature(attrs, age_minutes):
    """2026-07-29新增: 用户提出的"毕业前抢筹,毕业前卖回curve,不碰毕业瞬间"打法——
    REDO/FRANK案例分析发现,真正好赚、风险还低的那段利润在bonding curve内部本身
    (REDO创世到毕业前涨了993%),毕业瞬间(新池子)反而是风险最高、赏金最小的一段
    (FRANK案例里毕业跟砸盘是同一秒,根本没有反应时间)。这条信号只负责找"正在被
    bundler快速拉升的、还没毕业的极新池子",不要求起点MCAP高(REDO/FRANK起点都才
    几千到一万出头,远低于matches_origin_mcap_signature的$50000门槛)——门槛低是
    故意的,反正下游用极小仓位+严格止损止盈,抓错了代价也很小,漏掉了才是真正的
    机会成本。窗口卡得很紧(<=3分钟)是因为这整个"创世->毕业"的过程实测只有
    2-4分钟量级,超过这个窗口再判断"是不是正在被拉"意义不大(m5这个字段这么早
    基本等价于"自创世以来涨幅")。"""
    if not (0 < age_minutes <= 3):
        return False
    try:
        m5 = float((attrs.get("price_change_percentage") or {}).get("m5") or 0)
        tx_m5 = attrs.get("transactions", {}).get("m5") or {}
        n_tx = int(tx_m5.get("buys") or 0) + int(tx_m5.get("sells") or 0)
        liq = float(attrs.get("reserve_in_usd") or 0)
    except (TypeError, ValueError):
        return False
    # 2026-07-29白天补: 419笔实盘数据回看发现LIQ_DEAD退出里46%(45/98)是"入场即死"
    # (持仓<10秒,entry=exit=peak)——池子在被检测到的那一刻其实流动性已经没了,不是
    # 拉升途中崩的,是信号本身漏判。加一道最低流动性门槛,过滤掉这类"进场就是空气"
    # 的假信号,不改变原有涨幅/成交笔数门槛。
    return m5 >= 80 and n_tx >= 15 and liq >= 3000


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
