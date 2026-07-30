# -*- coding: utf-8 -*-
"""2026-07-29新增: 给pregrad_scalp_exit.py(毕业前抢筹打法)找候选——盯着GT
new_pools最新创建的一批池子(按创建时间,不是mcap_scanner.py那种按MCAP排序,
因为这个信号门槛低、要的是"刚出生、正在被快速拉",跟起点MCAP高不高无关),
命中operator_registry.matches_pregrad_ramp_signature就立刻部署一个
pregrad_scalp_exit.py纸盘实例。

这个策略全程只有2-4分钟寿命(硬超时180秒),所以扫描频率必须比mcap_scanner.py
(90秒一轮)更勤,而且不需要MAX_CONCURRENT_DEPLOYED=8那种长期并发上限——用
一个基于时间的滚动窗口就行(默认240秒内部署过的还算"在跑",不重复部署同一个mint)。

用法: python pregrad_scanner.py
"""
import io
import json
import os
import re
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from check_coin import GT_BASE, S, get
from operator_registry import matches_pregrad_ramp_signature

HERE = Path(__file__).parent
SEEN_F = HERE / "pregrad_seen.json"
# 2026-07-29晚间新增: 原来判断是"这个策略活得短,不需要长期并发上限",但没考虑到
# 一轮扫描可能同时命中好几个候选,叠加起来的并发数不受限——白天把轮询从5秒调紧到
# 3秒+1.5秒自适应加密复查后,实测云端43分钟没有一笔交易完成,GT接口本身响应正常
# (0.1-0.4秒),说明是同时活跃的仓位太多、请求量自己把限流顶爆了。补一个硬上限。
# 2026-07-29晚间再调低: 加了这个上限之后云端还是反复卡死(连续3次重启,每次
# 10-15分钟左右lifecycle_runner_loop自己的10分钟同步都停摆),这个上限只管
# pregrad这一条腿,跟mcap_scanner那边的MAX_CONCURRENT_DEPLOYED完全独立,两边
# 加起来系统总并发量还是很高。云端在GitHub Actions共享IP上,能扛住的限流预算
# 大概率比本地小,先压到3,配合mcap那边从8压到4,把系统总并发砍掉一半以上。
# 2026-07-30新增: VPS没有GitHub Actions那种共享IP限流顾虑,用户想试试把并发调高
# 是不是能提升扫描效率,跟云端(继续用默认值3)直接对比。加环境变量开关,不改
# 默认值,VPS这边单独设PREGRAD_MAX_CONCURRENT=6来测试。
MAX_CONCURRENT = int(os.environ.get("PREGRAD_MAX_CONCURRENT", "3"))
DEPLOY_WINDOW_SEC = 240  # 比脚本自己的180秒硬超时留一点余量,过了这个时间就当它已经跑完了

# 2026-07-31新增: pregrad上实盘(用户用数据拍板: 这条腿565笔/天 vs snipe腿35笔/天,
# 要快速攒实盘样本只能靠它)。SNIPE_LIVE_MODE=1时spawn的pregrad_scalp_exit.py
# 带LIVE_TRADING环境变量真实下单(走PumpPortal构造+本地签名);实盘名额跟snipe腿
# 共用同一个.live_position_open标记文件,全局同时只有MAX_CONCURRENT_LIVE个真实
# 仓位。没设私钥就拒绝下单只跳过,绝不静默降级成纸盘。
SNIPE_LIVE_MODE = os.environ.get("SNIPE_LIVE_MODE") == "1"
LIVE_POS_SIZE_USD = os.environ.get("LIVE_POS_SIZE_USD", "5")
# 2026-07-31新增: 用户要求分阶段验证——先跑N笔实盘就自动停止开新仓,人工检查
# 没问题再放开。0=不限。统计口径: journal.jsonl里dry_run=false且
# found_via=pregrad_ramp的记录总数(账本是append-only的,这个数只增不减)。
LIVE_MAX_TRADES = int(os.environ.get("LIVE_MAX_TRADES", "0"))
# 2026-07-31新增(用户拍板): 实盘只做Meteora DBC系——分台统计里pump.fun均值
# -2.7%、Meteora系+13.6%,利润几乎全在后者,实盘专注有验证优势的那一半,
# 其他发射台留在云端纸盘继续攒数据观察。按GT的dex slug关键词过滤
# (meteora-dbc/meteora-damm-v2都算),设成空字符串则不过滤(全发射台实盘)。
LIVE_DEX_KEYWORD = os.environ.get("LIVE_DEX_KEYWORD", "meteora")
JOURNAL_F = HERE / "journal.jsonl"
from lifecycle_logger import count_live_open_positions, MAX_CONCURRENT_LIVE


def count_live_pregrad_trades():
    """已完成 + 在途 的实盘笔数。

    2026-07-31: 并发提到6之后,只数journal里已完成的笔数会超额——最多可能有
    6笔在途还没写台账,限额20会实际跑到26笔。把当前持仓数也算进来,让限额
    是"总共投入过多少笔"的真实上限。"""
    n = 0
    if JOURNAL_F.exists():
        with JOURNAL_F.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("dry_run") is False and rec.get("found_via") == "pregrad_ramp":
                    n += 1
    return n + count_live_open_positions(HERE)


def make_prefix(addr):
    return re.sub(r"[^A-Za-z0-9]", "", addr)[:8]


def load_seen():
    if SEEN_F.exists():
        try:
            return json.loads(SEEN_F.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # 2026-07-29晚间修复: 本地实测踩到过一次——之前git stash pop冲突时
            # 这个文件被留下了没解决的冲突标记,变成无效JSON,导致每一轮都在这一行
            # 直接崩溃退出。pregrad_scanner_loop.py用subprocess.run(没有check=True)
            # 调这个脚本,子进程崩溃不会让外层循环报错,只会静默地"什么也没发生",
            # 每30秒重复失败,整条腿彻底停摆却看不出任何异常。文件坏了就清空重来,
            # 顶多重复部署几个已经在跑的候选,总比直接死循环卡死强。
            print(f"警告: {SEEN_F}内容不是有效JSON,当作空的重新开始")
            return {}
    return {}


def save_seen(seen):
    SEEN_F.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")


def fetch_new_pools():
    d = get(S, f"{GT_BASE}/networks/solana/new_pools", {"page": 1})
    return (d or {}).get("data", [])


def deploy(addr, mint, name, dex_id=""):
    prefix = make_prefix(addr)
    py = sys.executable
    kwargs = {"cwd": str(HERE), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        # 2026-07-29晚间修复: 用户反馈屏幕上一直弹cmd窗口——DETACHED_PROCESS只是让
        # 子进程脱离父进程的控制台,不等于"不开窗口",真正管这个的是CREATE_NO_WINDOW,
        # 之前漏加了。
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

    if SNIPE_LIVE_MODE:
        # 实盘模式: 只在有空余真实仓位名额+私钥就位时才spawn,而且spawn的就是
        # 真实下单实例。名额被占/没私钥时直接跳过这个候选(不开纸盘实例——VPS
        # 实盘期的journal数据要保持干净,纸盘对照组在云端跑)。
        # 2026-07-31用户拍板: 实盘只做Meteora系(分台统计的+13.6%那一半),
        # 执行通道走Jupiter(实测能路由DBC曲线池)。其余发射台由云端纸盘继续
        # 覆盖攒数据,不在实盘下单。
        if LIVE_DEX_KEYWORD and LIVE_DEX_KEYWORD not in str(dex_id):
            print(f"  [实盘]候选{name}的发射台({dex_id})不在实盘白名单(关键词={LIVE_DEX_KEYWORD}),跳过,留给云端纸盘")
            return False
        if not os.environ.get("WALLET_PRIVATE_KEY"):
            print("  [实盘]*** SNIPE_LIVE_MODE=1但没设WALLET_PRIVATE_KEY,拒绝假装在跑实盘,跳过 ***")
            return False
        if LIVE_MAX_TRADES:
            n_done = count_live_pregrad_trades()
            if n_done >= LIVE_MAX_TRADES:
                print(f"  [实盘]*** 已完成{n_done}/{LIVE_MAX_TRADES}笔实盘测试限额,不再开新仓,等人工检查后放开 ***")
                return False
        n_live = count_live_open_positions(HERE)
        if n_live >= MAX_CONCURRENT_LIVE:
            print(f"  [实盘]真实仓位名额已满({n_live}/{MAX_CONCURRENT_LIVE}),跳过候选 {name}")
            return False
        live_env = dict(os.environ)
        live_env["LIVE_TRADING"] = "1"
        live_env["CONFIRM_LIVE_SNIPE"] = "YES"
        live_env["POS_SIZE_USD"] = LIVE_POS_SIZE_USD
        kwargs["env"] = live_env
        subprocess.Popen([py, str(HERE / "pregrad_scalp_exit.py"), addr, mint, prefix], **kwargs)
        print(f"  [实盘]*** 已部署毕业前抢筹真实交易: {name} ({addr[:10]}...) ${LIVE_POS_SIZE_USD} ***")
        return True

    subprocess.Popen([py, str(HERE / "pregrad_scalp_exit.py"), addr, mint, prefix], **kwargs)
    print(f"  *** 已部署毕业前抢筹纸盘: {name} ({addr[:10]}...) ***")
    return True


def main():
    seen = load_seen()
    now = time.time()
    # 清掉已经跑完(超过部署窗口)的旧记录,不然seen会无限膨胀
    seen = {k: v for k, v in seen.items() if now - v < DEPLOY_WINDOW_SEC}

    rows = fetch_new_pools()
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] 拉到{len(rows)}个最新池子,当前正在跑的纸盘: {len(seen)}个")

    n_deployed = 0
    for row in rows:
        if len(seen) >= MAX_CONCURRENT:
            print(f"  已达并发上限({MAX_CONCURRENT}个在跑),本轮不再新部署,等旧仓位跑完腾位置")
            break
        a = row["attributes"]
        addr = a.get("address")
        if not addr or addr in seen:
            continue
        created = a.get("pool_created_at")
        if not created:
            continue
        try:
            age_min = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 60
        except ValueError:
            continue
        if not matches_pregrad_ramp_signature(a, age_min):
            continue

        rel = row.get("relationships", {})
        base_token_id = rel.get("base_token", {}).get("data", {}).get("id", "")
        mint = base_token_id.split("_")[-1] if "_" in base_token_id else None
        if not mint:
            continue

        # 2026-07-31改: deploy在实盘模式下可能因为名额被占/没私钥而跳过,跳过的
        # 候选不记入seen——名额几分钟后就会腾出来,30秒后的下一轮还能再试这个
        # 候选,不该被240秒的去重窗口白白浪费掉。
        dex_id = rel.get("dex", {}).get("data", {}).get("id", "")
        if deploy(addr, mint, a.get("name"), dex_id):
            seen[addr] = now
            n_deployed += 1

    save_seen(seen)
    print(f"本轮新部署: {n_deployed}个")


if __name__ == "__main__":
    main()
