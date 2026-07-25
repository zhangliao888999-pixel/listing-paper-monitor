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
  流动性: >= MIN_LIQUIDITY_USD (过滤明显貔貅雏形)
  仍在被交易: 近15分钟成交量/笔数不能是0(过滤"开盘冲一下就没人管了"的死币)

本地+云端混合双跑: 设置环境变量 SCREENER_LOCAL=1 时视为本地实例，使用独立的
screener_state_local.json / screener_candidates_local.json / screener_local.log，
与云端实例(screener_state.json等，无后缀)完全隔离，避免两边同时提交同一个JSON文件
产生git冲突。看盘页面(docs/index.html)会同时拉取两份候选文件并按地址去重合并展示，
本地开机时能更高频地补上云端调度粒度不够细导致的漏检。

输出 screener_candidates(_local).json，供看盘页面直接展示。
"""
import json
import os
import time
import datetime as dt
from pathlib import Path

import requests

HERE = Path(__file__).parent
INSTANCE = "local" if os.environ.get("SCREENER_LOCAL") else "cloud"
SUFFIX = "_local" if INSTANCE == "local" else ""
OUT_F = HERE / f"screener_candidates{SUFFIX}.json"
STATE_F = HERE / f"screener_state{SUFFIX}.json"
LOG_F = HERE / f"screener{SUFFIX}.log"

GT_BASE = "https://api.geckoterminal.com/api/v2"
MIN_AGE_MIN = 8
MAX_AGE_MIN = 240        # 4小时,早期窗口过了就不再是"候选"
PRUNE_AGE_MIN = MAX_AGE_MIN + 30   # 状态里超过这个年龄的条目直接丢弃,防止无限增长
MIN_LIQUIDITY_USD = 8000
MAX_LIQUIDITY_USD = 2_000_000  # 上限:这么年轻的币出现千万/上亿级流动性基本是报价异常导致reserve_in_usd失真,不是真实候选
MIN_VOL_15M_USD = 500    # 近15分钟成交额门槛,过滤"没人交易"的死币
MIN_TX_15M = 3           # 近15分钟买卖笔数门槛,单纯"买卖笔数不为0"太松(1-2笔可能是坏数据自成交)
NEW_POOLS_PAGES = 12     # 覆盖约10分钟的新池子创建量(约24个/分钟),配合定时任务间隔
TRENDING_PAGES = 2
MULTI_CHUNK = 30         # /pools/multi 单批最多30个地址

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


def refresh_candidates(state):
    """对追踪列表中处于候选年龄窗口的池子,批量刷新最新数据并过滤"""
    tracked = state["tracked"]
    in_window = [addr for addr, w in tracked.items()
                if MIN_AGE_MIN <= (NOW - w["created"]) / 60 <= MAX_AGE_MIN]
    candidates = []
    for i in range(0, len(in_window), MULTI_CHUNK):
        chunk = in_window[i:i + MULTI_CHUNK]
        d = get(f"{GT_BASE}/networks/solana/pools/multi/{','.join(chunk)}")
        rows = (d or {}).get("data") or []
        for row in rows:
            addr = pool_id_addr(row)
            w = tracked.get(addr)
            if not w:
                continue
            p = extract_stats(addr, w["name"], w["created"], row.get("attributes", {}))
            if not (MIN_LIQUIDITY_USD <= p["liq"] <= MAX_LIQUIDITY_USD):
                continue
            if p["vol_15m"] < MIN_VOL_15M_USD and (p["buys_15m"] + p["sells_15m"]) < MIN_TX_15M:
                continue  # 近15分钟没什么真实成交,判定为已死(或坏数据)
            candidates.append(p)
        time.sleep(0.5)
    return candidates


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
    cands = refresh_candidates(state)
    n_pruned = prune(state)
    save_state(state)

    cands.sort(key=lambda p: p["chg_1h"], reverse=True)
    out = {"updated_at": NOW, "updated_at_str": NOW_STR, "n_candidates": len(cands),
          "candidates": cands}
    OUT_F.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"CYCLE OK: 新发现{n_new} 追踪中{len(state['tracked'])} 清理{n_pruned} -> 筛出候选{len(cands)}")


if __name__ == "__main__":
    main()
