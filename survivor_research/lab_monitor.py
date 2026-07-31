# -*- coding: utf-8 -*-
"""狗庄实验室 - 实时状态面板。2026-07-31建。

配合 lab_monitor.ps1 在VPS上开一个自动刷新的窗口,一眼看清:
  - 守望进程还活着吗? 卡住了没有?(靠心跳文件的时间戳判断,光看进程在不在
    不够——进程可能活着但卡在某个池子的RPC上不动)
  - 现在盯着哪些狗庄,他们各自沉了多少钱、钓到多少鱼、危险度多少
  - 纸盘持仓的实时盈亏和距离离场线还有多远
  - API额度烧到哪了

排版上踩过的三个坑(第一版在VPS上整个糊掉了):
  1. Windows经典控制台默认不解析ANSI转义,颜色码会原样打出来成"[96m",
     还占着宽度把列全冲乱。所以先尝试开VT模式,开不了就不上色。
  2. 中文在终端里占2列,但 len() 只算1,按 len 补齐必然错位。要用
     east_asian_width 算真实显示宽度。
  3. 有的币名里藏着 U+202E(从右到左覆盖)这类双向控制符,会把整行文字
     方向翻转 —— 面板上那一行会变成倒着的乱码。必须先剔除。
"""
import json
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import lab_db as db          # noqa: E402
import lab_forensics as fx   # noqa: E402


def _enable_vt():
    """Windows经典控制台要显式打开虚拟终端处理,否则ANSI码原样输出。"""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


COLOR = _enable_vt()
_C = {"r": "\033[91m", "g": "\033[92m", "y": "\033[93m", "c": "\033[96m",
      "w": "\033[97m", "d": "\033[90m", "0": "\033[0m", "b": "\033[1m"}

# 双向控制符和零宽字符: 币名里塞这些是常见的伪装手法,会把整行显示搞乱
_BAD = {0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C,
        0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF}


def clean(t):
    """剔除双向/零宽控制符,并把不可打印字符换成点。"""
    if t is None:
        return ""
    out = []
    for ch in str(t):
        o = ord(ch)
        if o in _BAD:
            continue
        cat = unicodedata.category(ch)
        out.append("." if cat.startswith("C") else ch)
    return "".join(out)


def w(t):
    """终端显示宽度: 中日韩全角字符占2列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in t)


def fit(t, n, right=False):
    """按显示宽度截断并补齐到 n 列。上色必须在补齐**之后**,否则转义序列
    会被算进长度里(第一版就是这么错位的)。"""
    t = clean(t)
    cur = ""
    for ch in t:
        if w(cur) + w(ch) > n:
            break
        cur += ch
    pad = " " * max(n - w(cur), 0)
    return pad + cur if right else cur + pad


def col(t, c):
    return f"{_C[c]}{t}{_C['0']}" if COLOR else t


def ago(ts):
    if not ts:
        return "从未"
    s = time.time() - ts
    if s < 90:
        return f"{s:.0f}秒前"
    if s < 5400:
        return f"{s/60:.0f}分钟前"
    return f"{s/3600:.1f}小时前"


def money(v):
    return "$" + format(v or 0, ",.0f")


def main():
    line = "=" * 76
    print(col(line, "c"))
    print(col(fit(f"  狗庄研究实验室   刷新: {datetime.now():%Y-%m-%d %H:%M:%S}", 76), "b"))
    print(col(line, "c"))

    # ---- 心跳 ----
    hb = {}
    try:
        hb = json.loads((HERE / ".lab_heartbeat.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    age = time.time() - hb.get("ts", 0) if hb else 9e9
    if not hb:
        state = col("没有心跳文件 —— 守望进程从未启动过", "r")
    elif age < 400:
        state = col(f"运行中 (PID {hb.get('pid')})", "g")
    elif age < 900:
        state = col(f"心跳偏慢 {ago(hb.get('ts'))} —— 可能卡在某个池子", "y")
    else:
        state = col(f"心跳停了 {ago(hb.get('ts'))} —— 进程可能已死", "r")
    print(f"\n{col('守望进程', 'w')}: {state}")
    if hb:
        if hb.get("stage") == "扫描中":
            print(f"  当前: 扫描第 {hb.get('i')}/{hb.get('n')} 个  ->  "
                  f"{col(clean(hb.get('pool')), 'c')}")
        else:
            print(f"  当前: {clean(hb.get('stage'))}   观察 {hb.get('watching')} 个   "
                  f"持仓 {hb.get('open')}   已平 {hb.get('done')}")
        print(f"  心跳: {ago(hb.get('ts'))}      Helius额度: {hb.get('helius', '?')}")

    c = db.conn()

    # ---- 观察名单 ----
    print(f"\n{col('--- 正在盯的狗庄 ---', 'w')}")
    rows = c.execute("""
        SELECT s.*, w.name AS wname FROM snapshots s
        JOIN (SELECT pool, MAX(ts) m FROM snapshots GROUP BY pool) x
          ON s.pool = x.pool AND s.ts = x.m
        JOIN watchlist w ON w.pool = s.pool AND w.dropped_at IS NULL
        ORDER BY s.op_cost_usd DESC LIMIT 15""").fetchall()
    if not rows:
        print(col("  (还没有快照)", "d"))
    else:
        hdr = ("  " + fit("币", 18) + fit("交易", 6, True) + fit("钱包", 6, True)
               + fit("集中", 6, True) + fit("狗庄成本", 12, True)
               + fit("鱼的钱", 11, True) + fit("危险度", 9, True) + "  更新")
        print(col(hdr, "d"))
        for r in rows:
            dg = r["danger"] or 0
            dc = "r" if dg >= 0.30 else ("y" if dg >= 0.15 else "g")
            print("  " + fit(r["wname"] or r["pool"][:10], 18)
                  + fit(str(r["n_tx"]), 6, True)
                  + fit(str(r["n_wallet"]), 6, True)
                  + fit(f"{r['top_share']:.0%}", 6, True)
                  + fit(money(r["op_cost_usd"]), 12, True)
                  + fit(money(r["fish_in_usd"]), 11, True)
                  + col(fit(f"{dg:.2f}", 9, True), dc)
                  + "  " + ago(r["ts"]))

    # ---- 纸盘 ----
    print(f"\n{col('--- 纸盘持仓 ---', 'w')}")
    op = c.execute("SELECT * FROM paper_trades WHERE exit_ts IS NULL "
                   "ORDER BY entry_ts").fetchall()
    if not op:
        print(col("  (当前无持仓)", "d"))
    for r in op:
        snap = c.execute("SELECT * FROM snapshots WHERE pool=? ORDER BY ts DESC LIMIT 1",
                         (r["pool"],)).fetchone()
        px = snap["price"] if snap else None
        pnl = ((px / r["entry_price"] - 1) * 100 if px and r["entry_price"] else None)
        dg = (snap["danger"] if snap else 0) or 0
        print("  " + col(fit(r["name"] or r["pool"][:10], 20), "c")
              + f"进场时狗庄成本 {money(r['entry_op_cost']):>9}   持有 {ago(r['entry_ts'])}")
        pt = f"{pnl:+.1f}%" if pnl is not None else "?"
        print("    浮动盈亏 " + col(fit(pt, 9, True), "g" if (pnl or 0) >= 0 else "r")
              + f"   危险度 {dg:.2f}/0.30"
              + ("   " + col("接近离场线!", "r") if dg >= 0.2 else ""))

    done = c.execute("SELECT * FROM paper_trades WHERE exit_ts IS NOT NULL "
                     "ORDER BY exit_ts DESC LIMIT 6").fetchall()
    if done:
        print(f"\n{col('--- 最近平仓 ---', 'w')}")
        for r in done:
            raw = f"(名义{r['pnl_pct_raw']:+.1f}%)" if r["pnl_pct_raw"] is not None else ""
            print("  " + fit(r["name"] or r["pool"][:10], 18)
                  + col(fit(f"{r['pnl_pct'] or 0:+.1f}%", 9, True),
                        "g" if (r["pnl_pct"] or 0) >= 0 else "r")
                  + "  " + fit(raw, 16) + fit(r["exit_reason"] or "", 22)
                  + ago(r["exit_ts"]))
        pnls = [x["pnl_pct"] for x in c.execute(
            "SELECT pnl_pct FROM paper_trades WHERE exit_ts IS NOT NULL "
            "AND pnl_pct IS NOT NULL")]
        if pnls:
            win = sum(1 for x in pnls if x > 0)
            print(f"\n  累计 {len(pnls)} 笔   胜率 {win/len(pnls):.0%}   "
                  f"均值 {sum(pnls)/len(pnls):+.1f}%   "
                  f"最好 {max(pnls):+.1f}%   最差 {min(pnls):+.1f}%")

    ev = c.execute("SELECT reason, COUNT(*) n FROM watchlist "
                   "WHERE dropped_at IS NOT NULL GROUP BY reason "
                   "ORDER BY n DESC LIMIT 5").fetchall()
    if ev:
        tot = c.execute("SELECT COUNT(*) n FROM watchlist "
                        "WHERE dropped_at IS NOT NULL").fetchone()["n"]
        print(f"\n{col(f'--- 已腾位 {tot} 个 ---', 'w')}")
        for r in ev:
            print(col("  " + fit(str(r["n"]), 5, True) + "  " + clean(r["reason"]), "d"))

    print(col("\n" + "-" * 76, "d"))
    print(col("  每15秒自动刷新。关掉这个窗口不影响后台采集。", "d"))


if __name__ == "__main__":
    main()
