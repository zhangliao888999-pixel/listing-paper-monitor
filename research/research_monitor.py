# -*- coding: utf-8 -*-
"""全量新币研究监控：不做交易决策，只负责持续、全面地记录每一个新上线代币的
量价关系 + 钱包画像，产出一张不断增长的结构化数据表，供后续归纳交易规则用。

架构(两层，避免对每个池子持续轮询到天荒地老，也避免请求量爆炸):
  发现层: 每轮扫 new_pools(多页) + trending_pools，全量记录进 tracked_pools.jsonl
          (不设流动性/年龄门槛，来者不拒，和策略C的"能不能入场"逻辑完全分开)
  成熟层: 每个池子满48小时(覆盖我们统计出的"90%在23h内见顶+之后24h主要衰减"周期)后，
          做一次性深度采集: 完整历史K线(1h粒度) + GMGN钱包画像(默认前20名标签统计 +
          tag=dev 开发者钱包买卖详情)，写入 full_dataset.jsonl，然后标记"已完成"不再处理

每轮限制处理的"成熟"池子数量，避免单轮任务超时；发现层几乎零额外成本(复用列表接口
自带字段)。计划任务频率与策略C一致或稍低均可，不影响数据完整性(全量记录不依赖轮询密度)。
"""
import json
import time
import datetime as dt
from pathlib import Path

import requests

HERE = Path(__file__).parent
TRACKED_F = HERE / "tracked_pools.jsonl"
STATE_F = HERE / "research_state.json"
DATASET_F = HERE / "full_dataset.jsonl"
LOG_F = HERE / "research_monitor.log"

GT_BASE = "https://api.geckoterminal.com/api/v2"
GMGN_BASE = "https://gmgn.ai/vas/api/v1/token_traders/sol"
MATURITY_HOURS = 48          # 满这么久才做深度采集(覆盖见顶+主要衰减周期)
MAX_ENRICH_PER_CYCLE = 25    # 每轮最多深度采集这么多个,防止单轮超时
NEW_POOLS_PAGES = 15
TRENDING_PAGES = 3

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                  "Accept": "application/json;version=20230302"})
GS = requests.Session()
GS.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                   "Referer": "https://gmgn.ai/", "Accept": "application/json"})
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


def get(url, params=None, tries=3, session=None):
    sess = session or S
    for i in range(tries):
        try:
            r = sess.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(3 * (i + 1))
                continue
            return None
        except (requests.RequestException, json.JSONDecodeError):
            time.sleep(1.5 * (i + 1))
    return None


# ---------- 发现层 ----------

def discover(tracked_addrs):
    found = 0
    with TRACKED_F.open("a", encoding="utf-8") as f:
        for kind, url, pages in [("new", f"{GT_BASE}/networks/solana/new_pools", NEW_POOLS_PAGES),
                                 ("trend", f"{GT_BASE}/networks/solana/trending_pools", TRENDING_PAGES)]:
            for page in range(1, pages + 1):
                d = get(url, {"page": page, "include": "base_token"})
                rows = (d or {}).get("data") or []
                if not rows:
                    break
                mint_by_id = {inc["id"]: inc["attributes"]["address"]
                             for inc in (d.get("included") or []) if inc.get("type") == "token"}
                for row in rows:
                    addr = row["id"].split("_")[-1]
                    if addr in tracked_addrs:
                        continue
                    attrs = row["attributes"]
                    try:
                        created = dt.datetime.fromisoformat(
                            attrs["pool_created_at"].replace("Z", "+00:00")).timestamp()
                    except (KeyError, ValueError):
                        continue
                    base_id = (row.get("relationships", {}).get("base_token", {}).get("data", {}) or {}).get("id")
                    mint = mint_by_id.get(base_id)
                    rec = {"addr": addr, "mint": mint, "name": attrs.get("name", "?"),
                          "created": created, "first_seen": NOW, "source": kind, "enriched": False}
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    tracked_addrs.add(addr)
                    found += 1
                time.sleep(0.4)
    return found


# ---------- 成熟层: 量价特征 ----------

def fetch_ohlcv(addr, created_ts):
    span_days = (NOW - created_ts) / 86400
    after_ts = NOW - 7 * 86400 if span_days > 7 else int(created_ts) - 3600
    all_bars, before = [], NOW
    for _ in range(6):
        d = get(f"{GT_BASE}/networks/solana/pools/{addr}/ohlcv/hour",
               {"aggregate": 1, "limit": 1000, "before_timestamp": before})
        rows = (d or {}).get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        if not rows:
            break
        all_bars.extend(rows)
        oldest = min(r[0] for r in rows)
        if oldest <= after_ts:
            break
        before = oldest
        time.sleep(0.4)
    bars = sorted(set(tuple(r) for r in all_bars), key=lambda r: r[0])
    return [b for b in bars if b[0] >= after_ts]


def price_features(bars):
    if len(bars) < 4:
        return None
    base, open0 = bars[0][4], bars[0][1]
    if base <= 0:
        return None
    peak_idx = max(range(len(bars)), key=lambda i: bars[i][2])
    peak_bar = bars[peak_idx]
    peak_mult = peak_bar[2] / base
    t_peak_h = (peak_bar[0] - bars[0][0]) / 3600
    pre = bars[:peak_idx + 1]
    vol_ramp = None
    if len(pre) >= 4:
        half = len(pre) // 2
        v1 = sum(b[5] for b in pre[:half]) / max(half, 1)
        v2 = sum(b[5] for b in pre[half:]) / max(len(pre) - half, 1)
        vol_ramp = v2 / v1 if v1 > 0 else None
    post = [b for b in bars if peak_bar[0] < b[0] <= peak_bar[0] + 24 * 3600]
    post_24h_close = post[-1][4] if post else None
    dd_24h = (post_24h_close / peak_bar[2] - 1) if post_24h_close else None
    holds_base_24h = (post_24h_close >= base) if post_24h_close else None
    # 早期窗口特征(第1小时): 供未来建模用的"入场时就能看到"的信息
    first_h = [b for b in bars if b[0] <= bars[0][0] + 3600]
    vol_1h = sum(b[5] for b in first_h)
    chg_1h = (first_h[-1][4] / base - 1) if first_h else None
    return {"base": base, "open0": open0, "peak": peak_bar[2], "peak_mult": peak_mult,
           "t_peak_h": t_peak_h, "vol_ramp_pre_peak": vol_ramp,
           "dd_from_peak_24h": dd_24h, "holds_above_base_24h": holds_base_24h,
           "vol_first_1h": vol_1h, "chg_first_1h": chg_1h, "n_bars": len(bars)}


# ---------- 成熟层: 钱包画像 ----------

def wallet_features(mint):
    if not mint:
        return {}
    out = {}
    d = get(GMGN_BASE + f"/{mint}", session=GS)
    lst = (d or {}).get("data", {}).get("list") or []
    out["n_top_traders"] = len(lst)
    out["n_suspicious"] = sum(1 for w in lst if w.get("is_suspicious"))
    out["n_new_wallet"] = sum(1 for w in lst if w.get("is_new"))
    tags_all = [t for w in lst for t in (w.get("maker_token_tags") or [])]
    out["tag_counts"] = {t: tags_all.count(t) for t in set(tags_all)}
    time.sleep(0.3)
    dd = get(GMGN_BASE + f"/{mint}", {"tag": "dev"}, session=GS)
    dlst = (dd or {}).get("data", {}).get("list") or []
    dev = dlst[0] if dlst else None
    if dev:
        buy, sell = dev.get("current_buy_amount") or 0, dev.get("current_sell_amount") or 0
        out.update({"dev_buy_amount": buy, "dev_sell_amount": sell,
                    "dev_still_holding": (sell == 0 and buy > 0),
                    "dev_end_holding_at": dev.get("end_holding_at"),
                    "dev_tags": dev.get("maker_token_tags")})
    time.sleep(0.3)
    return out


def enrich_one(rec):
    bars = fetch_ohlcv(rec["addr"], rec["created"])
    pf = price_features(bars)
    if not pf:
        return None
    wf = wallet_features(rec.get("mint"))
    return {**rec, **pf, **wf, "enriched_at": NOW}


# ---------- 主流程 ----------

def load_tracked():
    if not TRACKED_F.exists():
        return [], set()
    recs, addrs = [], set()
    for line in TRACKED_F.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        recs.append(r)
        addrs.add(r["addr"])
    return recs, addrs


def main():
    recs, addrs = load_tracked()
    if STATE_F.exists():
        state = json.loads(STATE_F.read_text(encoding="utf-8"))
    else:
        state = {"enriched_addrs": []}
    enriched_set = set(state["enriched_addrs"])

    found = discover(addrs)
    log(f"发现层: 本轮新增 {found} 个, 累计追踪 {len(addrs)}")

    # 重新加载(discover已追加写入)
    recs, addrs = load_tracked()
    eligible = [r for r in recs if r["addr"] not in enriched_set
               and (NOW - r["created"]) / 3600 >= MATURITY_HOURS]
    log(f"成熟层: 待深度采集 {len(eligible)} 个(满{MATURITY_HOURS}h), 本轮处理上限 {MAX_ENRICH_PER_CYCLE}")

    n_done = 0
    with DATASET_F.open("a", encoding="utf-8") as f:
        for rec in eligible[:MAX_ENRICH_PER_CYCLE]:
            full = enrich_one(rec)
            enriched_set.add(rec["addr"])
            if full:
                f.write(json.dumps(full, ensure_ascii=False) + "\n")
                f.flush()
                n_done += 1
            time.sleep(0.3)

    state["enriched_addrs"] = list(enriched_set)
    STATE_F.write_text(json.dumps(state), encoding="utf-8")
    log(f"CYCLE OK: 新增追踪{found}, 深度采集完成{n_done}/{len(eligible[:MAX_ENRICH_PER_CYCLE])}, "
       f"累计追踪{len(addrs)}, 累计已入表{len(enriched_set)}")


if __name__ == "__main__":
    main()
