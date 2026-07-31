# -*- coding: utf-8 -*-
"""2026-07-31新建: 样本清洗——剔除"钓鱼币",只留有真实买家参与的币。

用户指出了我方法论上的根本问题: 我一直在调策略参数,却没先清洗样本。
用一堆钓鱼币做回测,再好的参数也是在噪音里找规律,三轮回测的"最好组合"
邻居全是负的(孤立尖峰)很可能就是这个原因。

钓鱼币的机制(用户原话): 狗庄拿几分钱不断撒饵把价格一点点抬高,发币时就
买进去等着,发现上钩的鱼够多了,一秒砸下来收网。

用户给的三个样本实测,特征极其鲜明:
  USOS: 买11788笔 / 卖53笔   = 222:1,卖家只有44人
  TNOS: 买19886笔 / 卖312笔  = 64:1, 卖家只有33人
正常有真实双向交易的币,买卖笔数应该在同一量级。买卖比几十上百倍意味着
"钱进得去出不来"——鱼被困在里面,等着最后被一次性收割。

过滤规则(全部基于可观测的链上统计,不靠猜):
  1. 买卖笔数比 <= MAX_BUY_SELL_RATIO   —— 剔除只进不出的钓鱼盘
  2. 独立卖家数 >= MIN_SELLERS          —— 有人能卖出去,才说明有真实流动性
  3. 独立买家数 >= MIN_BUYERS           —— 排除自买自卖的对倒盘
  4. 卖家/买家比 >= MIN_SELLER_BUYER    —— 双向参与度
  5. 存活分钟数 >= MIN_LIFE_MIN         —— 剔除几分钟就死的
  6. 有成交量的K线占比 >= MIN_ACTIVE    —— 剔除大段时间没人交易的僵尸币

用法: python filter_real_coins.py
输出: real_coins.json (通过清洗的池子清单+统计) 和 ohlcv_clean/ 软链接目录
"""
import json
import shutil
import sys
from pathlib import Path

import cg_client as cg

HERE = Path(__file__).parent
SRC_DIR = HERE / "ohlcv3"
OUT_DIR = HERE / "ohlcv_clean"
REPORT_F = HERE / "real_coins.json"

# 阈值: 先用宽松的,先看能筛出多少,再根据分布收紧
MAX_BUY_SELL_RATIO = 4.0    # 买卖笔数比上限(钓鱼币是几十上百)
MIN_SELLERS = 50            # 至少这么多独立卖家真的卖出去过
MIN_BUYERS = 50             # 至少这么多独立买家
MIN_SELLER_BUYER = 0.25     # 卖家数/买家数,双向参与度
MIN_LIFE_MIN = 120          # 至少活2小时
MIN_ACTIVE_FRAC = 0.5       # 至少一半的分钟有成交


def pool_stats(addr):
    """拉池子的24h买卖笔数/买卖家人数。这是识别钓鱼币的核心数据。"""
    d = cg.get(f"networks/solana/pools/{addr}")
    if not d:
        return None
    a = d.get("data", {}).get("attributes", {})
    tx = (a.get("transactions") or {}).get("h24") or {}
    try:
        return {
            "name": a.get("name"),
            "buys": int(tx.get("buys") or 0),
            "sells": int(tx.get("sells") or 0),
            "buyers": int(tx.get("buyers") or 0),
            "sellers": int(tx.get("sellers") or 0),
            "liq": float(a.get("reserve_in_usd") or 0),
            "vol24": float((a.get("volume_usd") or {}).get("h24") or 0),
        }
    except (TypeError, ValueError):
        return None


def bar_stats(f):
    """从K线算存活时长和活跃度。"""
    try:
        bars = sorted(json.loads(f.read_text(encoding="utf-8")), key=lambda x: x[0])
    except (json.JSONDecodeError, OSError):
        return None
    if len(bars) < 2:
        return None
    life_min = (bars[-1][0] - bars[0][0]) / 60
    active = sum(1 for b in bars if b[5] > 0) / len(bars)
    return {"life_min": life_min, "active_frac": active, "n_bars": len(bars)}


def judge(st, bs):
    """返回(是否通过, 拒绝原因)。"""
    if bs["life_min"] < MIN_LIFE_MIN:
        return False, "早死(存活<2小时)"
    if bs["active_frac"] < MIN_ACTIVE_FRAC:
        return False, "僵尸(大段时间无成交)"
    if st["sellers"] < MIN_SELLERS:
        return False, f"卖家太少({st['sellers']}人,卖不出去)"
    if st["buyers"] < MIN_BUYERS:
        return False, f"买家太少({st['buyers']}人)"
    ratio = st["buys"] / max(st["sells"], 1)
    if ratio > MAX_BUY_SELL_RATIO:
        return False, f"钓鱼盘(买卖比{ratio:.0f}:1,只进不出)"
    sb = st["sellers"] / max(st["buyers"], 1)
    if sb < MIN_SELLER_BUYER:
        return False, f"单向盘(卖家/买家={sb:.2f})"
    return True, "通过"


def main():
    files = [f for f in SRC_DIR.glob("*.json") if f.stat().st_size > 200]
    print(f"待清洗样本: {len(files)}个", flush=True)
    OUT_DIR.mkdir(exist_ok=True)

    kept, rejected = [], {}
    for i, f in enumerate(files, 1):
        bs = bar_stats(f)
        if not bs:
            rejected["数据不足"] = rejected.get("数据不足", 0) + 1
            continue
        # K线层面能判的先判,省API额度
        if bs["life_min"] < MIN_LIFE_MIN:
            rejected["早死(存活<2小时)"] = rejected.get("早死(存活<2小时)", 0) + 1
            continue
        if bs["active_frac"] < MIN_ACTIVE_FRAC:
            rejected["僵尸(大段时间无成交)"] = rejected.get("僵尸(大段时间无成交)", 0) + 1
            continue
        st = pool_stats(f.stem)
        if not st:
            rejected["查不到池子数据"] = rejected.get("查不到池子数据", 0) + 1
            continue
        ok, reason = judge(st, bs)
        if ok:
            shutil.copy(f, OUT_DIR / f.name)
            kept.append({"addr": f.stem, **st, **bs,
                         "buy_sell_ratio": round(st["buys"] / max(st["sells"], 1), 1)})
        else:
            rejected[reason.split("(")[0]] = rejected.get(reason.split("(")[0], 0) + 1
        if i % 50 == 0:
            print(f"  {i}/{len(files)}  通过{len(kept)}个", flush=True)

    REPORT_F.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n=== 清洗结果 ===")
    print(f"通过: {len(kept)}/{len(files)} ({100*len(kept)/max(len(files),1):.0f}%)")
    print(f"\n剔除原因分布:")
    for r, n in sorted(rejected.items(), key=lambda x: -x[1]):
        print(f"  {r}: {n}个")
    if kept:
        ratios = sorted(k["buy_sell_ratio"] for k in kept)
        sellers = sorted(k["sellers"] for k in kept)
        print(f"\n通过样本的特征:")
        print(f"  买卖笔数比: 中位{ratios[len(ratios)//2]:.1f}:1  最高{ratios[-1]:.1f}:1")
        print(f"  独立卖家数: 中位{sellers[len(sellers)//2]}人  最少{sellers[0]}人")


if __name__ == "__main__":
    main()
