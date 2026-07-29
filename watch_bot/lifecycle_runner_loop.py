# -*- coding: utf-8 -*-
"""让lifecycle_logger.py每隔一段时间自动跑一轮,持续攒时间序列数据,不用人工反复
手动触发。这个脚本本身没有分析逻辑,只是个循环调度器。"""
import sys
import time
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lifecycle_logger import scan_and_log

INTERVAL_SEC = 600   # 10分钟一轮
ROUNDS = 30          # 跑5小时(30*10分钟)

STATE_PATH = str(Path(__file__).resolve().parent.parent / "screener_state_local.json")

for i in range(ROUNDS):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] === 第{i+1}/{ROUNDS}轮 ===")
    try:
        scan_and_log(STATE_PATH)
    except Exception as e:
        print(f"本轮出错: {e}")
    if i < ROUNDS - 1:
        time.sleep(INTERVAL_SEC)

print("\n=== 循环结束 ===")
