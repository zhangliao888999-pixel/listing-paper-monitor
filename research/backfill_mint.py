# -*- coding: utf-8 -*-
"""给 pump_sample.jsonl 里的池子地址补上真正的代币铸造地址(mint)，
因为GMGN的token_traders接口要的是mint地址,不是流动性池地址(两者不同)。
输出 pump_sample_with_mint.jsonl。
"""
import json
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
SRC = HERE / "pump_sample.jsonl"
OUT = HERE / "pump_sample_with_mint.jsonl"
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                  "Accept": "application/json;version=20230302"})


def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            return None
        except (requests.RequestException, json.JSONDecodeError):
            time.sleep(1.5 * (i + 1))
    return None


def main():
    rows = [json.loads(l) for l in SRC.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"待补mint地址: {len(rows)}")
    n_ok = 0
    with OUT.open("w", encoding="utf-8") as f:
        for i, r in enumerate(rows):
            d = get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{r['addr']}",
                   {"include": "base_token"})
            mint = None
            if d:
                for inc in d.get("included", []):
                    if inc.get("type") == "token":
                        mint = inc["attributes"]["address"]
                        break
            r["mint"] = mint
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            if mint:
                n_ok += 1
            if (i + 1) % 20 == 0:
                print(f"  {i+1}/{len(rows)}, 成功 {n_ok}")
            time.sleep(0.3)
    print(f"DONE: {n_ok}/{len(rows)} 补到mint地址")


if __name__ == "__main__":
    main()
