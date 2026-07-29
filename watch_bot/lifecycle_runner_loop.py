# -*- coding: utf-8 -*-
"""让lifecycle_logger.py每隔一段时间自动跑一轮,持续攒时间序列数据,不用人工反复
手动触发。

2026-07-29新增: 每轮跑完之后自动commit+push watch_bot/的数据文件——用户在
docs/journal.html公开页面上问"多久自动更新一次",之前的答案很尴尬:页面本身
每30秒重新拉取,但底层数据只有我手动push的时候才会变,不是真正的自动更新。
现在补上,让数据采集这一步本身就包含推送,页面才算名副其实地"自动更新"。
"""
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lifecycle_logger import scan_and_log

INTERVAL_SEC = 600   # 10分钟一轮
ROUNDS = 6           # 跑1小时(6*10分钟)——切成小段跑,方便及时检查+发现新币就推送通知,
                     # 不用一次挂5小时看不到中途进展

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = str(REPO_ROOT / "screener_state_local.json")

# 只提交这几类数据文件,不用git add -A(避免误把__pycache__/lock文件这些噪音提交进去)
DATA_GLOBS = ["watch_bot/*.jsonl", "watch_bot/*.json", "watch_bot/*.log"]


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, **kw)


def git_sync():
    """add指定的数据文件 -> commit(没有变化就跳过) -> pull --rebase -> push,
    push失败重试几次(跟live_runner.py云端workflow那套retry逻辑一样,本地
    多个定时任务/脚本并发提交是这个仓库的常态)。"""
    add_cmd = ["git", "add"] + DATA_GLOBS
    run(add_cmd)
    commit = run(["git", "commit", "-m", f"watch_bot data sync {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}"])
    if commit.returncode != 0 and "nothing to commit" in (commit.stdout + commit.stderr):
        return  # 这轮没有新数据变化,不用推
    for attempt in range(3):
        push = run(["git", "push"])
        if push.returncode == 0:
            print(f"  git同步成功(第{attempt+1}次尝试)")
            return
        run(["git", "pull", "--rebase", "origin", "master"])
        time.sleep(3)
    print("  git同步失败,重试3次后放弃,下一轮再试")


for i in range(ROUNDS):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] === 第{i+1}/{ROUNDS}轮 ===")
    try:
        scan_and_log(STATE_PATH)
    except Exception as e:
        print(f"本轮出错: {e}")
    git_sync()
    if i < ROUNDS - 1:
        time.sleep(INTERVAL_SEC)

print("\n=== 循环结束 ===")
