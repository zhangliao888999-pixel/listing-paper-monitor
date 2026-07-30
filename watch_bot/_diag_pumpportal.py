# -*- coding: utf-8 -*-
"""诊断: PumpPortal买入全部HTTP 400,打印它返回的完整错误正文和我们发的参数,
看是哪个字段不合它的口味(不真实下单——只到构造交易这一步就停,不签名不广播)。"""
import json
import sys
from pathlib import Path

import requests
from solders.keypair import Keypair

HERE = Path(__file__).parent
URL = "https://pumpportal.fun/api/trade-local"

mint = sys.argv[1] if len(sys.argv) > 1 else "So11111111111111111111111111111111111111112"
key = (HERE / ".live_wallet_key").read_text(encoding="utf-8").strip()
pub = str(Keypair.from_base58_string(key).pubkey())
print(f"钱包: {pub[:10]}...  测试mint: {mint}")

variants = [
    ("当前代码用的参数", {"publicKey": pub, "action": "buy", "mint": mint, "amount": 0.025,
                          "denominatedInSol": "true", "slippage": 10.0, "priorityFee": 0.0005, "pool": "pump"}),
    ("slippage/priorityFee改成整数", {"publicKey": pub, "action": "buy", "mint": mint, "amount": 0.025,
                          "denominatedInSol": "true", "slippage": 10, "priorityFee": 0.0005, "pool": "pump"}),
    ("pool=auto", {"publicKey": pub, "action": "buy", "mint": mint, "amount": 0.025,
                          "denominatedInSol": "true", "slippage": 10, "priorityFee": 0.0005, "pool": "auto"}),
    ("不带pool字段", {"publicKey": pub, "action": "buy", "mint": mint, "amount": 0.025,
                          "denominatedInSol": "true", "slippage": 10, "priorityFee": 0.0005}),
]

for label, payload in variants:
    try:
        r = requests.post(URL, json=payload, timeout=15)
        body = r.text[:300] if r.status_code != 200 else f"(成功,返回{len(r.content)}字节交易数据)"
        print(f"\n[{label}] HTTP {r.status_code}")
        print(f"  请求: {json.dumps(payload, ensure_ascii=False)}")
        print(f"  响应: {body}")
        if r.status_code == 200:
            print("  ^^^ 这个参数组合可用")
            break
    except requests.RequestException as e:
        print(f"\n[{label}] 请求异常: {e}")
