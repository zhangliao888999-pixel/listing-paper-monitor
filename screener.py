# -*- coding: utf-8 -*-
"""实时候选币筛选器（不做交易决策，不模拟买卖，只负责选币）。

用户明确定位：新币交易的买卖时机由人工盯盘判断（庄家/散户心理无法量化），
本工具唯一的工作是把"没人交易的、几分钟就死"的垃圾过滤掉，把还活着、有真实
成交、流动性够格的候选币列出来，供人工决策。

架构说明(重要): GeckoTerminal的 new_pools 接口只是一个滚动窗口，无论翻多少页，
实测能拉到的最老池子也就 5~6 分钟(Solana每分钟约新增24个池子，接口深度有限)。
要判断"一个币有没有在开盘几分钟后就死掉"，必须跨多个采集周期持续观察同一批池子，
单次快照做不到。所以本工具改为两层结构，状态持久化在 screener_state.json:

  发现层: 每轮扫 new_pools + trending_pools，把没见过的池子地址记入追踪列表(带首次发现时间)
  刷新层: 对追踪列表中"年龄落在候选窗口内"的池子，用 /pools/multi 批量接口(每批<=30个)
          重新拉取最新数据，按流动性+近期成交活跃度过滤，活着的才输出为候选
  清理层: 追踪列表中年龄超出候选窗口太多的条目从状态里删掉，防止状态无限增长

筛选标准:
  年龄: MIN_AGE_MIN ~ MAX_AGE_MIN 之间(太新还看不出死活;太老早期窗口已经过了)
  流动性: MIN_LIQUIDITY_USD ~ MAX_LIQUIDITY_USD 之间(下限过滤貔貅雏形,上限过滤报价异常的假流动性)
  仍在被交易: 近15分钟买卖笔数 >= MIN_TX_15M，纯按笔数论，不允许"金额大但笔数少"顶替
             (曾经出现过AAIF/USDC只有1笔巨额买入就通过筛选的案例，用户明确要求剔除)

本地+云端混合双跑: 设置环境变量 SCREENER_LOCAL=1 时视为本地实例，使用独立的
screener_state_local.json / screener_candidates_local.json / screener_local.log，
与云端实例(screener_state.json等，无后缀)完全隔离，避免两边同时提交同一个JSON文件
产生git冲突。看盘页面(docs/index.html)会同时拉取两份候选文件并按地址去重合并展示，
本地开机时能更高频地补上云端调度粒度不够细导致的漏检。

尽调数据(check_coin.py同款检查，直接复用): 每个候选币附带K线脚本化检测+GMGN主力持仓
状态(见check_coin.py顶部说明——只报告事实，不产出买卖建议)。GMGN有明确的限速/封锁
(实测连续查十几次后403)，所以尽调不是每轮都对所有候选重查一遍，而是缓存在
screener_enrich_cache(_local).json 里，每个币最多每 ENRICH_REFRESH_MIN 分钟重查一次，
每轮最多查 MAX_ENRICH_PER_CYCLE 个(优先给从没查过的新候选)。

输出 screener_candidates(_local).json，供看盘页面直接展示。
"""
import json
import os
import time
import datetime as dt
from pathlib import Path

import requests

from check_coin import check_staircase, check_wallets, check_scalping

HERE = Path(__file__).parent
INSTANCE = "local" if os.environ.get("SCREENER_LOCAL") else "cloud"
SUFFIX = "_local" if INSTANCE == "local" else ""
OUT_F = HERE / f"screener_candidates{SUFFIX}.json"
STATE_F = HERE / f"screener_state{SUFFIX}.json"
LOG_F = HERE / f"screener{SUFFIX}.log"
ENRICH_CACHE_F = HERE / f"screener_enrich_cache{SUFFIX}.json"

GT_BASE = "https://api.geckoterminal.com/api/v2"
MIN_AGE_MIN = 8
MAX_AGE_MIN = 240        # 4小时,早期窗口过了就不再是"候选"
PRUNE_AGE_MIN = MAX_AGE_MIN + 30   # 状态里超过这个年龄的条目直接丢弃,防止无限增长
MIN_LIQUIDITY_USD = 8000
MAX_LIQUIDITY_USD = 2_000_000  # 上限:这么年轻的币出现千万/上亿级流动性基本是报价异常导致reserve_in_usd失真,不是真实候选
MIN_TX_15M = 5           # 近15分钟买卖笔数门槛,纯按笔数卡(不再允许用成交额金额顶替笔数不足)
NEW_POOLS_PAGES = 12     # 覆盖约10分钟的新池子创建量(约24个/分钟),配合定时任务间隔
TRENDING_PAGES = 2
MULTI_CHUNK = 30         # /pools/multi 单批最多30个地址
MAX_ENRICH_PER_CYCLE = 6   # 每轮最多做几个尽调检查(GMGN容易被限速/封锁,不能贪多)
ENRICH_REFRESH_MIN = 20    # 同一个币的GMGN主力持仓尽调结果最多每隔这么久重查一次
GT_REFRESH_MIN = 30        # K线形态+刷量检测(GeckoTerminal)的重查间隔,实测对所有候选每轮都查
                           # 会把单轮耗时从1-3分钟推到5分钟+,所以也要缓存,不能来一轮查一轮

EARLY_CHECK_MIN_AGE = 3    # 太新(<3分钟)基本还没几笔成交,查了也白查
EARLY_CHECK_MAX_AGE = MIN_AGE_MIN  # 超过这个年龄就已经进入正常候选流程,不需要"早期"检测了
MAX_EARLY_CHECK_PER_CYCLE = 10     # 实测30个/轮明显拖慢单轮耗时(可能顶到GeckoTerminal限速retry),先保守一点

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                  "Accept": "application/json;version=20230302"})
NOW = int(time.time())
NOW_STR = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def log(msg):
    line = f"[{NOW_STR}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        pass
    with LOG_F.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(3 * (i + 1))
                continue
            return None
        except (requests.RequestException, json.JSONDecodeError):
            time.sleep(1.5 * (i + 1))
    return None


def load_state():
    if STATE_F.exists():
        try:
            return json.loads(STATE_F.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"tracked": {}}


def save_state(state):
    STATE_F.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def pool_id_addr(row):
    return row["id"].split("_")[-1]


def extract_stats(addr, name, created, attrs):
    vol = attrs.get("volume_usd") or {}
    chg = attrs.get("price_change_percentage") or {}
    tx = attrs.get("transactions") or {}
    age_min = (NOW - created) / 60
    return {
        "addr": addr, "name": name, "age_min": age_min, "created": created,
        "price": float(attrs.get("base_token_price_usd") or 0),
        "liq": float(attrs.get("reserve_in_usd") or 0),
        "vol_15m": float(vol.get("m15") or 0), "vol_1h": float(vol.get("h1") or 0),
        "chg_15m": float(chg.get("m15") or 0), "chg_1h": float(chg.get("h1") or 0),
        "buys_15m": (tx.get("m15") or {}).get("buys", 0),
        "sells_15m": (tx.get("m15") or {}).get("sells", 0),
        "dex_url": f"https://www.geckoterminal.com/solana/pools/{addr}",
    }


def discover(state):
    """扫 new_pools + trending_pools，把没见过的池子地址记入追踪状态"""
    tracked = state["tracked"]
    n_new = 0
    for kind, url, pages in [("new", f"{GT_BASE}/networks/solana/new_pools", NEW_POOLS_PAGES),
                             ("trend", f"{GT_BASE}/networks/solana/trending_pools", TRENDING_PAGES)]:
        for page in range(1, pages + 1):
            d = get(url, {"page": page})
            rows = (d or {}).get("data") or []
            if not rows:
                break
            for row in rows:
                addr = pool_id_addr(row)
                if addr in tracked:
                    continue
                attrs = row.get("attributes", {})
                try:
                    created = dt.datetime.fromisoformat(
                        attrs["pool_created_at"].replace("Z", "+00:00")).timestamp()
                except (KeyError, ValueError):
                    continue
                tracked[addr] = {"name": attrs.get("name", "?"), "created": created}
                n_new += 1
            time.sleep(0.35)
    return n_new


def check_early_bots(state):
    """新币刚上线的3-8分钟内先查一次是不是机器人刷量(只用GeckoTerminal,不占GMGN配额)。
    等一个池子正式进入候选年龄窗口(>=MIN_AGE_MIN)才检测就太晚了——用户明确要求
    "新币刚上线就要盯着机器人"，所以在候选流程之前单独跑一遍，检测结果记在
    tracked[addr]里跟着这个池子一路带到候选阶段(见refresh_candidates的early_bot_flag)。
    每个池子只查一次(early_checked标记)，防止同一个池子被反复检测浪费配额。"""
    tracked = state["tracked"]
    todo = [addr for addr, w in tracked.items()
           if not w.get("early_checked")
           and EARLY_CHECK_MIN_AGE <= (NOW - w["created"]) / 60 <= EARLY_CHECK_MAX_AGE]
    todo.sort(key=lambda addr: tracked[addr]["created"])  # 快要超过窗口的优先查,不然就再也没机会了
    to_check = todo[:MAX_EARLY_CHECK_PER_CYCLE]
    n_bot = 0
    for addr in to_check:
        result = check_scalping(addr)
        tracked[addr]["early_checked"] = True
        tracked[addr]["early_bot_flag"] = result.get("flag", False)
        if result.get("flag"):
            n_bot += 1
        time.sleep(0.3)
    return len(to_check), n_bot


def refresh_candidates(state):
    """对追踪列表中处于候选年龄窗口的池子,批量刷新最新数据并过滤"""
    tracked = state["tracked"]
    in_window = [addr for addr, w in tracked.items()
                if MIN_AGE_MIN <= (NOW - w["created"]) / 60 <= MAX_AGE_MIN]
    candidates = []
    for i in range(0, len(in_window), MULTI_CHUNK):
        chunk = in_window[i:i + MULTI_CHUNK]
        d = get(f"{GT_BASE}/networks/solana/pools/multi/{','.join(chunk)}", {"include": "base_token"})
        rows = (d or {}).get("data") or []
        mint_by_id = {inc["id"]: inc["attributes"].get("address")
                     for inc in ((d or {}).get("included") or []) if inc.get("type") == "token"}
        for row in rows:
            addr = pool_id_addr(row)
            w = tracked.get(addr)
            if not w:
                continue
            p = extract_stats(addr, w["name"], w["created"], row.get("attributes", {}))
            if not (MIN_LIQUIDITY_USD <= p["liq"] <= MAX_LIQUIDITY_USD):
                continue
            if (p["buys_15m"] + p["sells_15m"]) < MIN_TX_15M:
                continue  # 近15分钟买卖笔数不够,判定为已死(或坏数据),不看金额
            base_tok_id = (row.get("relationships", {}).get("base_token", {}).get("data", {}) or {}).get("id")
            p["mint"] = mint_by_id.get(base_tok_id)
            p["early_bot_flag"] = w.get("early_bot_flag")  # None=太快进候选没来得及早期检测
            candidates.append(p)
        time.sleep(0.5)
    return candidates


def load_enrich_cache():
    if ENRICH_CACHE_F.exists():
        try:
            return json.loads(ENRICH_CACHE_F.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_enrich_cache(cache):
    ENRICH_CACHE_F.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def enrich_candidates(candidates):
    """给候选币附带check_coin.py同款尽调(K线脚本化/刷量机器人/GMGN主力持仓状态)。
    只报告事实,不产出买卖建议——只是把check_coin.py里已有的检查搬到看盘页面上
    自动跑，用户不用每次手动敲命令。

    check_staircase/check_scalping只用GeckoTerminal(没有GMGN那种明显限速)，
    所以每轮对所有候选都查，不用等排队；check_wallets要查GMGN(连续查十几次
    就403)，所以单独走缓存+限量+优先级那一套，只有它才会有"排队中"的情况。
    2026-07-26研究证实高频刷量机器人赢面只有约50%、均值净亏，且GMGN的历史盈利
    字段对这类钱包完全失真——机器人活跃是警示信号，不是主力信号，所以
    scalping_flag会用来把这个币排到候选列表靠后位置，而不只是摆在旁边好看。
    """
    cache = load_enrich_cache()
    stale = [c for c in candidates if NOW - cache.get(c["addr"], {}).get("checked_at", 0) > ENRICH_REFRESH_MIN * 60]
    stale.sort(key=lambda c: cache.get(c["addr"], {}).get("checked_at", 0))  # 从没查过的(0)排最前面
    to_check = stale[:MAX_ENRICH_PER_CYCLE]

    for c in to_check:
        wallets = check_wallets(c.get("mint"))
        e = cache.setdefault(c["addr"], {})  # 用setdefault+update,不要整个替换,免得把下面GT那部分缓存的字段冲掉
        e.update({
            "checked_at": NOW,
            "n_traders": wallets.get("n_traders"), "n_suspicious": wallets.get("n_suspicious"),
            "exit_ratio": wallets.get("exit_ratio"), "wallet_verdict": wallets.get("verdict"),
        })
        time.sleep(2)  # GMGN限速敏感,查完一个歇一下

    for c in candidates:
        e = cache.setdefault(c["addr"], {})
        if NOW - e.get("gt_checked_at", 0) > GT_REFRESH_MIN * 60:
            # 实测"每轮都对所有候选查K线+刷量"直接把单轮耗时从1-3分钟推到5分钟+
            # (每个候选2个GeckoTerminal请求,候选一多就堆起来了),所以这两项也要缓存/限频，
            # 跟GMGN那部分用同一个cache文件但独立的时间戳
            stair = check_staircase(c["addr"])
            scalp = check_scalping(c["addr"])
            e["gt_checked_at"] = NOW
            e["staircase_flag"] = stair.get("flag", False)
            e["scalping_flag"] = scalp.get("flag", False)
            time.sleep(0.3)
        c["staircase_flag"] = e.get("staircase_flag", False)
        c["scalping_flag"] = e.get("scalping_flag", False)
        if "checked_at" in e:
            c["diligence"] = {
                "checked_min_ago": round((NOW - e["checked_at"]) / 60, 1),
                "staircase_flag": c["staircase_flag"], "scalping_flag": c["scalping_flag"],
                "n_traders": e["n_traders"], "n_suspicious": e["n_suspicious"],
                "exit_ratio": e["exit_ratio"], "wallet_verdict": e["wallet_verdict"],
            }
        else:
            c["diligence"] = None  # GMGN那部分还没排到,K线/刷量检查已经查完了(见上面两个字段)
        time.sleep(0.3)

    # 缓存只留当前还在候选窗口里的币,过期的清掉防止无限增长
    live_addrs = {c["addr"] for c in candidates}
    for addr in list(cache.keys()):
        if addr not in live_addrs:
            del cache[addr]
    save_enrich_cache(cache)


def prune(state):
    tracked = state["tracked"]
    dead = [addr for addr, w in tracked.items()
           if (NOW - w["created"]) / 60 > PRUNE_AGE_MIN]
    for addr in dead:
        del tracked[addr]
    return len(dead)


def main():
    state = load_state()
    n_new = discover(state)
    n_early_checked, n_early_bot = check_early_bots(state)
    cands = refresh_candidates(state)
    n_pruned = prune(state)
    save_state(state)
    enrich_candidates(cands)

    # 2026-07-26改向: 前4条交易策略作废,只留策略D(薅机器人羊毛)，它专门要找
    # 交易活跃(尤其是有机器人在刷)的币下手，所以候选排序从"机器人排最后"反过来
    # 改成"按近15分钟买卖笔数(最直接的活跃度指标)从高到低排"，机器人币笔数天然很高，
    # 会自然排到前面——不再是要回避的信号，是这一条策略现在唯一要找的信号。
    def is_flagged(p):
        return bool(p.get("scalping_flag") or p.get("staircase_flag") or p.get("early_bot_flag"))
    cands.sort(key=lambda p: -(p["buys_15m"] + p["sells_15m"]))
    out = {"updated_at": NOW, "updated_at_str": NOW_STR, "n_candidates": len(cands),
          "candidates": cands}
    OUT_F.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n_diligenced = sum(1 for c in cands if c.get("diligence"))
    n_bot = sum(1 for c in cands if is_flagged(c))
    n_early_bot_in_cands = sum(1 for c in cands if c.get("early_bot_flag"))
    log(f"CYCLE OK: 新发现{n_new} 追踪中{len(state['tracked'])} 清理{n_pruned} -> 筛出候选{len(cands)}"
       f"(已尽调{n_diligenced} 疑似机器人{n_bot} 出生即机器人{n_early_bot_in_cands}) "
       f"早期检测{n_early_checked}个(其中机器人{n_early_bot}个)")


if __name__ == "__main__":
    main()
