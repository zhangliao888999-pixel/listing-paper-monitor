# -*- coding: utf-8 -*-
"""让pregrad_scanner.py反复跑——这个策略的整个窗口只有2-4分钟,比mcap_scanner.py
盯的那套(要等起点MCAP、可能几十分钟才见分晓)快得多,所以扫描间隔也要跟着收紧,
不然新池子从"刚满足信号"到"已经毕业/死了"之间可能整轮都扫不到。"""
import os
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path

HERE = Path(__file__).parent
INTERVAL_SEC = 30
# 2026-07-29晚间改: 原1小时(120轮)是配合逐小时手动续,通宵没人盯着风险更高,改成
# 10小时(1200*30秒)覆盖一整晚睡眠时间。2026-07-29白天再改: 迁移到GitHub Actions后
# 单个job最长跑不到10小时(免费版runner硬顶6小时),用LOOP_ROUNDS环境变量覆盖,
# 云端workflow设成匹配单次job时长的轮数,本地不设这个环境变量时还是原来的默认值。
ROUNDS = int(os.environ.get("LOOP_ROUNDS", "1200"))

for i in range(ROUNDS):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] === 第{i+1}/{ROUNDS}轮 ===")
    try:
        result = subprocess.run([sys.executable, str(HERE / "pregrad_scanner.py")], cwd=str(HERE))
        # 2026-07-29晚间新增: subprocess.run不带check=True,子进程崩溃(比如
        # pregrad_seen.json损坏导致的JSONDecodeError)不会被这层try/except抓到,
        # 只会静默地"这轮什么也没打印",云端连续几十分钟看不出任何异常。加一行
        # 显式检查退出码,崩溃了至少在日志里喊一声,方便事后排查。
        if result.returncode != 0:
            print(f"警告: pregrad_scanner.py本轮退出码非0({result.returncode}),可能崩溃了")
    except Exception as e:
        print(f"本轮出错: {e}")
    if i < ROUNDS - 1:
        time.sleep(INTERVAL_SEC)

print("\n=== 循环结束 ===")
