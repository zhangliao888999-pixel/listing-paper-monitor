# -*- coding: utf-8 -*-
"""本地直接查看候选币列表(不用等GitHub CDN缓存刷新)。
合并本地+云端两份候选文件(同地址取更新的那份)，按1h涨跌排序打印到终端。
用法: python view_candidates.py
"""
import json
from pathlib import Path

HERE = Path(__file__).parent


def load(name):
    f = HERE / name
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def main():
    cloud = load("screener_candidates.json")
    local = load("screener_candidates_local.json")
    if not cloud and not local:
        print("尚无候选数据(screener还没跑过)")
        return

    merged = {}
    for src in (cloud, local):
        if not src:
            continue
        ts = src.get("updated_at", 0)
        for c in src.get("candidates", []):
            existing = merged.get(c["addr"])
            if not existing or ts > existing["__ts"]:
                c = dict(c)
                c["__ts"] = ts
                merged[c["addr"]] = c
    cands = sorted(merged.values(), key=lambda c: c["chg_1h"], reverse=True)

    if cloud:
        print(f"云端更新: {cloud['updated_at_str']}")
    if local:
        print(f"本地更新: {local['updated_at_str']}")
    print(f"合并候选数: {len(cands)}\n")

    print(f"{'币种':<18}{'年龄':<8}{'流动性':<14}{'15m涨跌':>10}{'1h涨跌':>10}  买/卖")
    for c in cands:
        age = f"{c['age_min']:.0f}分" if c["age_min"] < 60 else f"{c['age_min']/60:.1f}时"
        print(f"{c['name']:<18}{age:<8}${c['liq']:<13,.0f}{c['chg_15m']:>+9.1f}%{c['chg_1h']:>+9.1f}%  {c['buys_15m']}/{c['sells_15m']}   {c['dex_url']}")


if __name__ == "__main__":
    main()
