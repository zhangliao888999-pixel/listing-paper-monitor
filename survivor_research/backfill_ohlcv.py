# -*- coding: utf-8 -*-
"""2026-07-31新建: 补齐K线到币诞生,修正回测的时间窗口偏差。

用户问"这批数据是活了多久的币",查下来发现一个会让结论失效的缺陷:
GT单次最多返回1000根分钟K线,而57%的样本(239个)都被这个上限截断了。
拿到的是**最近**1000分钟,不是完整生命周期——也就是说对这些币,我测的是
它们**后期**(很多已过了活跃期、进入低波动僵尸状态)的行情,而访谈里的
手法针对的是币**早期**"已经涨起来、还在活跃"的阶段。用错误的窗口测,
自然测不出信号,所以之前"0/192全负"的结论必须降低置信度、重新验证。

这个脚本用before_timestamp参数一直往前翻,把每个币补齐到诞生(或翻不动为止),
输出到ohlcv3/。之后回测就能用"币诞生后头几小时"这个正确的窗口。

用法: python backfill_ohlcv.py [最多往前翻几页,默认6]
"""
import json
import sys
import datetime as dt
from pathlib import Path

import cg_client as cg

HERE = Path(__file__).parent
SRC_DIR = HERE / "ohlcv2"
OUT_DIR = HERE / "ohlcv3"
OUT_DIR.mkdir(exist_ok=True)

MAX_PAGES = int(sys.argv[1]) if len(sys.argv) > 1 else 6


def backfill(addr, bars):
    """从已有K线往前翻,补到拿不到更早数据为止。返回合并后的完整K线。"""
    bars = sorted(bars, key=lambda x: x[0])
    seen_ts = {b[0] for b in bars}
    pages = 0
    while pages < MAX_PAGES:
        earliest = bars[0][0]
        d = cg.get(f"networks/solana/pools/{addr}/ohlcv/minute",
                   {"aggregate": 1, "limit": 1000, "before_timestamp": earliest})
        older = (d or {}).get("data", {}).get("attributes", {}).get("ohlcv_list", []) or []
        older = [b for b in older if b[0] not in seen_ts]
        if not older:
            break                      # 翻到头了,这就是币的起点
        seen_ts.update(b[0] for b in older)
        bars = sorted(older + bars, key=lambda x: x[0])
        pages += 1
        if len(older) < 900:
            break                      # 返回不满一页,说明已经到起点
    return bars


def main():
    files = [f for f in SRC_DIR.glob("*.json") if f.stat().st_size > 200]
    print(f"待补齐样本: {len(files)}个,每个最多往前翻{MAX_PAGES}页", flush=True)

    n_done = n_extended = 0
    total_added = 0
    for i, f in enumerate(files, 1):
        out = OUT_DIR / f.name
        if out.exists():
            n_done += 1
            continue
        try:
            bars = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if len(bars) < 999:
            # 没被截断的直接复制,不浪费额度
            out.write_text(json.dumps(sorted(bars, key=lambda x: x[0])), encoding="utf-8")
            n_done += 1
            continue
        before = len(bars)
        full = backfill(f.stem, bars)
        out.write_text(json.dumps(full), encoding="utf-8")
        added = len(full) - before
        total_added += added
        n_done += 1
        if added > 0:
            n_extended += 1
        if i % 25 == 0:
            print(f"  {i}/{len(files)}  已补齐{n_extended}个,累计新增{total_added}根K线", flush=True)

    print(f"\n完成: {n_done}个样本 -> ohlcv3/", flush=True)
    print(f"其中{n_extended}个被成功往前补齐,累计新增{total_added}根K线", flush=True)

    # 补齐后的跨度统计
    spans = []
    for f in OUT_DIR.glob("*.json"):
        try:
            b = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if len(b) >= 2:
            spans.append((b[-1][0] - b[0][0]) / 3600)
    spans.sort()
    if spans:
        print(f"\n补齐后的生命周期跨度(小时):")
        for p, l in ((10, "p10"), (25, "p25"), (50, "中位"), (75, "p75"), (90, "p90")):
            print(f"  {l}: {spans[int(len(spans)*p/100)]:.1f}小时")


if __name__ == "__main__":
    main()
