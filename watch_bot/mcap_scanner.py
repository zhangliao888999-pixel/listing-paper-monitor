# -*- coding: utf-8 -*-
"""2026-07-29新增: 用户提出的更直接的发现思路——不用在screener.py积累的1682+
存量池子里翻找,直接冲着GeckoTerminal的new_pools(最新池子)列表,按fdv_usd
(起点MCAP)从高到低排序,只看头部那几个。这正是今晚验证过的"起点MCAP"信号
(DINO/Look!死币~$2000 vs GDWR/TNOS活下来的$60万-$860万)的直接实战应用——
不用等池子攒够历史数据,新池子接口本身就带fdv_usd字段,拉一次就能排序。

2026-07-29再改: 用户提出用多线程并发查详情提速("一页50个币,开10个线程5次就
跑完")。10个线程对家庭网络毫无压力,真正的瓶颈是GeckoTerminal/GMGN这两个
接口自己的限流,不是本地网速——盲目上高并发,超过限流阈值触发的重试退避反而
可能更慢,甚至被临时封。所以从保守的并发数开始(默认4),用线程池实现,
可以通过命令行参数调整,自己试出实际能扛住的并发上限。

用法: python mcap_scanner.py [取前N个,默认10] [并发数,默认4]
"""
import io
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from check_coin import GT_BASE, S, get
from lifecycle_logger import load_lifecycle, save_lifecycle, deploy_full_stack

PAGES = 3  # new_pools一页约20个,拉3页凑够约50-60个,对应用户说的"一页50个币"
MIN_LOCKED_PCT = 90
# 2026-07-29再修: REDO/SOL一案发现——locked_liquidity_percentage首次扫描常是None
# (GT还没索引到锁仓/销毁交易),下一轮变成100%就被部署监控了,但完全没用起点MCAP
# 这个已经验证过的信号过滤(REDO起点MCAP只有$12,511,DINO/Look!等死币也是$2000量级,
# 而TNOS/GDWR这类真正被狗庄看上、有资金拉的币起点MCAP是$60万-$860万)。结果REDO
# 白白占用一个监控位(MAX_CONCURRENT_DEPLOYED=8),2分钟就死了还是全员机器人对倒。
# 补上MCAP门槛,跟lifecycle_logger.py的matches_origin_mcap_signature用同一个阈值。
MIN_MCAP_USD = 50000


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


def fetch_detail(addr):
    """单个池子的详情查询,给线程池并发调用用。返回(addr, locked_pct_or_None,
    是否429限流失败)——限流失败要单独标记出来,方便统计"这个并发数到底扛不扛得住"。"""
    d = get(S, f"{GT_BASE}/networks/solana/pools/{addr}")
    if d is None:
        return addr, None, True  # get()内部已经做了3次重试退避,还是失败,大概率是被限流了
    attrs = d.get("data", {}).get("attributes", {})
    locked = attrs.get("locked_liquidity_percentage")
    return addr, locked, False


def main():
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 4
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
    by_addr = {row["attributes"]["address"]: row for row in top}

    print(f"\n按起点MCAP从高到低,头部{len(top)}个(已去重),并发数={workers}查详情:")
    t0 = time.time()
    locked_map = {}
    n_rate_limited = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_detail, addr): addr for addr in by_addr}
        for fut in as_completed(futures):
            addr, locked, was_limited = fut.result()
            locked_map[addr] = locked
            if was_limited:
                n_rate_limited += 1
    elapsed = time.time() - t0
    print(f"并发查询耗时{elapsed:.1f}秒(如果限流失败数>0,说明这个并发数偏高,建议调低)")
    if n_rate_limited:
        print(f"*** 有{n_rate_limited}个查询疑似被限流失败,建议降低并发数重试 ***")

    db = load_lifecycle()
    n_deployed = 0
    for addr, row in by_addr.items():
        a = row["attributes"]
        name = a.get("name")
        fdv = get_fdv(row)
        locked = locked_map.get(addr)
        try:
            locked_f = float(locked) if locked is not None else 0
        except (TypeError, ValueError):
            locked_f = 0

        rel = row.get("relationships", {})
        base_token_id = rel.get("base_token", {}).get("data", {}).get("id", "")
        mint = base_token_id.split("_")[-1] if "_" in base_token_id else None

        flag = ""
        if locked_f >= MIN_LOCKED_PCT and fdv >= MIN_MCAP_USD and mint and addr not in db:
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
