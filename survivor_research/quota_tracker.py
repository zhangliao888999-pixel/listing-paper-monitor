# -*- coding: utf-8 -*-
"""2026-07-31新建: CoinGecko API额度追踪。

用户明确说"该花就得花,不够用提前告诉我"。所以不是省着不敢用,而是把额度
当可管理资源跟踪——每次采集记录消耗,接近上限时主动预警,让用户有时间决定
升级,而不是跑到一半突然断掉。

重要: 升级路径不是$29的Basic——Basic不含onchain DEX端点(我们要的Solana
池子K线正在这个范围),必须直接上Analyst $129。这一点要在预警里说清楚,
免得用户买错档位。

用法:
  python quota_tracker.py           # 看当前用量
  在脚本里: from quota_tracker import record; record(n_requests, "harvest")
"""
import json
import datetime as dt
from pathlib import Path

HERE = Path(__file__).parent
LOG_F = HERE / "quota_usage.jsonl"

MONTHLY_LIMIT = 10000      # Demo版
WARN_AT_PCT = 70           # 用到70%就开始提醒
CRITICAL_AT_PCT = 90


def record(n_requests, tag=""):
    """记一笔消耗。"""
    rec = {"ts": int(dt.datetime.now().timestamp()),
           "date": dt.datetime.now().strftime("%Y-%m-%d"),
           "n": n_requests, "tag": tag}
    with LOG_F.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def month_usage(year_month=None):
    """本月(或指定月份)累计消耗。CoinGecko每月1号重置。"""
    if year_month is None:
        year_month = dt.datetime.now().strftime("%Y-%m")
    total = 0
    by_tag = {}
    if LOG_F.exists():
        for line in LOG_F.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("date", "").startswith(year_month):
                total += r.get("n", 0)
                by_tag[r.get("tag", "?")] = by_tag.get(r.get("tag", "?"), 0) + r.get("n", 0)
    return total, by_tag


def check_and_warn():
    """返回(用量, 百分比, 预警文本或None)。"""
    used, by_tag = month_usage()
    pct = 100 * used / MONTHLY_LIMIT
    warn = None
    if pct >= CRITICAL_AT_PCT:
        warn = (f"*** API额度告急: 已用{used}/{MONTHLY_LIMIT} ({pct:.0f}%) ***\n"
                f"    需要升级才能继续大规模采集。注意: 不能买$29的Basic\n"
                f"    (不含onchain DEX端点),要直接上 Analyst $129/月(50万次)。")
    elif pct >= WARN_AT_PCT:
        warn = (f"API额度提醒: 已用{used}/{MONTHLY_LIMIT} ({pct:.0f}%),"
                f"剩余{MONTHLY_LIMIT-used}次。如需继续扩大样本,升级要选Analyst $129"
                f"(Basic $29不含onchain端点,买了用不了)。")
    return used, pct, warn


if __name__ == "__main__":
    used, by_tag = month_usage()
    pct = 100 * used / MONTHLY_LIMIT
    nxt = (dt.date.today().replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    print(f"=== CoinGecko Demo额度 ({dt.datetime.now().strftime('%Y-%m')}) ===")
    print(f"已用: {used} / {MONTHLY_LIMIT}  ({pct:.1f}%)")
    print(f"剩余: {MONTHLY_LIMIT - used} 次")
    print(f"重置日: {nxt}")
    if by_tag:
        print("\n按用途:")
        for t, n in sorted(by_tag.items(), key=lambda x: -x[1]):
            print(f"  {t}: {n}次")
    _, _, warn = check_and_warn()
    if warn:
        print(f"\n{warn}")
