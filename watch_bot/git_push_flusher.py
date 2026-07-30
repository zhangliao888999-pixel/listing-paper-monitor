# -*- coding: utf-8 -*-
"""2026-07-30新增: 4个交易脚本每笔交易一完成就想立刻push,3方(VPS本机+本机
screener_local+云端GitHub Actions)同时写同一个仓库,即使把重试从3次/3秒
加到6次/5秒,密集的时候还是会有个别几次全部重试耗尽、把这一批提交晾在
本地——这不是死锁也不是崩溃(手动push瞬间就成功),只是运气不好没赶上
干净的推送窗口,但监控窗口里积压数字变大看着像"卡住了"。

这个脚本单独跑一个轻量级循环,只做一件事: 每隔一段时间检查本地有没有
落后origin的提交,有的话按同样的pull-merge-push retry逻辑推一次。相当于
给那4个脚本的"实时push"加一个保底扫地机,不需要4个脚本自己在同一时刻都
恰好成功,只要这个扫地机隔几十秒能逮到一次干净窗口,积压就会被清空。
"""
import os
import sys
import time
import subprocess
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from git_lock import git_lock, resolve_stuck_merge

REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP_INTERVAL_SEC = int(os.environ.get("FLUSH_INTERVAL_SEC", "45"))
# VPS用Scheduled Task常驻,不设上限就一直跑;云端job有5小时45分的总时间预算,
# 要跟其他3个循环一样提前收尾退出,不然`wait`会一直等这个死循环,直到被
# GitHub Actions的350分钟job超时直接SIGKILL,可能打断其他脚本正常收尾。
MAX_ROUNDS = int(os.environ.get("FLUSH_MAX_ROUNDS", "0"))  # 0 = 不限(VPS默认)


def run(cmd):
    return subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)


def sweep_once():
    run(["git", "fetch", "origin", "master", "--quiet"])
    behind = run(["git", "rev-list", "--count", "origin/master..HEAD"])
    try:
        n_behind = int((behind.stdout or "0").strip())
    except ValueError:
        n_behind = 0
    if n_behind == 0:
        return

    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] 本地领先origin {n_behind}个未推送提交,尝试补推")

    with git_lock() as got_lock:
        if not got_lock:
            print(f"[{ts}] 拿不到git锁,交给下一轮")
            return
        if resolve_stuck_merge(REPO_ROOT, log=print):
            print(f"[{ts}] 清理了遗留的未解决合并冲突")
        for _ in range(6):
            push = run(["git", "push"])
            if push.returncode == 0:
                print(f"[{ts}] 补推成功")
                return
            run(["git", "pull", "--no-edit", "origin", "master"])
            time.sleep(5)
        print(f"[{ts}] 补推仍然失败(重试6次),下一轮再试")


def main():
    print(f"=== git推送保底扫地机启动,每{SWEEP_INTERVAL_SEC}秒检查一次积压 ===")
    round_no = 0
    while MAX_ROUNDS == 0 or round_no < MAX_ROUNDS:
        round_no += 1
        try:
            sweep_once()
        except Exception as e:
            print(f"扫地机本轮出错(不影响下一轮): {e}")
        time.sleep(SWEEP_INTERVAL_SEC)
    print(f"=== 达到{MAX_ROUNDS}轮上限,扫地机收尾退出 ===")


if __name__ == "__main__":
    main()
