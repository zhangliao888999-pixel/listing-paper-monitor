# -*- coding: utf-8 -*-
"""2026-07-31应急恢复: 头两笔实盘的卖出都因为"用信号时刻的旧报价直接广播"被
链上模拟拒绝(滑点超限/无路由),币卡在钱包里。这个脚本按当前真实状态重新卖:
  1. 从钱包真实token账户余额拿数量(不用买入时的报价数量——那个和实际到账有偏差)
  2. 现场重新拿Jupiter报价,滑点容忍放宽到10%(应急退出,能出来比价格重要)
  3. 失败自动换更高滑点重试(10%->30%)
用法(VPS上): python _recover_positions.py
"""
import base64
import json
import time
from pathlib import Path

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

HERE = Path(__file__).parent
SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"
RPC = "https://api.mainnet-beta.solana.com"

MINTS = {
    "Chiikawa": "HSh6yVvDTQhDT5DS76Qpn9MGcZbK66HefnpapJ8iPump",
    "Coupe": "2P8cKiGkqDGoLePKqUY15yTeTdMeEUhPHSMKWPMM3U8T",
}


def rpc(method, params):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    return r.json()


def main():
    key = (HERE / ".live_wallet_key").read_text(encoding="utf-8").strip()
    wallet = Keypair.from_base58_string(key)
    pub = str(wallet.pubkey())
    print(f"钱包: {pub[:8]}...")

    sol_before = rpc("getBalance", [pub])["result"]["value"]
    print(f"当前SOL余额: {sol_before/1e9:.4f}")

    for name, mint in MINTS.items():
        print(f"\n=== {name} ({mint[:10]}...) ===")
        resp = rpc("getTokenAccountsByOwner", [pub, {"mint": mint}, {"encoding": "jsonParsed"}])
        accounts = resp.get("result", {}).get("value", [])
        if not accounts:
            print("  钱包里没有这个币的token账户,无仓可回收")
            continue
        amount = int(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        ui = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmountString"]
        print(f"  真实余额: {ui} (raw={amount})")
        if amount == 0:
            print("  余额为0,无仓可回收")
            continue

        sold = False
        for slippage_bps in (1000, 3000):
            q = requests.get(JUP_QUOTE, params={
                "inputMint": mint, "outputMint": SOL_MINT,
                "amount": amount, "slippageBps": slippage_bps}, timeout=15)
            if q.status_code != 200:
                print(f"  拿不到报价(HTTP {q.status_code}): {q.text[:120]}")
                break
            quote = q.json()
            out_sol = int(quote.get("outAmount", 0)) / 1e9
            print(f"  报价: 可换回约{out_sol:.5f} SOL (滑点容忍{slippage_bps/100:.0f}%)")
            s = requests.post(JUP_SWAP, json={
                "quoteResponse": quote, "userPublicKey": pub, "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": "auto"}, timeout=15)
            if s.status_code != 200:
                print(f"  构造交易失败: {s.text[:120]}")
                continue
            raw = base64.b64decode(s.json()["swapTransaction"])
            tx = VersionedTransaction.from_bytes(raw)
            tx = VersionedTransaction(tx.message, [wallet])
            sig_b64 = base64.b64encode(bytes(tx)).decode()
            send = rpc("sendTransaction", [sig_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
            if "error" in send:
                print(f"  广播失败(滑点{slippage_bps/100:.0f}%): {str(send['error'])[:150]}")
                time.sleep(2)
                continue
            sig = send["result"]
            print(f"  已广播 tx={sig},等待确认...")
            for _ in range(15):
                st = rpc("getSignatureStatuses", [[sig]])
                v = (st.get("result", {}).get("value") or [None])[0]
                if v and v.get("confirmationStatus") in ("confirmed", "finalized"):
                    if v.get("err") is None:
                        print("  *** 卖出确认成功 ***")
                        sold = True
                    else:
                        print(f"  交易上链但执行失败: {v['err']}")
                    break
                time.sleep(2)
            if sold:
                break
        if not sold:
            print(f"  {name}未能卖出(两档滑点都失败,大概率没有可用路由/流动性已抽干)")

    time.sleep(2)
    sol_after = rpc("getBalance", [pub])["result"]["value"]
    print(f"\n回收后SOL余额: {sol_after/1e9:.4f} (变化: {(sol_after-sol_before)/1e9:+.5f} SOL)")


if __name__ == "__main__":
    main()
