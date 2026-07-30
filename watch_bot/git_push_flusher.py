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
from git_lock import git_lock, resolve_stuck_merge, run_git, GIT_PULL_CMD

REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP_INTERVAL_SEC = int(os.environ.get("FLUSH_INTERVAL_SEC", "45"))
# VPS用Scheduled Task常驻,不设上限就一直跑;云端job有5小时45分的总时间预算,
# 要跟其他3个循环一样提前收尾退出,不然`wait`会一直等这个死循环,直到被
# GitHub Actions的350分钟job超时直接SIGKILL,可能打断其他脚本正常收尾。
MAX_ROUNDS = int(os.environ.get("FLUSH_MAX_ROUNDS", "0"))  # 0 = 不限(VPS默认)


def run(cmd):
    return run_git(cmd, REPO_ROOT)


def sweep_once():
    ts = dt.datetime.now().strftime("%H:%M:%S")
    run(["git", "fetch", "origin", "master", "--quiet"])
    behind = run(["git", "rev-list", "--count", "origin/master..HEAD"])
    try:
        n_behind = int((behind.stdout or "0").strip())
    except ValueError:
        n_behind = 0
    if n_behind == 0:
        # 2026-07-30再补: 之前n_behind==0时直接静默返回,导致"一切正常"和
        # "卡在某个subprocess调用里没输出"在日志里长得一模一样(都是空白),
        # 排查用户反馈"积压没清空"时,曾经因为看不到任何心跳而怀疑扫地机
        # 是不是本身卡住了。每轮都打一行心跳,让日志本身就能证明"活着"。
        print(f"[{ts}] 心跳: 无积压,一切正常")
        return

    print(f"[{ts}] 本地领先origin {n_behind}个未推送提交,尝试补推")

    with git_lock() as got_lock:
        if not got_lock:
            print(f"[{ts}] 拿不到git锁,交给下一轮")
            return
        if resolve_stuck_merge(REPO_ROOT, log=print):
            print(f"[{ts}] 清理了遗留的未解决合并冲突")
        last_push = last_pull = None
        for _ in range(6):
            last_push = run(["git", "push"])
            if last_push.returncode == 0:
                print(f"[{ts}] 补推成功")
                return
            last_pull = run(GIT_PULL_CMD)
            time.sleep(5)
        # 2026-07-31新增: 云端那次13小时静默丢数据,就是因为这里只打"失败"两个字
        # 不打原因,从日志上完全看不出是撞车还是配置问题,排查绕了一大圈。以后
        # 失败必须带上git自己的报错原文(截断到200字符,防止刷屏)。
        print(f"[{ts}] 补推仍然失败(重试6次),下一轮再试")
        if last_push is not None:
            print(f"[{ts}]   push报错: {(last_push.stderr or last_push.stdout or '')[:200]}")
        if last_pull is not None and last_pull.returncode != 0:
            print(f"[{ts}]   pull报错: {(last_pull.stderr or last_pull.stdout or '')[:200]}")


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
