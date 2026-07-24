# -*- coding: utf-8 -*-
"""策略C配套研究：抓取一批 Solana 新兴meme币的完整历史K线，做暴涨特征统计。

样本来源：
  1. GeckoTerminal trending_pools（多页），过滤到 30分钟~20天 库龄（排除SOL/RAY/BONK等多年老币）
  2. 我们自己策略C的观察名单（state_pumpfun.json，即使年轻也一并收录，样本量大点没坏处）

对每个池子拉取创建以来的完整1小时K线（>7天历史的池子只拉最近7天，避免过度请求），
计算：peak_mult(峰值倍数)、t_peak_h(见顶用时)、pre_peak_vol_pattern(见顶前成交量曲线)、
post_peak_decay(见顶后24h内的衰减/是否反弹)。

输出 research/pump_sample.jsonl，每行一个池子的特征记录。
"""
import json
import time
import datetime as dt
from pathlib import Path

import requests

HERE = Path(__file__).parent
OUT = HERE / "pump_sample.jsonl"
GT_BASE = "https://api.geckoterminal.com/api/v2"
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                  "Accept": "application/json;version=20230302"})
NOW = int(time.time())
MIN_AGE_H, MAX_AGE_DAYS = 0.5, 20


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


def collect_candidates():
    cands = {}
    for page in range(1, 11):
        d = get(f"{GT_BASE}/networks/solana/trending_pools", {"page": page})
        rows = (d or {}).get("data") or []
        if not rows:
            break
        for row in rows:
            addr = row["id"].split("_")[-1]
            attrs = row["attributes"]
            created = dt.datetime.fromisoformat(attrs["pool_created_at"].replace("Z", "+00:00"))
            age_h = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 3600
            if MIN_AGE_H <= age_h <= MAX_AGE_DAYS * 24:
                cands[addr] = {"addr": addr, "name": attrs.get("name", "?"),
                               "created": created.timestamp(), "source": "trending"}
        time.sleep(0.6)
    # 混入我们自己的观察名单(即使年轻)
    pf_state_f = HERE.parent / "state_pumpfun.json"
    if pf_state_f.exists():
        s = json.loads(pf_state_f.read_text(encoding="utf-8"))
        for addr, w in s.get("watch", {}).items():
            if addr not in cands:
                cands[addr] = {"addr": addr, "name": w.get("name", "?"),
                               "created": w.get("first_seen", NOW), "source": "our_watch"}
    return list(cands.values())


def fetch_ohlcv(addr, created_ts):
    """拉取1小时K线,从创建时间到现在,超过7天历史只取最近7天"""
    span_days = (NOW - created_ts) / 86400
    if span_days > 7:
        after_ts = NOW - 7 * 86400
    else:
        after_ts = int(created_ts) - 3600
    all_bars = []
    before = NOW
    for _ in range(6):  # 最多拉6批(每批最多1000根1h K线,足够覆盖数月,这里主要防止死循环)
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
        time.sleep(0.5)
    bars = sorted(set(tuple(r) for r in all_bars), key=lambda r: r[0])
    return [b for b in bars if b[0] >= after_ts]


def analyze(addr, name, created_ts, bars):
    if len(bars) < 4:
        return None
    base = bars[0][4]  # close of first bar as reference base
    if base <= 0:
        return None
    peak_idx = max(range(len(bars)), key=lambda i: bars[i][2])  # highest high
    peak_bar = bars[peak_idx]
    peak_mult = peak_bar[2] / base
    t_peak_h = (peak_bar[0] - bars[0][0]) / 3600
    # pre-peak volume trend: compare avg volume in first half vs second half of pre-peak window
    pre = bars[:peak_idx + 1]
    if len(pre) >= 4:
        half = len(pre) // 2
        vol_first_half = sum(b[5] for b in pre[:half]) / max(half, 1)
        vol_second_half = sum(b[5] for b in pre[half:]) / max(len(pre) - half, 1)
        vol_ramp = vol_second_half / vol_first_half if vol_first_half > 0 else None
    else:
        vol_ramp = None
    # post-peak: price 24h after peak vs peak, and vs base (did it hold gains or fully round-trip)
    post = [b for b in bars if b[0] > peak_bar[0] and b[0] <= peak_bar[0] + 24 * 3600]
    post_24h_close = post[-1][4] if post else None
    dd_from_peak_24h = (post_24h_close / peak_bar[2] - 1) if post_24h_close else None
    holds_above_base_24h = (post_24h_close >= base) if post_24h_close else None
    # flatline check: does it stay within +-15% of the post-peak-24h price for the *next* 24h too (i.e. truly dead)
    post2 = [b for b in bars if b[0] > peak_bar[0] + 24 * 3600 and b[0] <= peak_bar[0] + 48 * 3600]
    flatlined_48h = None
    if post_24h_close and post2:
        rng = (max(b[2] for b in post2) - min(b[3] for b in post2)) / post_24h_close
        flatlined_48h = rng < 0.3
    return {
        "addr": addr, "name": name, "created": created_ts,
        "n_bars": len(bars), "base": base, "peak": peak_bar[2], "peak_mult": peak_mult,
        "t_peak_h": t_peak_h, "vol_ramp_pre_peak": vol_ramp,
        "dd_from_peak_24h": dd_from_peak_24h, "holds_above_base_24h": holds_above_base_24h,
        "flatlined_48h_after_24h": flatlined_48h,
        "total_span_h": (bars[-1][0] - bars[0][0]) / 3600,
    }


def main():
    cands = collect_candidates()
    print(f"候选样本: {len(cands)}")
    done = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["addr"])
    n_ok = 0
    with OUT.open("a", encoding="utf-8") as f:
        for i, c in enumerate(cands):
            if c["addr"] in done:
                continue
            bars = fetch_ohlcv(c["addr"], c["created"])
            rec = analyze(c["addr"], c["name"], c["created"], bars)
            if rec:
                rec["source"] = c["source"]
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                n_ok += 1
            if (i + 1) % 20 == 0:
                print(f"  {i+1}/{len(cands)}, 有效记录 {n_ok}")
            time.sleep(0.4)
    print(f"DONE: {n_ok} 条有效记录")


if __name__ == "__main__":
    main()
