# -*- coding: utf-8 -*-
"""验证用户假设: 开发者钱包"买入未卖出(DB无DS)"是不是有效的正向信号，
"开发者卖出(DS)"是不是经常标志着顶部。

用 GMGN tag=dev 精确拿到唯一的开发者/部署者钱包记录(实测确认: 返回且仅返回1条)，
核心字段: current_buy_amount(当前买入量) / current_sell_amount(当前卖出量) /
end_holding_at(清仓时间戳,可为空) / maker_token_tags。

和 pump_sample_with_mint.jsonl 里已经算好的 peak_mult / t_peak_h 关联分析。
输出 research/dev_signal.jsonl。
"""
import json
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
SRC = HERE / "pump_sample_with_mint.jsonl"
OUT = HERE / "dev_signal.jsonl"
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://gmgn.ai/", "Accept": "application/json",
})


def get_dev(mint, tries=3):
    for i in range(tries):
        try:
            r = S.get(f"https://gmgn.ai/vas/api/v1/token_traders/sol/{mint}",
                     params={"tag": "dev"}, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(3 * (i + 1))
                continue
            return None
        except (requests.RequestException, json.JSONDecodeError):
            time.sleep(1.5 * (i + 1))
    return None


def main():
    samples = [json.loads(l) for l in SRC.read_text(encoding="utf-8").splitlines() if l.strip()]
    samples = [s for s in samples if s.get("mint")]
    print(f"待查开发者钱包: {len(samples)}")
    done = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["mint"])
    n_ok = 0
    with OUT.open("a", encoding="utf-8") as f:
        for i, s in enumerate(samples):
            if s["mint"] in done:
                continue
            d = get_dev(s["mint"])
            lst = (d or {}).get("data", {}).get("list") or []
            dev = lst[0] if lst else None
            rec = {"mint": s["mint"], "name": s["name"], "peak_mult": s["peak_mult"],
                  "t_peak_h": s["t_peak_h"], "created": s["created"],
                  "has_dev": dev is not None}
            if dev:
                buy = dev.get("current_buy_amount") or 0
                sell = dev.get("current_sell_amount") or 0
                rec.update({
                    "dev_buy_amount": buy, "dev_sell_amount": sell,
                    "dev_sell_ratio": (sell / buy) if buy > 0 else None,
                    "dev_still_holding": (sell == 0 and buy > 0),
                    "dev_end_holding_at": dev.get("end_holding_at"),
                    "dev_tags": dev.get("maker_token_tags"),
                    "dev_profit": dev.get("profit"),
                    "dev_realized_pnl": dev.get("realized_pnl"),
                    "dev_last_active": dev.get("last_active_timestamp"),
                })
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            n_ok += 1
            if (i + 1) % 10 == 0:
                print(f"  {i+1}/{len(samples)}")
            time.sleep(0.4)
    print(f"DONE: {n_ok}")


if __name__ == "__main__":
    main()
