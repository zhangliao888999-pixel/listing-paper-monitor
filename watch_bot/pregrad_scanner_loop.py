# -*- coding: utf-8 -*-
"""让pregrad_scanner.py反复跑——这个策略的整个窗口只有2-4分钟,比mcap_scanner.py
盯的那套(要等起点MCAP、可能几十分钟才见分晓)快得多,所以扫描间隔也要跟着收紧,
不然新池子从"刚满足信号"到"已经毕业/死了"之间可能整轮都扫不到。"""
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path

HERE = Path(__file__).parent
INTERVAL_SEC = 30
ROUNDS = 120   # 跑1小时(120*30秒)

for i in range(ROUNDS):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] === 第{i+1}/{ROUNDS}轮 ===")
    try:
        subprocess.run([sys.executable, str(HERE / "pregrad_scanner.py")], cwd=str(HERE))
    except Exception as e:
        print(f"本轮出错: {e}")
    if i < ROUNDS - 1:
        time.sleep(INTERVAL_SEC)

print("\n=== 循环结束 ===")
