# -*- coding: utf-8 -*-
"""2026-07-30新增: VPS把并发调到6之后,好几个pregrad_scalp_exit.py/
post_grad_scalp_exit.py/snipe_exit.py/lifecycle_runner_loop.py同时想commit+
push,互相撞车——每个进程自己的3次重试(pull merge重试)在高并发下大概率还是
撞得上下一个进程,导致大量交易记录堆在本地推不上去(实测VPS本地攒了30个
未推送提交,手动push一次就成功了,说明不是网络/权限问题,是并发竞争问题)。

用一个简单的文件锁,让这几个脚本的git操作互相排队,一次只有一个进程在做
add/commit/push,而不是同时抢。Windows下用O_CREAT|O_EXCL模拟独占锁,拿不到
锁就短暂等待重试,不是长时间阻塞(git操作本身很快,排队等待成本远小于并发
互相打架导致的失败重试成本)。
"""
import os
import re
import time
import subprocess
import contextlib
from pathlib import Path

LOCK_PATH = Path(__file__).parent / "git_push.lock"

_MARKER_RE = re.compile(r"^(<{7} |={7}$|>{7} )")

# 2026-07-31修复: 云端13小时"零成交"的真正根因——git 2.34+在分支分叉时,没有
# pull.rebase/pull.ff配置的裸`git pull`会直接报错拒绝("Need to specify how to
# reconcile divergent branches")。VPS/本机的git有全局配置所以从来没踩到,GH
# Actions的runner是全新checkout+git 2.54,什么配置都没有——之前把--rebase改成
# 普通merge的修复,在云端恰好变成了"每次pull都确定性失败",重试多少次都没用,
# 云端整个run攒了57个提交的交易数据最后随容器销毁全部丢失。所有自动化pull
# 统一用-c显式钉死合并策略,不再依赖跑在哪台机器上的本地配置。
GIT_PULL_CMD = ["git", "-c", "pull.rebase=false", "pull", "--no-edit", "origin", "master"]


class _TimedOut:
    """subprocess.run超时后的占位返回值,伪装成一个失败的CompletedProcess,
    调用方原有的`.returncode == 0`判断不用改就能正确当成这次失败处理。"""
    returncode = 124
    stdout = ""
    stderr = "[git_lock] subprocess timed out"


def run_git(cmd, cwd, timeout=60, **kw):
    """2026-07-30再补: 之前所有git调用都没有超时保护——如果某次git fetch/push
    因为网络抖动真的卡住不返回(不是重试失败,是subprocess.run本身不返回),
    锁会被这个进程永久占着,其余脚本全部卡在等锁上,这是比"积压暂时清不掉"
    严重得多的故障模式(真正的死锁,不是误报)。给所有git子进程调用统一加
    超时,超时就当这次失败处理,交给外层retry逻辑,不再无限期等待。"""
    try:
        return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return _TimedOut()


@contextlib.contextmanager
def git_lock(timeout=30, poll=0.3):
    """拿不到锁最多等timeout秒,还拿不到就放弃(返回False),调用方自己决定
    要不要跳过这次推送、留给下一轮补推——总比无限等待卡死整个进程强。"""
    deadline = time.time() + timeout
    fd = None
    while time.time() < deadline:
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(poll)
    if fd is None:
        yield False
        return
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield True
    finally:
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass


def resolve_stuck_merge(repo_root, log=print):
    """2026-07-30再修: 真正的根因排查——journal.jsonl里两次出现的字面冲突
    标记(<<<<<<< / ======= / >>>>>>>)不是历史遗留,是还在持续发生的bug:
    如果某一轮`git pull --no-edit`真的撞上一个自动合并解决不了的冲突(不是
    "两边都只是各自追加新行"这种简单情况),git会把冲突标记原样留在工作区
    文件里、把仓库晾在"合并中"状态,而重试循环拿不到新提交,push还是会
    失败,6次retry耗尽后直接放弃——但从没人真正解决这个卡住的合并。
    下一轮任何脚本一旦对同一个文件`git add`+`git commit`,git会把当前工作区
    内容(冲突标记原样还在)当成"已经手动解决"直接完成这次合并提交,冲突
    标记就这样被永久烤进了历史——这才是两次复现同一个bug、且commit hash
    每次都不一样的真正原因。

    在每次真正commit之前,先检查有没有这种被前一轮晾在半路的合并:有的话
    按文件类型自动收拾干净,而不是放任下一次commit糊里糊涂地把它坐实。"""
    merge_head = repo_root / ".git" / "MERGE_HEAD"
    if not merge_head.exists():
        return False

    status = run_git(["git", "status", "--porcelain"], repo_root)
    conflicted = [line[3:].strip() for line in status.stdout.splitlines() if line.startswith("UU ")]
    if not conflicted:
        # MERGE_HEAD在,但没有UU文件了(可能已经被手动清过),直接尝试收尾
        run_git(["git", "commit", "--no-edit"], repo_root)
        return True

    for rel_path in conflicted:
        full = repo_root / rel_path
        if rel_path.endswith(".jsonl"):
            # 逐行独立的台账文件: 两边各自的行都是真实数据,不能只选一边,
            # 把冲突标记这几行去掉、两边内容全部保留(union),不会丢交易记录。
            if full.exists():
                text = full.read_text(encoding="utf-8")
                kept = [ln for ln in text.splitlines(keepends=True)
                        if not _MARKER_RE.match(ln.rstrip("\n"))]
                full.write_text("".join(kept), encoding="utf-8")
        else:
            # 整体结构化的json缓存/日志文件: 冲突意味着两边改的是同一段内容,
            # 拼接两边行内容会拼出无法解析的文件,宁可用自己这边(HEAD)最新
            # 版本覆盖、丢一点对方的缓存状态,也不要产出损坏的结构化文件。
            run_git(["git", "checkout", "--ours", rel_path], repo_root)
        run_git(["git", "add", rel_path], repo_root)

    commit = run_git(["git", "commit", "--no-edit"], repo_root)
    log(f"发现上一轮遗留的未解决合并,已自动清理冲突标记并收尾提交: {conflicted}")
    return True
