# -*- coding: utf-8 -*-
"""2026-07-29白天新增: "狗庄毕业币纸盘模拟买入"——用户明确要求做这条腿,基于目前
的数据现状如实设计,不假装这是个稳赚的思路:

  17个已核实毕业样本里,外部/散户资金17/17全部净亏,REDO/FRANK两案例显示毕业后
  的反应窗口极不稳定(REDO有52秒、FRANK跟砸盘同一秒零窗口)。这条腿本质是纯研究
  性质的数据采集,不是"发现了新的正期望机会"——用极小仓位+最紧的风控参数去
  验证"到底能不能跑赢",而不是假设能跑赢。

触发方式: 由pregrad_scalp_exit.py检测到"连续查不到老池子数据,大概率已经毕业"
时直接调用,不需要独立扫描器——毕业事件本来就是通过老池子失联发现的,复用这个
信号最快最准,不用另起一套发现逻辑。

风控参数专门针对已知的两种模式收紧: FAST_POLL_SEC=2(比pregrad_scalp_exit.py的
3秒更紧,因为这是已知风险更集中的窗口),MAX_HOLD_SEC=90(REDO那次实测的窗口
上限,超过这个说明这次侥幸没被瞬间砸),HARD_STOP_LOSS_PCT=15(比pregrad那边的
20%还紧,因为这里没有"等它再拉一拉"的期待,进来就是纯博反应速度)。

用法: python post_grad_scalp_exit.py <mint> <老池子地址> <文件名前缀>
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
FAST_POLL_SEC = 2
MAX_HOLD_SEC = 90          # REDO案例实测的毕业后存活窗口上限,超过这个算侥幸survived
HARD_STOP_LOSS_PCT = 15    # 比pregrad_scalp_exit.py更紧——这里没有"等它再拉一拉"的
                            # 期待,纯粹是博反应速度,亏相跑得越早损失越小
NEW_POOL_MAX_WAIT_SEC = 60  # 等新池子出现最多等这么久(FRANK案例新池子跟老池子
                             # 消失几乎同时出现,但也可能有延迟,给点容错)

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


def find_new_pool(mint, old_addr):
    d = get(f"{GT_BASE}/networks/solana/tokens/{mint}/pools")
    rows = (d or {}).get("data", [])
    candidates = [row for row in rows if row["attributes"].get("address") != old_addr]
    if not candidates:
        return None
    # 按创建时间倒序,拿最新的那个(理论上应该只有一个,保险起见排一下)
    candidates.sort(key=lambda r: r["attributes"].get("pool_created_at") or "", reverse=True)
    return candidates[0]["attributes"]


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
    log("交易记录推送失败(重试3次),会在下一轮同步时补推")


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
        "n_insiders": None, "found_via": "post_grad_scalp",
        "pos_size_usd": POS_SIZE_USD, "dry_run": True,
        "exit_reason": exit_reason, "exit_price": exit_price, "pnl_pct": pnl_pct,
        "hold_sec": round(hold_sec, 1),
        "prior_entries_on_this_mint": entry_info["prior_entries"],
        "peak_price": entry_info.get("peak_price"),
    }
    write_journal(record)
    pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "未知"
    log(f"台账记录完毕: {exit_reason} pnl={pnl_str} 持仓{hold_sec:.0f}秒")
    git_push_journal()


def main():
    global LOG_F
    if len(sys.argv) < 3:
        print("用法: python post_grad_scalp_exit.py <mint> <老池子地址> <文件名前缀>")
        return
    mint = sys.argv[1]
    old_addr = sys.argv[2]
    prefix = sys.argv[3] if len(sys.argv) > 3 else re.sub(r"[^A-Za-z0-9]", "", mint)[:8]
    LOG_F = HERE / f"{prefix}_post_grad.log"

    log(f"=== 毕业后纸盘模拟买入: mint={mint[:10]}... (老池子{old_addr[:10]}...已消失) ===")

    new_pool = None
    t0 = time.time()
    while time.time() - t0 < NEW_POOL_MAX_WAIT_SEC:
        new_pool = find_new_pool(mint, old_addr)
        if new_pool:
            break
        time.sleep(5)

    if not new_pool:
        log(f"*** 等了{NEW_POOL_MAX_WAIT_SEC}秒仍找不到新池子(可能确实已死,不是毕业),放弃 ***")
        return

    addr = new_pool.get("address")
    log(f"找到新池子: {addr[:10]}...  created_at={new_pool.get('pool_created_at')}")

    try:
        entry_price = float(new_pool.get("base_token_price_usd") or 0)
    except (TypeError, ValueError):
        entry_price = None
    if not entry_price:
        log("拿不到新池子入场价,放弃")
        return

    prior_entries = count_prior_entries(mint)
    entry_info = {
        "name": new_pool.get("name"), "mint": mint, "addr": addr, "entry_ts": time.time(),
        "coin_age_min_at_entry": 0.0, "entry_price": entry_price,
        "entry_liq_usd": new_pool.get("reserve_in_usd"), "entry_locked_liq_pct": new_pool.get("locked_liquidity_percentage"),
        "entry_fdv_usd": new_pool.get("fdv_usd"), "prior_entries": prior_entries, "peak_price": entry_price,
    }
    log(f"[纸盘] 建仓 entry_price={entry_price}  仓位=${POS_SIZE_USD:.2f}  硬止损={HARD_STOP_LOSS_PCT}%  硬超时={MAX_HOLD_SEC}秒")

    peak_price = entry_price
    n_fail_in_a_row = 0
    deadline = entry_info["entry_ts"] + MAX_HOLD_SEC
    while time.time() < deadline:
        time.sleep(FAST_POLL_SEC)
        snap = get_pool_snapshot(addr)
        if not snap or snap.get("price") is None:
            n_fail_in_a_row += 1
            if n_fail_in_a_row >= 3:
                log("*** 连续查不到新池子数据,判定为流动性瞬间抽干,按最后已知价结算 ***")
                entry_info["peak_price"] = peak_price
                finish_trade(entry_info, "POOL_VANISHED_AGAIN", peak_price)
                return
            continue
        n_fail_in_a_row = 0
        price = snap["price"]
        if price > peak_price:
            peak_price = price

        if snap.get("liq") is not None and snap["liq"] < 500:
            log(f"*** 流动性只剩${snap['liq']:,.0f},判定已死透,卖出离场 ***")
            entry_info["peak_price"] = peak_price
            finish_trade(entry_info, "LIQ_DEAD", price)
            return

        loss_from_entry = (1 - price / entry_price) * 100
        if loss_from_entry >= HARD_STOP_LOSS_PCT:
            log(f"*** 硬止损触发: 跌破入场价{loss_from_entry:.1f}%,立刻离场 ***")
            entry_info["peak_price"] = peak_price
            finish_trade(entry_info, "HARD_STOP_LOSS", price)
            return

    log(f"=== {MAX_HOLD_SEC}秒窗口挺过来了,不管当前盈亏离场(不贪心,别的机器人这时候可能也在跑) ===")
    final_snap = get_pool_snapshot(addr)
    final_price = final_snap.get("price") if final_snap else peak_price
    entry_info["peak_price"] = peak_price
    finish_trade(entry_info, "SURVIVED_WINDOW_TIMEOUT", final_price)


if __name__ == "__main__":
    main()
