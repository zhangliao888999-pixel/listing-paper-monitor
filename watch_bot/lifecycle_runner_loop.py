# -*- coding: utf-8 -*-
"""让lifecycle_logger.py每隔一段时间自动跑一轮,持续攒时间序列数据,不用人工反复
手动触发。

2026-07-29新增: 每轮跑完之后自动commit+push watch_bot/的数据文件——用户在
docs/journal.html公开页面上问"多久自动更新一次",之前的答案很尴尬:页面本身
每30秒重新拉取,但底层数据只有我手动push的时候才会变,不是真正的自动更新。
现在补上,让数据采集这一步本身就包含推送,页面才算名副其实地"自动更新"。
"""
import os
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lifecycle_logger import scan_and_log
from git_lock import git_lock, resolve_stuck_merge, run_git

# 2026-07-30改: 实盘首跑10分钟没有任何开仓,排查发现这条腿10分钟一轮的节奏
# 是纸盘时代跟pregrad(30秒)/mcap(90秒)两条高频腿并行时定的——那时候它慢无
# 所谓,发现主力是另外两条腿。现在VPS上纸盘腿停了,它成了唯一/主要的发现
# 入口,10分钟一轮等于大部分1-30分钟年龄窗口的候选还没被看到就已经过期。
# 改成环境变量可调,实盘模式(vps_run_forever.ps1 -LiveMode)设180秒,云端
# 纸盘不设时维持600秒不变。
INTERVAL_SEC = int(os.environ.get("LIFECYCLE_INTERVAL_SEC", "600"))
# 2026-07-29晚间改: 原1小时(6轮)是为了切成小段方便随时检查,但通宵没人盯着重启,
# 断档风险比"看不到中途进展"更糟,改成10小时(60*10分钟),覆盖一整晚睡眠时间。
# 白天再改: 云端job单次最长6小时,用LOOP_ROUNDS环境变量覆盖,本地不设时还是默认值。
ROUNDS = int(os.environ.get("LOOP_ROUNDS", "60"))

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = str(REPO_ROOT / "screener_state_local.json")

# 只提交这几类数据文件,不用git add -A(避免误把__pycache__/lock文件这些噪音提交进去)
DATA_GLOBS = ["watch_bot/*.jsonl", "watch_bot/*.json", "watch_bot/*.log"]


def run(cmd, **kw):
    return run_git(cmd, REPO_ROOT, **kw)


def git_sync():
    """add指定的数据文件 -> commit(没有变化就跳过) -> pull(普通merge,不是
    rebase) -> push,push失败重试几次(跟live_runner.py云端workflow那套retry
    逻辑一样,本地多个定时任务/脚本并发提交是这个仓库的常态)。

    2026-07-30新增: VPS并发调到6之后好几个进程同时commit+push互相撞车,单靠
    重试次数扛不住,加文件锁让这几个脚本的git操作排队,一次只有一个在做。"""
    with git_lock() as got_lock:
        if not got_lock:
            print("  拿不到git锁(30秒超时,可能有很多进程在排队),这次先不推,交给下一轮补推")
            return
        if resolve_stuck_merge(REPO_ROOT, log=print):
            print("  清理了上一轮遗留的未解决合并冲突")
        add_cmd = ["git", "add"] + DATA_GLOBS
        run(add_cmd)
        commit = run(["git", "commit", "-m", f"watch_bot data sync {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}"])
        if commit.returncode != 0 and "nothing to commit" in (commit.stdout + commit.stderr):
            return  # 这轮没有新数据变化,不用推
        # 2026-07-30再修: 本机screener_local和云端GitHub Actions也在并发push
        # 同一仓库,锁挡不住跨主机的fetch-first冲突,3次重试偶尔追不上,加到
        # 6次+间隔拉长到5秒。
        for attempt in range(6):
            push = run(["git", "push"])
            if push.returncode == 0:
                print(f"  git同步成功(第{attempt+1}次尝试)")
                return
            # 2026-07-30修复: --rebase遇到journal.jsonl/pump_lifecycle.json这类只追加
            # 型文件冲突时会直接卡住需要人工--abort才能恢复,改用普通merge能自动合并
            # "两边各自追加了新行"这种冲突,不需要人工介入。VPS并发调高后实测踩到过
            # 这个坑(rebase卡死导致连续多笔交易记录推不上去)。
            run(["git", "pull", "--no-edit", "origin", "master"])
            time.sleep(5)
        print("  git同步失败,重试6次后放弃,下一轮再试")


def git_refresh():
    """2026-07-30新增: 每轮扫描前先拉一次远端——发现的候选来源是本机screener
    推上来的screener_state_local.json,VPS这边如果没人定期pull,这个文件会
    一直停在部署那一刻的版本(实测停了13分钟,GitHub上已经有新版本),年龄
    过滤(1-30分钟新币)会把过期候选全部筛掉,表现就是每轮都"发现0个新池子"、
    实盘永远等不来第一笔交易。之前纸盘时代没暴露这个问题,是因为push重试
    里的pull顺带把数据带新了——现在交易少了push也少,不能再靠那个副作用。"""
    with git_lock() as got_lock:
        if not got_lock:
            return
        resolve_stuck_merge(REPO_ROOT, log=print)
        pull = run(["git", "pull", "--no-edit", "origin", "master"])
        if pull.returncode != 0:
            print(f"  轮前刷新pull失败(不影响本轮扫描,用现有数据继续): {(pull.stderr or '')[:120]}")


for i in range(ROUNDS):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] === 第{i+1}/{ROUNDS}轮 ===")
    git_refresh()
    try:
        scan_and_log(STATE_PATH)
    except Exception as e:
        print(f"本轮出错: {e}")
    git_sync()
    if i < ROUNDS - 1:
        time.sleep(INTERVAL_SEC)

print("\n=== 循环结束 ===")
