# -*- coding: utf-8 -*-
"""2026-07-29新增: "毕业前抢筹,毕业前卖回curve"打法的纸盘执行器——跟snipe_exit.py
是两套完全不同的退出逻辑,不能共用:

  snipe_exit.py 的策略是"买入已毕业/接近毕业的池子,等真买家/操盘方卖出信号才跑",
  会持仓跨越毕业这个瞬间,吃的是毕业后新池子里的行情。

  这个脚本反过来: 只在老池子(bonding curve,毕业前)里买卖,全程不碰新池子。
  用户的原话是"毕业后1秒马上跑",但REDO/FRANK两个案例分析发现毕业瞬间到底有没有
  反应窗口是抛硬币(REDO有52秒窗口,FRANK毕业跟砸盘是同一秒),跟操盘方自己的
  砸盘交易抢同一个身位赢面很小。所以改成更稳的版本: 只吃bonding curve内部本身
  的涨幅(REDO这段实测涨了993%,比毕业瞬间那47%跳涨大得多),用移动止盈+硬止损+
  硬超时控制风险,一旦观察到"这个池子好像要没了"(有可能是要毕业了,也可能是真的
  死了),不管是哪种都不恋战,直接按当前价卖出离场——不去赌毕业后的行情。

跟snipe_exit.py一样是纯DRY-RUN、写同一份journal.jsonl(found_via标记为
pregrad_ramp,方便以后跟其他策略横向对比)。这个阶段的代币在Jupiter上大概率还
查不到报价(bonding curve没接入聚合器路由,这也是"毕业后大家才能买"这件事本身
的技术原因),所以不像snipe_exit.py那样调用Jupiter模拟买卖行情,直接用
GeckoTerminal自己报的价格做纸面结算——反正只是纸盘,不需要真实可执行的报价。

用法: python pregrad_scalp_exit.py <池子地址> <mint> <文件前缀>

2026-07-29白天补充: 用户看完统计后明确要求补一条"狗庄毕业币纸盘模拟买入"——
既然17个已核实毕业样本里外部资金17/17全部净亏,这条腿定位是纯数据采集/验证,
不是"找到了新的赚钱思路"。检测到大概率已毕业(连续查不到老池子)时,直接拉起
post_grad_scalp_exit.py接手去查新池子、用更紧的风控参数单独跑一笔纸盘,数据
写进同一份journal.jsonl(found_via=post_grad_scalp),不阻塞这个脚本自己收尾退出。
"""
import json
import re
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path

import requests

HERE = Path(__file__).parent
JOURNAL_F = HERE / "journal.jsonl"

GT_BASE = "https://api.geckoterminal.com/api/v2"
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                  "Accept": "application/json;version=20230302"})

POS_SIZE_USD = 5.0
# 2026-07-29白天调: 419笔实盘回看后调整。原POLL_SEC=5秒时,HARD_STOP_LOSS中位数
# 超调达13.3个百分点(45%的止损单超调超过20点),说明崩盘经常比轮询间隔更快。
# 缩到3秒不能根治(链上瞬间归零的情况5秒3秒都来不及),但能缩小平均超调幅度。
POLL_SEC = 3
FAST_RECHECK_SEC = 1.5    # 新增: 一旦观察到价格开始从高点回落,不等到下一个完整
                           # POLL_SEC,立刻用更短间隔再看一眼——只在"看起来要跌"的
                           # 时候加密,不是全程都用最快频率,省着点用有限的限流额度
MAX_HOLD_SEC = 180        # 硬超时3分钟——REDO/FRANK从创世到毕业都在这个量级内
PLATEAU_CHECK_SEC = 90    # 新增: 419笔数据显示HARD_TIMEOUT一半是中位数~0%的死账户,
                           # 白白占了3分钟仓位。90秒时如果价格基本没动(在入场价±5%内),
                           # 提前离场腾仓位,不硬等满3分钟
PLATEAU_BAND_PCT = 5
# 原20%/35%的止损止盈线,实测中位数超调都相当可观(尤其HARD_STOP_LOSS超调13.3pp,
# 45%的单子超调>20pp)——既然结算价本来就会比设定线更差,把设定线本身收紧,
# 让"更差的结算价"落在更能接受的范围,而不是继续放任-48%中位数这种结果。
TRAIL_STOP_PCT = 15       # 从最高点回撤这么多就跑,不贪图猜中最高点(原20)
HARD_STOP_LOSS_PCT = 20   # 从没盈利过、直接跌破入场价这么多,说明这次没被拉起来,止损离场(原35)

LOG_F = None


def log(msg):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        pass
    with LOG_F.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1.5 * (i + 1))
    return None


def extract_addr(arg):
    m = re.search(r"/pools/([A-Za-z0-9]+)", arg)
    return m.group(1) if m else arg


def check_pool_and_mint(addr):
    d = get(f"{GT_BASE}/networks/solana/pools/{addr}", {"include": "base_token"})
    if not d:
        return None, None
    attrs = d["data"]["attributes"]
    mint = None
    for inc in d.get("included", []):
        if inc.get("type") == "token":
            mint = inc["attributes"].get("address")
    return attrs, mint


def get_pool_snapshot(addr):
    d = get(f"{GT_BASE}/networks/solana/pools/{addr}")
    if not d:
        return None
    a = d.get("data", {}).get("attributes", {})
    try:
        price = float(a.get("base_token_price_usd"))
    except (TypeError, ValueError):
        price = None
    try:
        liq = float(a.get("reserve_in_usd"))
    except (TypeError, ValueError):
        liq = None
    return {"price": price, "liq": liq}


def make_prefix(addr):
    return re.sub(r"[^A-Za-z0-9]", "", addr)[:8]


def handoff_to_post_grad(old_addr, mint, name):
    """检测到大概率已毕业时,拉起独立的post_grad_scalp_exit.py去查新池子、单独
    跑一笔纸盘——用subprocess.Popen不等待,这个脚本自己该收尾就收尾,不因为
    handoff卡住。"""
    try:
        py = sys.executable
        prefix = re.sub(r"[^A-Za-z0-9]", "", mint)[:8]
        kwargs = {"cwd": str(HERE), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.CREATE_NO_WINDOW)
        subprocess.Popen([py, str(HERE / "post_grad_scalp_exit.py"), mint, old_addr, prefix], **kwargs)
        log(f"已交接给post_grad_scalp_exit.py去查{name}的新池子")
    except Exception as e:
        log(f"交接post_grad_scalp_exit.py失败: {e}")


def count_prior_entries(mint):
    if not JOURNAL_F.exists():
        return 0
    n = 0
    with JOURNAL_F.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("mint") == mint:
                n += 1
    return n


def write_journal(record):
    record["written_at"] = int(time.time())
    with JOURNAL_F.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def git_push_journal():
    repo_root = HERE.parent
    def run(cmd):
        return subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)
    run(["git", "add", "watch_bot/journal.jsonl"])
    commit = run(["git", "commit", "-m", f"trade completed: {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"])
    if commit.returncode != 0 and "nothing to commit" in (commit.stdout + commit.stderr):
        return
    for _ in range(3):
        push = run(["git", "push"])
        if push.returncode == 0:
            log("交易记录已立刻推送到GitHub,页面刷新即可见")
            return
        run(["git", "pull", "--rebase", "origin", "master"])
        time.sleep(3)
    log("交易记录推送失败(重试3次),会在下一轮lifecycle循环时补推")


def finish_trade(entry_info, exit_reason, exit_price):
    entry_price = entry_info["entry_price"]
    pnl_pct = (exit_price / entry_price - 1) * 100 if (entry_price and exit_price) else None
    hold_sec = time.time() - entry_info["entry_ts"]
    record = {
        "name": entry_info["name"], "mint": entry_info["mint"], "addr": entry_info["addr"],
        "entry_ts": entry_info["entry_ts"], "entry_ts_str": dt.datetime.fromtimestamp(entry_info["entry_ts"]).strftime("%Y-%m-%d %H:%M:%S"),
        "coin_age_min_at_entry": entry_info["coin_age_min_at_entry"],
        "entry_price": entry_price, "entry_liq_usd": entry_info["entry_liq_usd"],
        "entry_locked_liq_pct": entry_info["entry_locked_liq_pct"], "entry_fdv_usd": entry_info["entry_fdv_usd"],
        "n_insiders": None, "found_via": "pregrad_ramp",
        "pos_size_usd": POS_SIZE_USD, "dry_run": True,
        "exit_reason": exit_reason, "exit_price": exit_price, "pnl_pct": pnl_pct,
        "hold_sec": round(hold_sec, 1),
        "prior_entries_on_this_mint": entry_info["prior_entries"],
        "peak_price": entry_info.get("peak_price"),
    }
    write_journal(record)
    pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "未知(拿不到退出价)"
    log(f"台账记录完毕: {exit_reason} pnl={pnl_str} 持仓{hold_sec:.0f}秒 "
       f"(这个币之前进过{entry_info['prior_entries']}次仓)")
    git_push_journal()


def main():
    global LOG_F
    if len(sys.argv) < 3:
        print("用法: python pregrad_scalp_exit.py <池子地址> <mint> <文件前缀>")
        return
    addr = extract_addr(sys.argv[1])
    mint = sys.argv[2]
    prefix = sys.argv[3] if len(sys.argv) > 3 else make_prefix(addr)
    LOG_F = HERE / f"{prefix}_pregrad_scalp.log"

    attrs, mint_check = check_pool_and_mint(addr)
    if not attrs:
        log("查不到这个池子,地址可能不对")
        return
    if mint_check:
        mint = mint_check

    log(f"=== 毕业前抢筹纸盘: {attrs.get('name')} ({addr[:10]}...) ===")
    log(f"仓位=${POS_SIZE_USD:.2f}(纸盘)  移动止盈回撤={TRAIL_STOP_PCT}%  硬止损={HARD_STOP_LOSS_PCT}%  硬超时={MAX_HOLD_SEC}秒")

    prior_entries = count_prior_entries(mint)
    if prior_entries:
        log(f"注意: 这个币之前已经进过{prior_entries}次仓了,这次是反复进入")

    try:
        entry_price = float(attrs.get("base_token_price_usd") or 0)
    except (TypeError, ValueError):
        entry_price = None
    if not entry_price:
        log("拿不到入场价,放弃这次纸盘")
        return

    try:
        created = dt.datetime.fromisoformat(attrs["pool_created_at"].replace("Z", "+00:00"))
        coin_age_min = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 60
    except (KeyError, ValueError, TypeError):
        coin_age_min = None

    entry_info = {
        "name": attrs.get("name"), "mint": mint, "addr": addr, "entry_ts": time.time(),
        "coin_age_min_at_entry": coin_age_min, "entry_price": entry_price,
        "entry_liq_usd": attrs.get("reserve_in_usd"), "entry_locked_liq_pct": attrs.get("locked_liquidity_percentage"),
        "entry_fdv_usd": attrs.get("fdv_usd"), "prior_entries": prior_entries, "peak_price": entry_price,
    }
    log(f"[纸盘] 建仓 entry_price={entry_price}  (池子年龄约{coin_age_min:.1f}分钟)" if coin_age_min is not None
        else f"[纸盘] 建仓 entry_price={entry_price}")

    peak_price = entry_price
    n_fail_in_a_row = 0
    was_declining = False   # 上一次观察到价格低于峰值,下一次用更短间隔加密复查
    plateau_checked = False
    entry_ts = entry_info["entry_ts"]
    deadline = entry_ts + MAX_HOLD_SEC
    while time.time() < deadline:
        time.sleep(FAST_RECHECK_SEC if was_declining else POLL_SEC)
        snap = get_pool_snapshot(addr)
        if not snap or snap.get("price") is None:
            n_fail_in_a_row += 1
            # 2026-07-29: 查不到池子/价格,最可能的原因就是"已经毕业迁移到新池子了"
            # (老的bonding curve地址在GT上要么消失要么不再更新),其次才是纯API抖动。
            # 连续3次查不到,判定为"没跑赢毕业,被留在老池子里"——用最后
            # 已知价格结算,老实记录这次没跑赢,不装作若无其事。
            if n_fail_in_a_row >= 3:
                log("*** 连续查不到池子数据,大概率已经毕业迁移,没能抢在毕业前跑掉 ***")
                entry_info["peak_price"] = peak_price
                finish_trade(entry_info, "MISSED_EXIT_LIKELY_GRADUATED", peak_price)
                handoff_to_post_grad(addr, mint, attrs.get("name"))
                return
            continue
        n_fail_in_a_row = 0
        price = snap["price"]
        was_declining = price < peak_price
        if price > peak_price:
            peak_price = price

        if snap.get("liq") is not None and snap["liq"] < 500:
            log(f"*** 流动性只剩${snap['liq']:,.0f},判定已死透,卖出离场 ***")
            entry_info["peak_price"] = peak_price
            finish_trade(entry_info, "LIQ_DEAD", price)
            return

        drawdown_from_peak = (1 - price / peak_price) * 100 if peak_price else 0
        if peak_price > entry_price and drawdown_from_peak >= TRAIL_STOP_PCT:
            log(f"*** 移动止盈触发: 最高价{peak_price:.10g}回撤{drawdown_from_peak:.1f}%,卖出离场 ***")
            entry_info["peak_price"] = peak_price
            finish_trade(entry_info, "TRAILING_STOP", price)
            return

        loss_from_entry = (1 - price / entry_price) * 100
        if loss_from_entry >= HARD_STOP_LOSS_PCT:
            log(f"*** 硬止损触发: 跌破入场价{loss_from_entry:.1f}%,没被拉起来,止损离场 ***")
            entry_info["peak_price"] = peak_price
            finish_trade(entry_info, "HARD_STOP_LOSS", price)
            return

        # 2026-07-29白天新增: 419笔数据显示HARD_TIMEOUT一半是中位数~0%的死账户,
        # 白白占满3分钟仓位没有任何意义。90秒时价格基本没挪窝(入场价±5%内),提前
        # 离场腾仓位,不硬等满3分钟去赌一个已经看起来没有动能的池子。
        if not plateau_checked and time.time() - entry_ts >= PLATEAU_CHECK_SEC:
            plateau_checked = True
            move_pct = abs(price / entry_price - 1) * 100
            if move_pct < PLATEAU_BAND_PCT:
                log(f"*** {PLATEAU_CHECK_SEC:.0f}秒时价格仍在入场价±{PLATEAU_BAND_PCT}%内(现变动{move_pct:.1f}%),判定没动能,提前离场腾仓位 ***")
                entry_info["peak_price"] = peak_price
                finish_trade(entry_info, "PLATEAU_NO_MOMENTUM", price)
                return

    log(f"=== 硬超时{MAX_HOLD_SEC}秒到,不管当前盈亏直接离场(不恋战,不赌毕业后的行情) ===")
    final_snap = get_pool_snapshot(addr)
    final_price = final_snap.get("price") if final_snap else peak_price
    entry_info["peak_price"] = peak_price
    finish_trade(entry_info, "HARD_TIMEOUT", final_price)


if __name__ == "__main__":
    main()
