# -*- coding: utf-8 -*-
"""2026-07-31新建: CoinGecko API 统一客户端(带key + 节流 + 重试)。

背景: 之前直接打GeckoTerminal公开接口,免费限额只有约10-30请求/分钟,本机
反复429导致采集器磨了半天0样本。注册CoinGecko免费Demo key后额度提到
100请求/分钟(10倍),同样的onchain数据改走 api.coingecko.com/api/v3/onchain/*
端点,路径结构跟GT基本一一对应。

key从 .cg_api_key 文件读(已加入.gitignore,不进git历史、不进对话记录)。
没有key时自动退回GT公开接口,保证脚本在任何环境都能跑,只是慢。
"""
import os
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
KEY_F = HERE / ".cg_api_key"

# 2026-07-31: Demo版额度是1万次/月(不是10万,已在仪表盘核对),100次/分钟。
# 这个额度很紧,必须省着用:
#   - 短时高强度任务(回测拉K线,一轮约900次)-> 用key,值得
#   - 常驻低频任务(VPS前向采集器,每天4320次)-> 绝不能用key,两天半就烧光,
#     它走GT公开接口完全够用(频率低,不会触发限流)
# 用 CG_NO_KEY=1 显式禁用key,常驻脚本必须设这个。
_key = None
if KEY_F.exists():
    _key = KEY_F.read_text(encoding="utf-8").strip() or None
_key = os.environ.get("CG_API_KEY", _key)
if os.environ.get("CG_NO_KEY") == "1":
    _key = None

HAS_KEY = bool(_key)
# 有key走CoinGecko onchain端点(100/min),没key退回GT公开接口(~10-30/min)
CG_BASE = "https://api.coingecko.com/api/v3/onchain"
GT_BASE = "https://api.geckoterminal.com/api/v2"
BASE = CG_BASE if HAS_KEY else GT_BASE

# Demo版100请求/分钟 = 0.6s/请求,留20%余量按0.75s;没key时保守到3s
MIN_GAP_SEC = float(os.environ.get("CG_MIN_GAP", "0.75" if HAS_KEY else "3.0"))

_H = {"Accept": "application/json;version=20230302",
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
if HAS_KEY:
    _H["x-cg-demo-api-key"] = _key

_last = [0.0]
_req_count = [0]   # 本进程累计请求数,退出时由atexit上报给quota_tracker


def get(path, params=None, tries=4):
    """path是相对路径,如 'networks/solana/trending_pools'。
    返回json dict或None。全局节流保证不超限。"""
    url = f"{BASE}/{path.lstrip('/')}"
    for i in range(tries):
        gap = time.time() - _last[0]
        if gap < MIN_GAP_SEC:
            time.sleep(MIN_GAP_SEC - gap)
        _last[0] = time.time()
        if HAS_KEY:
            _req_count[0] += 1
        try:
            r = requests.get(url, params=params, headers=_H, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(3 * (i + 1))
                continue
            if r.status_code in (404, 400):
                return None
        except requests.RequestException:
            time.sleep(2 * (i + 1))
    return None


def ohlcv_minute(pool_addr, limit=1000, network="solana"):
    d = get(f"networks/{network}/pools/{pool_addr}/ohlcv/minute",
            {"aggregate": 1, "limit": limit})
    if not d:
        return []
    return d.get("data", {}).get("attributes", {}).get("ohlcv_list", []) or []


if __name__ == "__main__":
    print(f"key已加载: {HAS_KEY}   base={BASE}   最小间隔={MIN_GAP_SEC}s")
    d = get("networks/solana/trending_pools", {"duration": "6h", "page": 1})
    print("trending返回行数:", len((d or {}).get("data", [])))


# 进程退出时把本次消耗记进额度台账(只统计用了key的请求;走公开接口的不占额度)
import atexit as _atexit


def _report_usage():
    if not HAS_KEY or _req_count[0] == 0:
        return
    try:
        import quota_tracker
        quota_tracker.record(_req_count[0], os.environ.get("CG_TAG", "unknown"))
        used, pct, warn = quota_tracker.check_and_warn()
        print(f"[额度] 本次用了{_req_count[0]}次, 本月累计{used}/{quota_tracker.MONTHLY_LIMIT} ({pct:.1f}%)")
        if warn:
            print(warn)
    except Exception:
        pass


_atexit.register(_report_usage)
