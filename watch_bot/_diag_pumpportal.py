# -*- coding: utf-8 -*-
"""诊断v2: 上一版全部400但看不出原因,两个新嫌疑:
1. 测试mint是编的(假mint本来就该400)——这版从GT new_pools实时拉一个真实的
   pump.fun新币mint来测。
2. PumpPortal官方示例用的是form编码(data=),不是JSON body——两种编码都测。
只构造交易,不签名不广播,零资金风险。"""
import json
from pathlib import Path

import requests
from solders.keypair import Keypair

HERE = Path(__file__).parent
URL = "https://pumpportal.fun/api/trade-local"

# 从GT实时拉最新的pump.fun池子,拿一个真实存在的mint
gt = requests.get("https://api.geckoterminal.com/api/v2/networks/solana/new_pools",
                  headers={"Accept": "application/json;version=20230302"}, timeout=15).json()
mint = None
for row in gt.get("data", []):
    tid = row.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
    cand = tid.split("_")[-1] if "_" in tid else None
    if cand and cand.endswith("pump"):
        mint = cand
        print(f"用GT最新真实pump币测试: {row['attributes'].get('name')}  mint={mint}")
        break
if not mint:
    print("GT最新池子里没找到pump币,退而求其次用第一个池子的mint")
    tid = gt["data"][0]["relationships"]["base_token"]["data"]["id"]
    mint = tid.split("_")[-1]

key = (HERE / ".live_wallet_key").read_text(encoding="utf-8").strip()
pub = str(Keypair.from_base58_string(key).pubkey())

payload = {"publicKey": pub, "action": "buy", "mint": mint, "amount": 0.025,
           "denominatedInSol": "true", "slippage": 10, "priorityFee": 0.0005, "pool": "pump"}

for label, kwargs in (("JSON编码", {"json": payload}), ("form编码(官方示例用法)", {"data": payload})):
    try:
        r = requests.post(URL, timeout=15, **kwargs)
        if r.status_code == 200:
            print(f"[{label}] HTTP 200 成功,返回{len(r.content)}字节交易数据  ^^^ 用这种编码")
        else:
            print(f"[{label}] HTTP {r.status_code}  响应: {r.text[:200]}")
    except requests.RequestException as e:
        print(f"[{label}] 请求异常: {e}")
