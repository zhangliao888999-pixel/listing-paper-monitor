# -*- coding: utf-8 -*-
"""2026-07-29新增: 用户提出的更直接的发现思路——不用在screener.py积累的1682+
存量池子里翻找,直接冲着GeckoTerminal的new_pools(最新池子)列表,按fdv_usd
(起点MCAP)从高到低排序,只看头部那几个。这正是今晚验证过的"起点MCAP"信号
(DINO/Look!死币~$2000 vs GDWR/TNOS活下来的$60万-$860万)的直接实战应用——
不用等池子攒够历史数据,新池子接口本身就带fdv_usd字段,拉一次就能排序。

用法: python mcap_scanner.py [取前N个,默认20]
"""
import io
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from check_coin import GT_BASE, S, get
from lifecycle_logger import load_lifecycle, save_lifecycle, deploy_full_stack

PAGES = 1  # 2026-07-29改: 用户指出的关键洞察——绝大多数新币MCAP基线就在2-3k
           # (用户自己发的DINO起点就是$2.1k),一旦有钱包真金白银开始往里砸,
           # MCAP会迅速甩开这个基线冲进排名前列,不需要深翻页去找,只盯第一页
           # (最新20个池子)按MCAP排序,谁冒头了自然会冲到前面来
MIN_LOCKED_PCT = 90


def fetch_new_pools():
    all_rows = []
    for page in range(1, PAGES + 1):
        d = get(S, f"{GT_BASE}/networks/solana/new_pools", {"page": page})
        rows = (d or {}).get("data", [])
        if not rows:
            break
        all_rows.extend(rows)
        time.sleep(0.3)
    return all_rows


def main():
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    rows = fetch_new_pools()
    print(f"共拉到{len(rows)}个最新池子")

    def get_fdv(row):
        try:
            return float(row["attributes"].get("fdv_usd") or 0)
        except (TypeError, ValueError):
            return 0

    # 去重(new_pools翻页窗口有重叠,同一个池子可能在好几页里都出现)
    seen_addr = set()
    dedup = []
    for row in rows:
        addr = row["attributes"].get("address")
        if addr in seen_addr:
            continue
        seen_addr.add(addr)
        dedup.append(row)
    dedup.sort(key=get_fdv, reverse=True)
    top = dedup[:top_n]

    print(f"\n按起点MCAP从高到低,头部{len(top)}个(已去重):")
    db = load_lifecycle()
    n_deployed = 0
    for row in top:
        a = row["attributes"]
        addr = a.get("address")
        name = a.get("name")
        fdv = get_fdv(row)

        # 2026-07-29修复: new_pools列表接口本身不带locked_liquidity_percentage
        # 字段(实测全是None),得对头部候选单独查一次/pools/{addr}详情才能拿到
        # 准确的锁仓比例,不能直接信列表接口里的值。
        detail = get(S, f"{GT_BASE}/networks/solana/pools/{addr}")
        detail_attrs = (detail or {}).get("data", {}).get("attributes", {})
        locked = detail_attrs.get("locked_liquidity_percentage")
        try:
            locked_f = float(locked) if locked is not None else 0
        except (TypeError, ValueError):
            locked_f = 0
        time.sleep(0.2)

        rel = row.get("relationships", {})
        base_token_id = rel.get("base_token", {}).get("data", {}).get("id", "")
        mint = base_token_id.split("_")[-1] if "_" in base_token_id else None

        flag = ""
        if locked_f >= MIN_LOCKED_PCT and mint and addr not in db:
            deployed = deploy_full_stack(addr, mint, db)
            if deployed:
                db[addr] = {"name": name, "mint": mint, "first_seen": time.time(),
                           "status": "alive", "history": [], "found_via": "mcap_scanner", "stack_deployed": True}
                n_deployed += 1
                flag = " *** 已部署监控+模拟交易 ***"
        print(f"  {name}  MCAP=${fdv:,.0f}  锁仓={locked}%  {addr}{flag}")

    save_lifecycle(db)
    print(f"\n本轮新部署: {n_deployed}个")


if __name__ == "__main__":
    main()
