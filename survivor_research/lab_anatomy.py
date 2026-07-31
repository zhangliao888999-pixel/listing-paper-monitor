# -*- coding: utf-8 -*-
"""单笔交易完全解剖。2026-07-31建。

写这个是因为方法上栽了跟头: 对同一批账户我连续给了三个判断(MEV套利机器人
-> 第三方机器人服务 -> pump.fun官方程序),前两个都是**只看聚合统计就反推
行为**,没去看链上硬证据。

聚合统计会骗人,单笔交易不会。所以这个工具把一笔交易彻底摊开:
  - 完整账户表(含地址查找表),标出谁是签名人、谁可写
  - 每个账户的SOL收支,配平核对
  - 每个账户每种代币的收支
  - 完整指令树,包括内层指令(inner instructions)——真正的转账都藏在里面
  - Program return 的字节解码(pump.fun的GetFees把费率放在返回值里)

用法:
  python lab_anatomy.py <交易签名>
  python lab_anatomy.py --wallet <钱包>   自动挑该钱包一笔有资金变动的交易
"""
import base64
import struct
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import lab_forensics as fx   # noqa: E402

SOL = 1e9
KNOWN = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun联合曲线",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap AMM",
    "MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e": "pump.fun路由层",
    "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ": "pump.fun费用程序",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "SPL Token",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022",
    "11111111111111111111111111111111": "System",
    "ComputeBudget111111111111111111111111111111": "ComputeBudget",
    "So11111111111111111111111111111111111111112": "WSOL",
}


def tag(a):
    return f"  <{KNOWN[a]}>" if a in KNOWN else ""


def decode_return(data_b64):
    """pump.fun 的 GetFees 把费率放在 program return 里,按小端u64解。"""
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return None
    out = {"字节数": len(raw), "hex": raw.hex()}
    if len(raw) % 8 == 0:
        out["按u64小端解"] = list(struct.unpack("<" + "Q" * (len(raw) // 8), raw))
    return out


def walk(instrs, keys, depth=0, inner_map=None):
    for i, ins in enumerate(instrs):
        p = ins.get("program") or ins.get("programId") or "?"
        pad = "    " * depth
        parsed = ins.get("parsed")
        if isinstance(parsed, dict):
            t = parsed.get("type")
            info = parsed.get("info") or {}
            bits = []
            for k in ("source", "destination", "authority", "lamports", "amount", "mint"):
                if k in info:
                    v = info[k]
                    if k == "lamports":
                        v = f"{int(v)/SOL:.9f} SOL"
                    bits.append(f"{k}={str(v)[:44]}")
            print(f"  {pad}[{depth}.{i}] {p}{tag(str(p))}  {t}")
            for b in bits:
                print(f"  {pad}       {b}")
        else:
            print(f"  {pad}[{depth}.{i}] {p}{tag(str(p))}")
        if inner_map and depth == 0 and i in inner_map:
            walk(inner_map[i], keys, depth + 1)


def main():
    args = sys.argv[1:]
    sig = None
    if "--wallet" in args:
        w = args[args.index("--wallet") + 1]
        for s in reversed(fx.get_signatures(w, cap=300)):
            if s["err"] or not s.get("ts"):
                continue
            r = fx.parse_tx(s)
            if r and any(abs(v) > 1e-6 for v in r["sol_delta"].values()):
                sig = s["sig"]
                break
    else:
        sig = next((a for a in args if len(a) > 40), None)
    if not sig:
        print(__doc__); return

    res = fx.rpc("getTransaction", [sig, {"maxSupportedTransactionVersion": 0,
                                          "encoding": "jsonParsed"}])
    if not res:
        print("拉不到这笔交易"); return
    meta = res["meta"]
    msg = res["transaction"]["message"]
    keys = fx.account_keys(res)
    static = len(msg.get("accountKeys") or [])

    print("=" * 78)
    print(f"  {sig}")
    print("=" * 78)
    print(f"  slot {res.get('slot')}   区块时间 {res.get('blockTime')}")
    print(f"  账户 {len(keys)} 个 (静态 {static} + 查找表 {len(keys)-static})")
    print(f"  手续费 {meta.get('fee',0)/SOL:.9f} SOL   状态 {'成功' if not meta.get('err') else meta['err']}")
    print(f"  计算单元 {meta.get('computeUnitsConsumed')}")

    print("\n  --- SOL 收支 ---")
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    tot = 0.0
    rows = []
    for i, k in enumerate(keys):
        if i < len(pre) and i < len(post):
            d = (post[i] - pre[i]) / SOL
            if abs(d) > 1e-12:
                rows.append((d, i, k, post[i] / SOL))
                tot += d
    for d, i, k, bal in sorted(rows, key=lambda x: -abs(x[0])):
        role = "签名人" if i == 0 else ("查找表" if i >= static else "")
        print(f"    {d:>+16.9f} SOL   余额后 {bal:>12.4f}   {k}  {role}{tag(k)}")
    print(f"    {'-'*70}")
    print(f"    {tot:>+16.9f} SOL   合计 (应等于 -手续费 = {-meta.get('fee',0)/SOL:.9f})")

    print("\n  --- 代币收支 ---")
    prev = {b.get("accountIndex"): b for b in (meta.get("preTokenBalances") or [])}
    any_tok = False
    for b in (meta.get("postTokenBalances") or []):
        idx = b.get("accountIndex")
        now = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        was = float(((prev.get(idx) or {}).get("uiTokenAmount") or {}).get("uiAmount") or 0)
        if abs(now - was) < 1e-12:
            continue
        any_tok = True
        print(f"    {now-was:>+20,.6f}   mint {str(b.get('mint'))[:20]}..{tag(str(b.get('mint')))}")
        print(f"    {'':>20}   持有人 {b.get('owner')}")
    if not any_tok:
        print("    (无代币变动)")

    print("\n  --- 指令树 ---")
    inner_map = {}
    for g in (meta.get("innerInstructions") or []):
        inner_map[g.get("index")] = g.get("instructions") or []
    walk(msg.get("instructions") or [], keys, 0, inner_map)

    print("\n  --- 日志里的关键行 ---")
    for l in (meta.get("logMessages") or []):
        if any(x in l for x in ("Instruction:", "Program return:", "Program log:")):
            if "invoke" in l or "success" in l or "consumed" in l:
                continue
            print(f"    {l[:120]}")
            if "Program return:" in l:
                parts = l.split()
                dec = decode_return(parts[-1])
                if dec:
                    print(f"      解码 -> {dec}")


if __name__ == "__main__":
    main()
