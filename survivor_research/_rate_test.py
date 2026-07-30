# -*- coding: utf-8 -*-
"""测当前机器IP对GeckoTerminal的可用请求速率,用来决定采集器放哪台机器跑。"""
import time
import requests

H = {"Accept": "application/json;version=20230302", "User-Agent": "Mozilla/5.0"}
URL = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"

for gap in (2.2, 4.0):
    ok = err = 0
    for i in range(5):
        try:
            r = requests.get(URL, params={"duration": "6h", "page": i % 3 + 1}, headers=H, timeout=15)
            if r.status_code == 200:
                ok += 1
            else:
                err += 1
        except requests.RequestException:
            err += 1
        time.sleep(gap)
    print(f"间隔{gap}s: 成功{ok}/5 失败{err}")
