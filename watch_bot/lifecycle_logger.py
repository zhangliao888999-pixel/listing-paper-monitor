# -*- coding: utf-8 -*-
"""2026-07-29新增: "操盘方拉升生命周期"数据库——用户提出的下一步重点:早期K线
形态和"已经跑了几小时"都不能预判会不会崩(USOS爬了5小时、涨2.2倍之后才在第6
小时单小时归零,过程中跟TNOS/GDWR长得一模一样),真正能不能提前埋伏/提前跑掉,
得靠扎实的数据统计,不是靠猜。

但GMGN的钱包数据(卖出比例、成本)只给"当前快照",没有历史时间序列——没法回头
把TNOS/GDWR过去几小时的卖出比例找出来,只能从现在开始持续记录,攒够跨度之后
才能算出真正的统计规律(比如"卖出比例到多少、真买家信号攒到多少,平均还有多久
崩盘")。这个脚本需要反复运行(建议每15-30分钟跑一次,类似screener.py那样挂
定时任务)才能积累出有意义的时间序列,不是跑一次就有用的。

持久化文件: pump_lifecycle.json —— 每个匹配"操盘方拉升"特征的池子,记录完整的
历史快照序列(价格/流动性/操盘方卖出比例/真买家信号量),直到判定死亡为止。

用法: python lifecycle_logger.py [screener_state_local.json路径]
"""
import json
import os
import re
import subprocess
import sys
import time
import statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from check_coin import GT_BASE, S, GMGN_S, get, check_pool_and_mint


# 2026-07-29晚间调低: 云端迁移后连续3次重启,每次都在10-15分钟左右彻底卡死
# (不是候选变少,是lifecycle_runner_loop自己每10分钟一次的git_sync都停了)。
# 这个上限跟pregrad_scanner.py自己的MAX_CONCURRENT完全独立,互不知道对方——
# 两边加起来最多可能同时有6个pregrad仓位+8个mcap全套监控(每套crash_watch/
# insider_sell_watch/snipe_exit三个子进程),总共二三十个进程一起打同一个
# GT/GMGN限流接口。云端在GitHub Actions共享IP上,能扛住的限流预算大概率比
# 本地独享IP小,原来8这个上限是照着本地实测调的,先往下压到4,配合pregrad那边
# 从6压到3(见pregrad_scanner.py),把系统总并发量砍掉一半以上再看是否还会卡死。
# 2026-07-30新增: VPS没有GitHub Actions那种共享IP限流顾虑,用户想试试把并发调高
# 是不是能提升扫描效率,跟云端(继续用默认值4)直接对比。加环境变量开关,不改
# 默认值,VPS这边单独设MCAP_MAX_CONCURRENT_DEPLOYED=8来测试。
MAX_CONCURRENT_DEPLOYED = int(os.environ.get("MCAP_MAX_CONCURRENT_DEPLOYED", "4"))
from operator_registry import matches_pump_signature, matches_early_signature, matches_origin_mcap_signature, rugcheck_creator

# 2026-07-30新增: 纸盘大框架跑通了,用户要求开始拿真钱小额测试——crash_watch/
# insider_sell_watch这两个纯监控不管live/纸盘都照常部署,唯独snipe_exit.py这一个
# 换成真实下单。SNIPE_LIVE_MODE=1时才切换,默认(不设或者=0)保持原来纸盘行为
# 完全不变,不会因为这次改动影响现在还在跑的纸盘。
# MAX_CONCURRENT_LIVE故意给一个很小的默认值(1)——这是小额测试阶段,不是要
# 让全自动系统同时开好几个真实仓位,并发上限单独算,不跟纸盘的MAX_CONCURRENT_
# DEPLOYED共用同一个计数,这样纸盘该怎么跑还怎么跑。
SNIPE_LIVE_MODE = os.environ.get("SNIPE_LIVE_MODE") == "1"
MAX_CONCURRENT_LIVE = int(os.environ.get("MAX_CONCURRENT_LIVE", "1"))
LIVE_POS_SIZE_USD = os.environ.get("LIVE_POS_SIZE_USD", "5")

HERE = Path(__file__).parent
LIFECYCLE_F = HERE / "pump_lifecycle.json"

DEATH_LIQ_USD = 2000.0        # 流动性跌破这个数,判定已经死透
DEATH_DRAWDOWN_PCT = 0.95     # 或者价格从历史最高点跌了95%以上,也判定死透


def load_lifecycle():
    if LIFECYCLE_F.exists():
        return json.loads(LIFECYCLE_F.read_text(encoding="utf-8"))
    return {}


def save_lifecycle(db):
    LIFECYCLE_F.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")


def get_insider_sell_pct(mint):
    """复用今晚验证过的方法: bundler/transfer_in标记的钱包,合计投入vs合计已卖出。
    排除掉那种total_cost远大于total_volume的脏数据钱包(TNOS的creator钱包就踩过
    这个坑),避免个别异常值把统计拉偏。"""
    d = get(GMGN_S, f"https://gmgn.ai/vas/api/v1/token_traders/sol/{mint}", {"limit": 40})
    rows = (d or {}).get("data", {}).get("list", [])
    insiders = [r for r in rows if any(t in (r.get("maker_token_tags") or [])
               for t in ("bundler", "transfer_in", "creator", "dev_team"))]
    # 只用total_cost在合理范围内的(< $1M,过滤掉刷量刷出来的天文数字)
    clean = [r for r in insiders if 0 < (r.get("total_cost") or 0) < 1_000_000]
    if not clean:
        return None, 0
    total_cost = sum(r.get("total_cost") or 0 for r in clean)
    total_sold = sum((r.get("total_cost") or 0) * (r.get("sell_amount_percentage") or 0) for r in clean)
    return (total_sold / total_cost if total_cost else None), len(clean)


def snapshot_pool(addr, mint):
    attrs, _ = check_pool_and_mint(addr)
    if not attrs:
        return None
    try:
        price = float(attrs.get("base_token_price_usd") or 0)
        liq = float(attrs.get("reserve_in_usd") or 0)
    except (TypeError, ValueError):
        return None
    sell_pct, n_insiders = get_insider_sell_pct(mint)
    return {
        "ts": time.time(), "price": price, "liq": liq,
        "insider_sell_pct": sell_pct, "n_insiders": n_insiders,
        "locked_liq_pct": attrs.get("locked_liquidity_percentage"),
        "h1_chg": (attrs.get("price_change_percentage") or {}).get("h1"),
    }


def update_death_status(entry):
    hist = entry["history"]
    if not hist:
        return
    peak_price = max(h["price"] for h in hist if h.get("price"))
    cur = hist[-1]
    if cur.get("liq", 0) < DEATH_LIQ_USD or (peak_price > 0 and cur.get("price", 0) < peak_price * (1 - DEATH_DRAWDOWN_PCT)):
        if entry["status"] != "dead":
            entry["status"] = "dead"
            entry["died_at"] = cur["ts"]
            entry["peak_price"] = peak_price
            entry["hours_alive"] = (cur["ts"] - entry["first_seen"]) / 3600


def make_prefix(addr):
    """拿池子地址前8位做日志文件名前缀,过滤掉可能有问题的字符。"""
    return re.sub(r"[^A-Za-z0-9]", "", addr)[:8]


# 建仓超过这么久还没看到finish_trade()清掉标记文件,大概率是snipe_exit.py
# 自己崩了(未捕获异常/进程被杀),不是真的还占着仓位——按MAX_MINUTES默认值
# (40分钟)加10分钟缓冲自动判定过期,避免一次崩溃就把唯一的实盘名额永久卡死。
LIVE_MARKER_STALE_SEC = 50 * 60


def live_positions_dir(here):
    """2026-07-31改: 原来用单个.live_position_open文件表示"有没有实盘仓位",
    只能表达占用/空闲两种状态——用户要把并发从1加到6,单文件机制天生做不到
    (6个进程抢同一个文件,永远只有1个能成功)。改成一个目录、每个持仓一个
    以mint命名的标记文件: 数文件个数就是当前持仓数,用O_CREAT|O_EXCL创建
    单个文件仍然是原子抢占。额外好处: 同一个币不会被两条腿同时买两次。"""
    d = here / ".live_positions"
    d.mkdir(exist_ok=True)
    return d


def count_live_open_positions(here):
    """当前真实持仓数 = 标记目录里没过期的文件数。

    2026-07-30背景: 之前用pump_lifecycle.json的live_deployed+status字段判断,
    那两个字段跟"这笔交易平没平仓"没关系(live_deployed永不清零、status是
    池子自己死没死),导致名额从第一笔交易后被永久占死。改用持仓脚本自己
    维护的标记文件——建仓时创建、平仓时删除,这才是真实信号。"""
    d = live_positions_dir(here)
    n = 0
    for f in d.glob("*.json"):
        try:
            age = time.time() - f.stat().st_mtime
        except OSError:
            continue
        if age > LIVE_MARKER_STALE_SEC:
            # 进程崩溃残留的标记: 清掉,不让它永久占着名额
            print(f"  [实盘]标记{f.name}已存在{age/60:.0f}分钟,判定为崩溃残留,清理并释放名额")
            try:
                f.unlink()
            except OSError:
                pass
            continue
        n += 1
    return n


def deploy_full_stack(addr, mint, db):
    """2026-07-29新增: 用户明确要求"纸盘可以完全大胆尝试,筛选出来的币质量都
    很高,全部拿去跑全流程采集数据"——不用我每次手动一个个接,发现新样本就自动
    部署完整三件套(crash_watch取证监控 + insider_sell_watch已知钱包盯防 +
    snipe_exit.py dry-run模拟买卖),全部只读/dry-run,不碰真钱。
    加了并发上限(MAX_CONCURRENT_DEPLOYED),避免同时跑的池子太多把GMGN调用量
    和本机资源拖垮。

    2026-07-30新增: SNIPE_LIVE_MODE=1时,snipe_exit.py这一步换成真实下单
    (crash_watch/insider_sell_watch两个纯监控不受影响,不管live/纸盘都照常
    部署)。真实仓位用独立的MAX_CONCURRENT_LIVE计数,不跟纸盘的并发上限混在
    一起——小额测试阶段就该只开很少的真实仓位,不是让全自动系统一次性铺开。
    没设WALLET_PRIVATE_KEY就直接跳过、不装死回退成纸盘,免得"以为在跑real
    money实际上只是纸盘"这种更危险的误解。"""
    here = Path(__file__).parent
    # 2026-07-29修复: 原来数的是历史上所有stack_deployed=True的币,币死了这个
    # 标记也不清零,导致部署满MAX_CONCURRENT_DEPLOYED次之后永久锁死、再也发现
    # 不了新币(REDO/SOL确认死亡但仍占坑就是实例)。改成只数"还活着"的。
    n_deployed = sum(1 for v in db.values() if v.get("stack_deployed") and v.get("status") != "dead")
    if n_deployed >= MAX_CONCURRENT_DEPLOYED:
        return False

    prefix = make_prefix(addr)
    wallets_f = here / f"{prefix}_insider_wallets.json"
    try:
        d = get(GMGN_S, f"https://gmgn.ai/vas/api/v1/token_traders/sol/{mint}", {"limit": 40})
        rows = (d or {}).get("data", {}).get("list", [])
        insiders = [r["address"] for r in rows if any(t in (r.get("maker_token_tags") or [])
                   for t in ("bundler", "transfer_in", "creator", "dev_team"))]
        wallets_f.write_text(json.dumps(insiders, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        insiders = []

    py = sys.executable
    # 用DETACHED_PROCESS+CREATE_NEW_PROCESS_GROUP,让子进程完全独立于lifecycle_logger
    # 自己这个父进程——lifecycle_runner_loop.py每小时会被我重启一次,不加这个的话
    # 子进程会被一起杀掉,之前部署的监控全部白费。
    # 2026-07-29晚间修复: 用户反馈屏幕上一直弹cmd窗口——DETACHED_PROCESS只是让
    # 子进程脱离父进程的控制台,不等于"不开窗口",真正管这个的是CREATE_NO_WINDOW,
    # 之前漏加了。
    creationflags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    log_devnull = subprocess.DEVNULL

    live_this_one = False
    if SNIPE_LIVE_MODE:
        n_live = count_live_open_positions(here)
        if n_live >= MAX_CONCURRENT_LIVE:
            print(f"  [实盘]已有{n_live}个真实仓位达到上限MAX_CONCURRENT_LIVE={MAX_CONCURRENT_LIVE},这个候选先跳过实盘(纸盘/监控照常)")
        elif not os.environ.get("WALLET_PRIVATE_KEY"):
            print("  [实盘]*** SNIPE_LIVE_MODE=1但没设WALLET_PRIVATE_KEY,拒绝假装在跑实盘——这个候选跳过snipe_exit ***")
        else:
            live_this_one = True

    try:
        subprocess.Popen([py, str(here / "crash_watch.py"), addr, mint, prefix],
                         cwd=str(here), stdout=log_devnull, stderr=log_devnull, creationflags=creationflags)
        if insiders:
            subprocess.Popen([py, str(here / "insider_sell_watch.py"), addr, str(wallets_f), prefix],
                             cwd=str(here), stdout=log_devnull, stderr=log_devnull, creationflags=creationflags)
        if SNIPE_LIVE_MODE:
            if live_this_one:
                live_env = dict(os.environ)
                live_env["LIVE_TRADING"] = "1"
                live_env["CONFIRM_LIVE_SNIPE"] = "YES"
                live_env["POS_SIZE_USD"] = LIVE_POS_SIZE_USD
                subprocess.Popen([py, str(here / "snipe_exit.py"), addr],
                                 cwd=str(here), stdout=log_devnull, stderr=log_devnull,
                                 creationflags=creationflags, env=live_env)
                db[addr]["live_deployed"] = True
                print(f"  [实盘]*** 真实下单已启动: {mint[:10]}... (${LIVE_POS_SIZE_USD}) ***")
            # SNIPE_LIVE_MODE下,没资格实盘的候选也不额外起纸盘snipe_exit——
            # 避免同一个候选同时占纸盘+实盘两份资源,监控(crash_watch/insider)
            # 已经够用于观察这个候选后续走势。
        else:
            subprocess.Popen([py, str(here / "snipe_exit.py"), addr],
                             cwd=str(here), stdout=log_devnull, stderr=log_devnull, creationflags=creationflags)
        return True
    except Exception as e:
        print(f"自动部署失败: {e}")
        return False


def scan_and_log(state_path, max_new_scan=300):
    """两步走: 1) 从追踪池子里找新的匹配"操盘方拉升"特征的池子,加入lifecycle库
    2) 给库里所有还"活着"的池子记一条新快照,顺便判断有没有刚刚死亡"""
    db = load_lifecycle()

    # 第一步: 发现新样本
    if Path(state_path).exists():
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
        tracked = data.get("tracked", {})
        n_new, n_new_early, n_new_mcap = 0, 0, 0
        for i, (addr, w) in enumerate(list(tracked.items())[:max_new_scan]):
            if addr in db:
                continue
            attrs, mint = check_pool_and_mint(addr)
            time.sleep(0.2)
            if not attrs or not mint:
                continue
            age_minutes = (time.time() - w.get("created", time.time())) / 60
            # 2026-07-29新增: 后期版本(要求h6>=50%)对刚发出来1-30分钟的新币用不上,
            # 这些窗口还没积累够数据。用户明确要求不用每秒盯,1-30分钟查一次能发现
            # 大多数案例(今晚TNOS/GDWR/CXMT都是几分钟内bundler买单就动了,不是延迟
            # 半小时才启动),漏掉故意延迟启动的操盘方可以接受。
            # 2026-07-29新增: 用户提出"起点MCAP应该都很高"这个思路,拿DINO/Look!
            # (死币,起点MCAP$2000级别) vs GDWR/TNOS(活下来的,起点MCAP$60万-$860万
            # 级别)验证后区分度极其干净,而且开盘头10分钟就能查,比等15-90分钟的
            # 涨幅百分比快得多,优先判断。
            is_mcap = matches_origin_mcap_signature(attrs, age_minutes)
            is_early = matches_early_signature(attrs, age_minutes)
            is_late = matches_pump_signature(attrs)
            if not (is_mcap or is_early or is_late):
                continue
            found_via = "origin_mcap" if is_mcap else ("early" if is_early else "late")
            db[addr] = {"name": w.get("name"), "mint": mint, "first_seen": time.time(),
                       "status": "alive", "history": [], "found_via": found_via}
            n_new += 1
            if is_mcap:
                n_new_mcap += 1
            elif is_early:
                n_new_early += 1
            # 2026-07-29新增: 用户明确要求发现新样本就自动全流程部署,不用手动一个个接
            deployed = deploy_full_stack(addr, mint, db)
            db[addr]["stack_deployed"] = deployed
            if deployed:
                print(f"  已自动部署监控+模拟交易: {w.get('name')} ({addr[:10]}...)")
        print(f"新发现符合特征的池子: {n_new}个(起点MCAP发现{n_new_mcap}个,早期1-90分钟发现{n_new_early}个)")

    # 第二步: 给所有还活着的池子记一条新快照
    n_updated, n_died_this_round = 0, 0
    for addr, entry in db.items():
        if entry["status"] == "dead":
            continue
        snap = snapshot_pool(addr, entry["mint"])
        time.sleep(0.3)
        if not snap:
            continue
        entry["history"].append(snap)
        was_alive = entry["status"] == "alive"
        update_death_status(entry)
        n_updated += 1
        if was_alive and entry["status"] == "dead":
            n_died_this_round += 1
            print(f"*** {entry['name']} 判定死亡: 存活{entry['hours_alive']:.1f}小时, "
                 f"峰值${entry['peak_price']:.10g} ***")

    save_lifecycle(db)
    n_alive = sum(1 for v in db.values() if v["status"] == "alive")
    n_dead = sum(1 for v in db.values() if v["status"] == "dead")
    print(f"本轮更新{n_updated}个池子的快照,本轮新增死亡{n_died_this_round}个")
    print(f"库存总数: {len(db)}  存活{n_alive}  已死亡{n_dead}")


if __name__ == "__main__":
    state_path = sys.argv[1] if len(sys.argv) > 1 else str(HERE.parent / "screener_state_local.json")
    scan_and_log(state_path)
