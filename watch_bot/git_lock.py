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
import time
import contextlib
from pathlib import Path

LOCK_PATH = Path(__file__).parent / "git_push.lock"


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
