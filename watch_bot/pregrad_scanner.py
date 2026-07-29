# -*- coding: utf-8 -*-
"""2026-07-29新增: 给pregrad_scalp_exit.py(毕业前抢筹打法)找候选——盯着GT
new_pools最新创建的一批池子(按创建时间,不是mcap_scanner.py那种按MCAP排序,
因为这个信号门槛低、要的是"刚出生、正在被快速拉",跟起点MCAP高不高无关),
命中operator_registry.matches_pregrad_ramp_signature就立刻部署一个
pregrad_scalp_exit.py纸盘实例。

这个策略全程只有2-4分钟寿命(硬超时180秒),所以扫描频率必须比mcap_scanner.py
(90秒一轮)更勤,而且不需要MAX_CONCURRENT_DEPLOYED=8那种长期并发上限——用
一个基于时间的滚动窗口就行(默认240秒内部署过的还算"在跑",不重复部署同一个mint)。

用法: python pregrad_scanner.py
"""
import io
import json
import re
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from check_coin import GT_BASE, S, get
from operator_registry import matches_pregrad_ramp_signature

HERE = Path(__file__).parent
SEEN_F = HERE / "pregrad_seen.json"
# 2026-07-29晚间新增: 原来判断是"这个策略活得短,不需要长期并发上限",但没考虑到
# 一轮扫描可能同时命中好几个候选,叠加起来的并发数不受限——白天把轮询从5秒调紧到
# 3秒+1.5秒自适应加密复查后,实测云端43分钟没有一笔交易完成,GT接口本身响应正常
# (0.1-0.4秒),说明是同时活跃的仓位太多、请求量自己把限流顶爆了。补一个硬上限。
MAX_CONCURRENT = 6
DEPLOY_WINDOW_SEC = 240  # 比脚本自己的180秒硬超时留一点余量,过了这个时间就当它已经跑完了


def make_prefix(addr):
    return re.sub(r"[^A-Za-z0-9]", "", addr)[:8]


def load_seen():
    if SEEN_F.exists():
        return json.loads(SEEN_F.read_text(encoding="utf-8"))
    return {}


def save_seen(seen):
    SEEN_F.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")


def fetch_new_pools():
    d = get(S, f"{GT_BASE}/networks/solana/new_pools", {"page": 1})
    return (d or {}).get("data", [])


def deploy(addr, mint, name):
    prefix = make_prefix(addr)
    py = sys.executable
    kwargs = {"cwd": str(HERE), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        # 2026-07-29晚间修复: 用户反馈屏幕上一直弹cmd窗口——DETACHED_PROCESS只是让
        # 子进程脱离父进程的控制台,不等于"不开窗口",真正管这个的是CREATE_NO_WINDOW,
        # 之前漏加了。
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen([py, str(HERE / "pregrad_scalp_exit.py"), addr, mint, prefix], **kwargs)
    print(f"  *** 已部署毕业前抢筹纸盘: {name} ({addr[:10]}...) ***")


def main():
    seen = load_seen()
    now = time.time()
    # 清掉已经跑完(超过部署窗口)的旧记录,不然seen会无限膨胀
    seen = {k: v for k, v in seen.items() if now - v < DEPLOY_WINDOW_SEC}

    rows = fetch_new_pools()
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] 拉到{len(rows)}个最新池子,当前正在跑的纸盘: {len(seen)}个")

    n_deployed = 0
    for row in rows:
        if len(seen) >= MAX_CONCURRENT:
            print(f"  已达并发上限({MAX_CONCURRENT}个在跑),本轮不再新部署,等旧仓位跑完腾位置")
            break
        a = row["attributes"]
        addr = a.get("address")
        if not addr or addr in seen:
            continue
        created = a.get("pool_created_at")
        if not created:
            continue
        try:
            age_min = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 60
        except ValueError:
            continue
        if not matches_pregrad_ramp_signature(a, age_min):
            continue

        rel = row.get("relationships", {})
        base_token_id = rel.get("base_token", {}).get("data", {}).get("id", "")
        mint = base_token_id.split("_")[-1] if "_" in base_token_id else None
        if not mint:
            continue

        deploy(addr, mint, a.get("name"))
        seen[addr] = now
        n_deployed += 1

    save_seen(seen)
    print(f"本轮新部署: {n_deployed}个")


if __name__ == "__main__":
    main()
