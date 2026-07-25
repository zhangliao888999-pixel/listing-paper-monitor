# -*- coding: utf-8 -*-
"""全量新币研究监控：不做交易决策，只负责持续、全面地记录每一个新上线代币的
量价关系 + 钱包画像，产出一张不断增长的结构化数据表，供后续归纳交易规则用。

架构(三层):
  发现层: 每轮扫 new_pools(多页) + trending_pools，全量记录进 tracked_pools.jsonl
          (不设流动性/年龄门槛，来者不拒，和策略C的"能不能入场"逻辑完全分开)
  年轻层: 池子年龄在0~YOUNG_HOURS小时内时，每轮都去抓一次逐笔交易(GeckoTerminal /trades)，
          追加进 young_trades.jsonl(允许重复,聚合时按tx_hash去重)。之所以要反复抓而不是
          抓一次: 实测/trades接口的page参数不是真正的历史翻页,只返回"现在往前一段"的
          滚动窗口，唯一能拼出完整"上市头几小时"逐笔记录的办法就是趁它还年轻时反复采样、
          靠多次快照的并集覆盖整个窗口。
  成熟层: 每个池子满48小时(覆盖我们统计出的"90%在23h内见顶+之后24h主要衰减"周期)后，
          做一次性深度采集: 完整历史K线(1h粒度) + GMGN钱包画像(默认前20名标签统计 +
          tag=dev 开发者钱包买卖详情) + 聚合年轻层攒的逐笔交易(首1h/3h/6h成交笔数、
          独立钱包数、买卖比例、疑似刷量的钱包重复次数)，写入 full_dataset.jsonl

每轮限制处理的"成熟"和"年轻层轮询"数量，避免单轮任务超时。
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
EARLY_TRADES_F = HERE / "early_trades.jsonl"        # 旧版一次性快照,保留兼容
YOUNG_TRADES_F = HERE / "young_trades.jsonl"        # 新版: 年轻期反复采样累积的逐笔交易
LOG_F = HERE / "research_monitor.log"

GT_BASE = "https://api.geckoterminal.com/api/v2"
GMGN_BASE = "https://gmgn.ai/vas/api/v1/token_traders/sol"
MATURITY_HOURS = 48          # 满这么久才做深度采集(覆盖见顶+主要衰减周期)
MAX_ENRICH_PER_CYCLE = 25    # 每轮最多深度采集这么多个,防止单轮超时
YOUNG_HOURS = 6              # 池子年龄在这个窗口内,每轮都反复抓逐笔交易
MAX_YOUNG_POLLS_PER_CYCLE = 40  # 每轮最多轮询这么多个"年轻"池子的逐笔交易,防止单轮超时
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


# ---------- 年轻层: 反复采样逐笔交易，拼出上市头几小时的完整记录 ----------

def poll_young_pools(recs, last_polled):
    """对年龄在 0~YOUNG_HOURS 内的池子，本轮抓一次逐笔交易(允许和之前重复,聚合时按
    tx_hash去重)。按上次轮询时间从旧到新排序,保证6小时窗口内每个池子都被多次覆盖到，
    而不是每轮都只顾着抓最新发现的那几个。"""
    young = [r for r in recs if 0 <= (NOW - r["created"]) / 3600 <= YOUNG_HOURS]
    young.sort(key=lambda r: last_polled.get(r["addr"], 0))
    batch = young[:MAX_YOUNG_POLLS_PER_CYCLE]
    n_trades = 0
    with YOUNG_TRADES_F.open("a", encoding="utf-8") as f:
        for r in batch:
            d = get(f"{GT_BASE}/networks/solana/pools/{r['addr']}/trades")
            rows = (d or {}).get("data") or []
            for row in rows:
                a = row["attributes"]
                try:
                    ts = dt.datetime.fromisoformat(a["block_timestamp"].replace("Z", "+00:00")).timestamp()
                except (KeyError, ValueError):
                    continue
                if ts < r["created"]:
                    continue
                f.write(json.dumps({"addr": r["addr"], "tx_hash": a.get("tx_hash"), "ts": ts,
                                    "wallet": a.get("tx_from_address"), "kind": a.get("kind"),
                                    "usd": float(a.get("volume_in_usd") or 0)}, ensure_ascii=False) + "\n")
                n_trades += 1
            last_polled[r["addr"]] = NOW
            time.sleep(0.3)
    return len(batch), n_trades, len(young)


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


def load_young_trades_by_addr():
    """加载年轻层反复采样积累的逐笔交易,按addr分组、按tx_hash去重(同一笔交易可能被
    多轮轮询重复抓到)。"""
    out = {}
    if not YOUNG_TRADES_F.exists():
        return out
    seen = {}
    for line in YOUNG_TRADES_F.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        key = (r["addr"], r.get("tx_hash"))
        if key in seen:
            continue
        seen[key] = True
        out.setdefault(r["addr"], []).append(r)
    return out


def young_trade_features(trades, created_ts):
    """把某个池子攒到的逐笔交易(可能覆盖0~6小时不等,取决于实际轮询到的次数)
    汇总成买卖信号特征: 各时间窗口成交笔数/独立钱包数/买卖比例/疑似刷量信号。"""
    if not trades:
        return {}
    trades = sorted(trades, key=lambda t: t["ts"])
    from collections import Counter
    out = {"n_young_trades_total": len(trades),
          "young_trades_coverage_h": (trades[-1]["ts"] - created_ts) / 3600}
    for window_min, tag in [(1, "1min"), (5, "5min"), (60, "1h"), (180, "3h"), (360, "6h")]:
        w = [t for t in trades if t["ts"] <= created_ts + window_min * 60]
        if not w:
            continue
        wallets = {t["wallet"] for t in w}
        wc = Counter(t["wallet"] for t in w)
        buys = sum(1 for t in w if t["kind"] == "buy")
        out[f"n_trades_{tag}"] = len(w)
        out[f"n_wallets_{tag}"] = len(wallets)
        out[f"buy_ratio_{tag}"] = buys / len(w)
        out[f"max_wallet_repeat_{tag}"] = max(wc.values())
        out[f"vol_usd_{tag}"] = sum(t["usd"] for t in w)
    return out


def enrich_one(rec, young_trades_by_addr):
    bars = fetch_ohlcv(rec["addr"], rec["created"])
    pf = price_features(bars)
    if not pf:
        return None
    wf = wallet_features(rec.get("mint"))
    tf = young_trade_features(young_trades_by_addr.get(rec["addr"], []), rec["created"])
    return {**rec, **pf, **wf, **tf, "enriched_at": NOW}


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
        state = {"enriched_addrs": [], "last_polled": {}}
    enriched_set = set(state["enriched_addrs"])
    last_polled = state.get("last_polled", {})

    found = discover(addrs)
    log(f"发现层: 本轮新增 {found} 个, 累计追踪 {len(addrs)}")

    # 重新加载(discover已追加写入)
    recs, addrs = load_tracked()

    n_polled, n_new_trades, n_young_total = poll_young_pools(recs, last_polled)
    log(f"年轻层: 当前{n_young_total}个池子在0~{YOUNG_HOURS}h窗口内, 本轮轮询{n_polled}个, 抓到{n_new_trades}笔交易")

    young_trades_by_addr = load_young_trades_by_addr()
    eligible = [r for r in recs if r["addr"] not in enriched_set
               and (NOW - r["created"]) / 3600 >= MATURITY_HOURS]
    log(f"成熟层: 待深度采集 {len(eligible)} 个(满{MATURITY_HOURS}h), 本轮处理上限 {MAX_ENRICH_PER_CYCLE}")

    n_done = 0
    with DATASET_F.open("a", encoding="utf-8") as f:
        for rec in eligible[:MAX_ENRICH_PER_CYCLE]:
            full = enrich_one(rec, young_trades_by_addr)
            enriched_set.add(rec["addr"])
            if full:
                f.write(json.dumps(full, ensure_ascii=False) + "\n")
                f.flush()
                n_done += 1
            time.sleep(0.3)

    state["enriched_addrs"] = list(enriched_set)
    # last_polled只保留仍在年轻窗口内的,避免无限增长
    state["last_polled"] = {a: t for a, t in last_polled.items() if NOW - t < YOUNG_HOURS * 3600}
    STATE_F.write_text(json.dumps(state), encoding="utf-8")
    log(f"CYCLE OK: 新增追踪{found}, 深度采集完成{n_done}/{len(eligible[:MAX_ENRICH_PER_CYCLE])}, "
       f"累计追踪{len(addrs)}, 累计已入表{len(enriched_set)}")


if __name__ == "__main__":
    main()
