# -*- coding: utf-8 -*-
"""核心研究：暴涨币 vs 平庸币，早期交易者画像(sniper/bundler/whale/可疑钱包占比)有没有区别。

数据源: GMGN 的 token_traders 内部接口(免费，浏览器请求头即可，本次会话实测确认可用)。
标签体系(实测确认有效): sniper(狙击手) / fresh_wallet(新钱包) / smart_degen(聪明钱)；
maker_token_tags 字段里还会出现 bundler(捆绑交易，开盘瞬间抢筹) / whale(巨鲸)。

方法：对 pump_sample.jsonl 里每个已算出peak_mult的池子，拉默认交易者列表(按profit降序,
前20个通常是最活跃/最大的做市/交易地址)，统计sniper/bundler/whale/suspicious标签占比，
和 buy_tx_count 里"开仓极早"(用created_at估计)的比例。输出 research/insider_signals.jsonl，
再和 peak_mult 关联分析。
"""
import json
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
SRC = HERE / "pump_sample.jsonl"
OUT = HERE / "insider_signals.jsonl"
GT_BASE = "https://gmgn.ai/vas/api/v1/token_traders/sol"
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://gmgn.ai/", "Accept": "application/json",
})


def get(params=None, tag=None, addr=None, tries=3):
    p = dict(params or {})
    if tag:
        p["tag"] = tag
    for i in range(tries):
        try:
            r = S.get(f"{GT_BASE}/{addr}", params=p, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(3 * (i + 1))
                continue
            return None
        except (requests.RequestException, json.JSONDecodeError):
            time.sleep(1.5 * (i + 1))
    return None


def analyze_traders(addr):
    out = {}
    default = get(addr=addr)
    lst = (default or {}).get("data", {}).get("list") or []
    out["n_top_traders"] = len(lst)
    out["n_suspicious"] = sum(1 for w in lst if w.get("is_suspicious"))
    out["n_new_wallet"] = sum(1 for w in lst if w.get("is_new"))
    tags_all = [t for w in lst for t in (w.get("maker_token_tags") or [])]
    out["tag_counts"] = {t: tags_all.count(t) for t in set(tags_all)}
    out["top20_total_profit"] = sum(w.get("profit") or 0 for w in lst)
    out["top20_avg_realized_pnl"] = (sum(w.get("realized_pnl") or 0 for w in lst) / len(lst)) if lst else None
    time.sleep(0.4)
    for tag in ["sniper", "fresh_wallet"]:
        d = get(tag=tag, addr=addr)
        tlst = (d or {}).get("data", {}).get("list") or []
        out[f"n_{tag}"] = len(tlst)
        out[f"{tag}_total_profit"] = sum(w.get("profit") or 0 for w in tlst)
        time.sleep(0.4)
    return out


def main():
    src = HERE / "pump_sample_with_mint.jsonl"
    if not src.exists():
        src = SRC
        print("警告: 用池子地址而非mint地址(GMGN可能查不到数据)，建议先跑 backfill_mint.py")
    samples = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
    samples = [s for s in samples if s.get("mint")]  # 没有mint地址的样本跳过,GMGN查不到
    print(f"待分析样本(有mint地址): {len(samples)}")
    done = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["addr"])
    n_ok = 0
    with OUT.open("a", encoding="utf-8") as f:
        for i, s in enumerate(samples):
            if s["addr"] in done:
                continue
            sig = analyze_traders(s["mint"])
            rec = {"addr": s["addr"], "mint": s["mint"], "name": s["name"], "peak_mult": s["peak_mult"],
                  "t_peak_h": s["t_peak_h"], "vol_ramp_pre_peak": s.get("vol_ramp_pre_peak"),
                  **sig}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            n_ok += 1
            if (i + 1) % 10 == 0:
                print(f"  {i+1}/{len(samples)}, 完成 {n_ok}")
    print(f"DONE: {n_ok}")


if __name__ == "__main__":
    main()
