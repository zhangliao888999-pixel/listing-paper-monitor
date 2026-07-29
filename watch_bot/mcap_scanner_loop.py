# -*- coding: utf-8 -*-
"""让mcap_scanner.py高频反复跑——用户的洞察: 只盯new_pools第一页(最新~20个
池子)按MCAP排序,一旦有操盘方钱包真金白银开始砸某个币,MCAP会迅速冲进这一页
的前几名,不需要深翻页/大批量扫描,所以这个可以跑得很勤(便宜,一次只查约
10个候选的详情)。"""
import os
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path

HERE = Path(__file__).parent
INTERVAL_SEC = 90   # 90秒一轮,比lifecycle_runner_loop(10分钟)勤得多,因为这个便宜
# 2026-07-29晚间改: 原1小时(40轮)是配合逐小时手动续,通宵没人盯着风险更高,
# 改成10小时(400*90秒),覆盖一整晚睡眠时间。白天再改: 云端job单次最长6小时,
# 用LOOP_ROUNDS环境变量覆盖,本地不设时还是默认值。
ROUNDS = int(os.environ.get("LOOP_ROUNDS", "400"))

for i in range(ROUNDS):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] === 第{i+1}/{ROUNDS}轮 ===")
    try:
        # 2026-07-29实测: 并发数调高完全没用(4个并发反而比1个更慢更多失败),
        # 真正瓶颈是所有后台进程加起来的总请求量在打同一个限流接口,不是这个
        # 脚本自己的并发设置。老实用1个线程。
        subprocess.run([sys.executable, str(HERE / "mcap_scanner.py"), "10", "1"], cwd=str(HERE))
    except Exception as e:
        print(f"本轮出错: {e}")
    if i < ROUNDS - 1:
        time.sleep(INTERVAL_SEC)

print("\n=== 循环结束 ===")
