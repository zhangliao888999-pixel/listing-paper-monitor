# -*- coding: utf-8 -*-
"""狗庄研究实验室 - 实时守望。2026-07-31建。

用户设计的策略(反过来吃狗庄),这个进程负责把它跑起来并采数据验证:

  1. 盯刚发出来 2-30 分钟的新币
  2. 认出"钓鱼盘"形态: 交易高度集中在一个钱包、几乎没有外部买家
  3. 持续跟踪这个狗庄的**沉没成本**
  4. 当他沉没成本 >= 门槛(比如$50),纸盘买入一个远小于它的仓位(比如$5)
     —— 依据: 他为了吃我们$5而砸盘,等于把自己几小时的撒饵和几百刀成本
        全部作废,亏本的买卖没人做
  5. 他继续撒饵抬价,我们不动
  6. 当**鱼的钱超过他的成本**(危险度>=1),他随时可以盈利离场 —— 立刻卖出
  7. 全程落库,最后用数据检验这套猜测成不成立

性能上的关键设计: 每个池子的交易明细在内存里增量累积,每轮只拉**新增**的
签名。否则盯20个池子、每个几百笔、每5分钟全量重拉一次,RPC直接打爆(今天
已经踩过一次: 单节点被打爆后 getTransaction 直接返回空)。

用法:
  python lab_watch.py                持续守望
  python lab_watch.py --add <pool>   手工把某个池子加进观察名单
"""
import calendar
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cg_client as cg
import lab_db as db
import lab_dump as ld
import lab_forensics as fx

HERE = Path(__file__).parent

# ---- 策略参数(先用保守值,等数据跑出来再校准)----
MIN_AGE_MIN = 2         # 太新的币还看不出形态
MAX_AGE_MIN = 45        # 太老的错过了最佳观察期
MIN_OP_COST = 50.0      # 狗庄沉没成本达到这个数才考虑进场
POS_USD = 5.0           # 纸盘仓位
MAX_POS_FRAC = 0.05     # 仓位不得超过他沉没成本的这个比例
MIN_TOP_SHARE = 0.60    # 交易集中度低于这个说明不是单人钓鱼盘
MAX_FISH_AT_ENTRY = 20.0  # 进场时外部资金必须还很少
# 危险度离场线。**不是1.0**——USOH的全量数据显示他在危险度0.41就收网了:
#   狗庄成本约660 SOL($49,500),鱼的钱约270 SOL($20,275),比值0.41。
# 原因是他的"成本"其实大部分不是沉没的——池子里的SOL一砸就连本带利回来了,
# 真正沉没的只有手续费。所以他远在"鱼的钱够本"之前就有得赚。
# 0.30 是根据这一个样本定的保守值,等实验室采够 fish_in_at_dump 分布再校准。
DANGER_EXIT = 0.30
STOP_PCT = -35.0        # 止损
MAX_HOLD_MIN = 240      # 最长持有
MAX_POOL_SIGS = 2500    # 单个池子的交易上限。超过这个量的池子: (a) 一轮
                        # 解析不完(限速7笔/秒,3000笔要7分钟,循环才3分钟),
                        # 永远追不上,CPU持续满载; (b) 本来就不是单人钓鱼盘,
                        # 狗庄盘是几百笔的冷清盘。直接腾位。
MAX_NEW_PER_ROUND = 400 # 单轮单池最多解析多少笔新交易,防止突发把循环撑爆
MAX_WATCH = 40          # 同时观察上限。Helius免费档约10请求/秒是硬顶,
                        # 但增量刷新只拉新增交易,腾掉非狗庄盘后40个撑得住
EVICT_IDLE_MIN = 90     # 盘死了多久就腾位
SCAN_GAP = 180          # 每轮间隔(秒)
DISCOVER_GAP = 600      # 多久扫一次新币

def depth_adjust(value_usd, reserve_usd):
    """按池子真实深度折算成交价,恒定乘积做市商的标准结果。

    卖出一笔边际市值 V 的仓位,池内计价币储备 R,实得 = V·R/(R+V)。
    买入同理要多付。V 远小于 R 时几乎无损耗,V 接近 R 时腰斩。

    这一步不能省。上一轮纸盘就是败在"按最后已知价结算",给流动性已经归零的
    币算出 +18.59% 的平均收益。FFILL 那笔记 +57.9%,但它的池子只剩几十刀,
    $5 砸进去根本卖不到那个价。
    """
    if not reserve_usd or reserve_usd <= 0:
        return 0.0
    return value_usd * reserve_usd / (reserve_usd + value_usd)


EXTRA_DDL = """
CREATE TABLE IF NOT EXISTS snapshots (
  pool TEXT, ts INTEGER, age_min REAL, n_tx INTEGER, n_wallet INTEGER,
  top_share REAL, reserve_usd REAL, op_cost_usd REAL, fish_in_usd REAL,
  danger REAL, price REAL, PRIMARY KEY (pool, ts)
);
CREATE TABLE IF NOT EXISTS paper_trades (
  pool TEXT PRIMARY KEY, name TEXT,
  entry_ts INTEGER, entry_price REAL, entry_op_cost REAL, size_usd REAL,
  exit_ts INTEGER, exit_price REAL, exit_reason TEXT, pnl_pct REAL,
  pnl_pct_raw REAL, exit_reserve REAL, entry_reserve REAL,
  peak_price REAL, max_danger REAL
);
CREATE INDEX IF NOT EXISTS ix_snap_pool ON snapshots(pool);
CREATE TABLE IF NOT EXISTS watchlist (
  pool TEXT PRIMARY KEY, name TEXT, added_at INTEGER, dropped_at INTEGER, reason TEXT
);
"""

_lock = threading.Lock()


# 输出统一走UTF-8。有的币故意在名字里塞 U+202E(从右到左覆盖)这类字符,
# 默认GBK控制台直接抛 UnicodeEncodeError 把整个池子的处理打断。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def log(*a):
    with _lock:
        print(f"[{time.strftime('%m-%d %H:%M:%S')}]", *a, flush=True)


class Pool:
    """一个被观察的池子。交易明细增量累积,不重复拉。"""

    def __init__(self, addr, name=""):
        self.addr = addr
        self.name = name
        self.txs = []
        self.seen = set()
        self.dead = False
        self.too_big = 0
        self.failed = 0

    def refresh(self):
        """增量拉取新交易。返回新解析的笔数,-1 表示池子太大应当腾位。"""
        sigs = fx.get_signatures(self.addr, cap=MAX_POOL_SIGS + 100)
        if len(sigs) > MAX_POOL_SIGS:
            self.too_big = len(sigs)
            return -1
        new = [s for s in sigs
               if not s["err"] and s.get("ts") and s["sig"] not in self.seen]
        if not new:
            return 0
        new = new[:MAX_NEW_PER_ROUND]
        with ThreadPoolExecutor(max_workers=10) as ex:
            for s, t in zip(new, ex.map(fx.parse_tx, new)):
                # 无论解析成不成功都记进 seen。原本只在成功时记,导致拉不到的
                # 交易每一轮都重新请求一遍,RPC和CPU双重浪费,而且永远追不上。
                self.seen.add(s["sig"])
                if t:
                    self.txs.append(t)
                else:
                    self.failed += 1
        self.txs.sort(key=lambda x: x["ts"])
        return len(new)

    def snapshot(self):
        # expected用已知签名数,覆盖率不够宁可这轮不出快照
        m, _ = fx.analyze(self.addr, self.txs,
                          expected=len(self.seen) - self.failed)
        if not m:
            return None
        op = m["op_cost_usd"]
        fish = m["fish_in_usd"]
        m["danger"] = (fish / op) if op > 1 else (999.0 if fish > 1 else 0.0)
        return m


def price_of(pool):
    d = cg.get(f"networks/solana/pools/{pool}")
    try:
        return float(d["data"]["attributes"]["base_token_price_usd"])
    except (TypeError, KeyError, ValueError):
        return None


def discover_new():
    """找刚发出来的币。GT的new_pools按时间倒序,取年龄在窗口内的。"""
    out = []
    for page in (1, 2, 3):
        d = cg.get("networks/solana/new_pools", {"page": page})
        for r in (d or {}).get("data", []):
            a = r.get("attributes", {})
            addr, created = a.get("address"), a.get("pool_created_at")
            if not addr or not created:
                continue
            try:
                # GT给的是UTC。mktime按本地时区解释,再减time.timezone方向还是错的
                # (本机UTC+8时 time.timezone=-28800,减等于又加了8小时)。
                # calendar.timegm 才是"把UTC的struct_time转成epoch"的正确函数。
                born = calendar.timegm(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                continue
            age = (time.time() - born) / 60
            if MIN_AGE_MIN <= age <= MAX_AGE_MIN:
                out.append((addr, a.get("name") or "", age))
        time.sleep(0.3)
    return out


HEARTBEAT_F = HERE / ".lab_heartbeat.json"


def write_heartbeat(**kw):
    """每轮写一次心跳。监控窗口靠它判断"进程还活着吗、卡在哪一步"。

    单看进程在不在不够: 进程可能活着但卡在某个池子的RPC上不动了。心跳里带
    上时间戳和当前处理到哪个池子,一眼能看出是在干活还是僵住了。
    """
    kw["ts"] = int(time.time())
    kw["pid"] = __import__("os").getpid()
    try:
        HEARTBEAT_F.write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def record_snapshot(c, pool, m, price):
    c.execute("INSERT OR REPLACE INTO snapshots (pool,ts,age_min,n_tx,n_wallet,"
              "top_share,reserve_usd,op_cost_usd,fish_in_usd,danger,price) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (pool, int(time.time()), m["life_min"], m["n_tx"], m["n_wallet"],
               m["top_share"], m["peak_res_usd"], m["op_cost_usd"],
               m["fish_in_usd"], m["danger"], price))
    c.commit()


def main():
    db.init()
    c = db.conn()
    c.executescript(EXTRA_DDL)
    c.executescript(ld.DDL)
    c.commit()

    watching = {}
    if "--add" in sys.argv:
        a = sys.argv[sys.argv.index("--add") + 1]
        watching[a] = Pool(a)
        log(f"手工加入观察: {a}")

    for r in c.execute("SELECT pool,name FROM watchlist WHERE dropped_at IS NULL "
                       "ORDER BY added_at DESC LIMIT ?", (MAX_WATCH,)):
        watching[r["pool"]] = Pool(r["pool"], r["name"] or "")
    if watching:
        log(f"从库里恢复 {len(watching)} 个观察对象")

    last_disc = 0.0
    log(f"RPC: {'Helius' if fx.HAS_HELIUS else '公共节点'}  本月用量 {fx.usage_report()}")
    log(f"守望启动。进场条件: 狗庄沉没成本>=${MIN_OP_COST:.0f}, 集中度>={MIN_TOP_SHARE:.0%}, "
        f"外部资金<=${MAX_FISH_AT_ENTRY:.0f}; 仓位${POS_USD:.0f}; 危险度>={DANGER_EXIT}离场")

    while True:
        if time.time() - last_disc > DISCOVER_GAP:
            try:
                for addr, name, age in discover_new():
                    if addr not in watching and len(watching) < MAX_WATCH:
                        watching[addr] = Pool(addr, name)
                        db.add_pool(addr, name=name,
                                    found_at=time.strftime("%Y-%m-%d %H:%M:%S"))
                        c.execute("INSERT OR REPLACE INTO watchlist (pool,name,added_at) "
                                  "VALUES (?,?,?)", (addr, name, int(time.time())))
                        c.commit()
                log(f"观察名单 {len(watching)} 个")
            except Exception as e:
                log(f"发现新币失败: {e}")
            last_disc = time.time()

        for i, (addr, p) in enumerate(list(watching.items()), 1):
            write_heartbeat(stage="扫描中", pool=p.name or addr[:10],
                            i=i, n=len(watching), helius=fx.usage_report())
            try:
                if p.refresh() == -1:
                    watching.pop(addr, None)
                    c.execute("UPDATE watchlist SET dropped_at=?, reason=? WHERE pool=?",
                              (int(time.time()), f"池子太大({p.too_big}笔)", addr))
                    c.commit()
                    log(f"腾位 {p.name or addr[:10]}: 池子太大({p.too_big}笔),不是钓鱼盘")
                    continue
                m = p.snapshot()
                if not m:
                    continue
                price = price_of(addr)
                record_snapshot(c, addr, m, price)

                # ---- 收网即记录 ----
                # 有没有持仓都要记。策略验收要的是全样本: 只统计我们进过场的
                # 那些,会漏掉"信号从没亮过就砸了"的情形,胜率算出来必然虚高。
                if m["outcome"] == "caught" or (
                        m["drained_usd"] > max(m["peak_res_usd"], 1) * 0.3):
                    if ld.record_dump(c, addr, p.name, m, price, DANGER_EXIT):
                        lead = c.execute("SELECT lead_sec, escaped FROM dump_events "
                                         "WHERE pool=?", (addr,)).fetchone()
                        tag = ("信号提前%.0f秒亮" % lead["lead_sec"]
                               if lead and lead["escaped"] else "信号没来得及")
                        log(f"收网 {p.name or addr[:10]}  抽走${m['drained_usd']:,.0f} "
                            f"危险度{m['danger']:.2f}  [{tag}]")

                # ---- 分诊 ----
                # 名额有限(RPC限速),必须只留狗庄盘。原本来者不拒,25个位置
                # 大半被"几十个钱包各买几刀"的普通新币占着,真正的钓鱼盘反而
                # 挤不进来。判据用集中度: 狗庄盘是一个钱包刷几百笔,集中度
                # 极高;有真实散户参与的币集中度都在20%以下。
                held_now = c.execute("SELECT 1 FROM paper_trades WHERE pool=? "
                                     "AND exit_ts IS NULL", (addr,)).fetchone()
                if not held_now:
                    drop = None
                    if m["n_tx"] >= 25 and m["top_share"] < 0.45:
                        drop = f"非狗庄盘(集中度{m['top_share']:.0%})"
                    elif m["idle_min"] > EVICT_IDLE_MIN:
                        drop = f"盘已死(静止{m['idle_min']:.0f}分钟)"
                    elif m["outcome"] == "caught":
                        drop = "已收网"
                    if drop:
                        watching.pop(addr, None)
                        c.execute("UPDATE watchlist SET dropped_at=?, reason=? WHERE pool=?",
                                  (int(time.time()), drop, addr))
                        c.commit()
                        log(f"腾位 {p.name or addr[:10]}: {drop}")
                        continue

                row = c.execute("SELECT * FROM paper_trades WHERE pool=?", (addr,)).fetchone()
                held = row and row["exit_ts"] is None

                if held:
                    pnl = ((price / row["entry_price"] - 1) * 100
                           if price and row["entry_price"] else 0.0)
                    peak = max(row["peak_price"] or 0, price or 0)
                    mx = max(row["max_danger"] or 0, m["danger"])
                    held_min = (time.time() - row["entry_ts"]) / 60
                    reason = None
                    if m["danger"] >= DANGER_EXIT:
                        reason = "危险度过线(鱼的钱超过他成本)"
                    elif pnl <= STOP_PCT:
                        reason = "止损"
                    elif held_min >= MAX_HOLD_MIN:
                        reason = "超时"
                    elif m["outcome"] == "caught" or m["drained_usd"] > m["peak_res_usd"] * 0.3:
                        reason = "他已砸盘"
                    c.execute("UPDATE paper_trades SET peak_price=?, max_danger=? WHERE pool=?",
                              (peak, mx, addr))
                    if reason:
                        # 边际价算出来的名义盈亏,再按出场时真实池深折算成能拿到手的钱。
                        # 进场也要付一次滑点,所以成本按 depth_adjust 的反向算。
                        res_now = max(m["peak_res_usd"] - m["drained_usd"], 0.0)
                        mkt_val = POS_USD * (1 + pnl / 100)
                        got = depth_adjust(mkt_val, res_now)
                        paid = POS_USD * (1 + POS_USD / max(row["entry_reserve"] or 1e9, 1e-9))
                        net = (got / paid - 1) * 100 if paid else 0.0
                        c.execute("UPDATE paper_trades SET exit_ts=?,exit_price=?,"
                                  "exit_reason=?,pnl_pct=?,pnl_pct_raw=?,exit_reserve=? "
                                  "WHERE pool=?",
                                  (int(time.time()), price, reason, round(net, 2),
                                   round(pnl, 2), round(res_now, 2), addr))
                        log(f"平仓 {p.name or addr[:10]}  名义{pnl:+.1f}% -> "
                            f"扣滑点{net:+.1f}%  (池深${res_now:,.0f})  [{reason}]")
                    c.commit()
                elif not row:
                    ok = (m["op_cost_usd"] >= MIN_OP_COST
                          and m["top_share"] >= MIN_TOP_SHARE
                          and m["fish_in_usd"] <= MAX_FISH_AT_ENTRY
                          and m["danger"] < 0.5
                          and POS_USD <= m["op_cost_usd"] * MAX_POS_FRAC
                          and price)
                    if ok:
                        c.execute("INSERT OR REPLACE INTO paper_trades (pool,name,entry_ts,"
                                  "entry_price,entry_op_cost,size_usd,peak_price,max_danger,"
                                  "entry_reserve) VALUES (?,?,?,?,?,?,?,?,?)",
                                  (addr, p.name, int(time.time()), price,
                                   m["op_cost_usd"], POS_USD, price, m["danger"],
                                   m["peak_res_usd"] - m["drained_usd"]))
                        c.commit()
                        log(f"*** 进场 {p.name or addr[:10]}  狗庄成本${m['op_cost_usd']:,.0f} "
                            f"集中度{m['top_share']:.0%} 鱼${m['fish_in_usd']:,.0f} ***")
            except Exception as e:
                log(f"{addr[:10]} 处理出错: {type(e).__name__}: {e}")

        n_open = c.execute("SELECT COUNT(*) n FROM paper_trades WHERE exit_ts IS NULL").fetchone()["n"]
        n_done = c.execute("SELECT COUNT(*) n FROM paper_trades WHERE exit_ts IS NOT NULL").fetchone()["n"]
        log(f"一轮结束  观察{len(watching)}个  持仓{n_open}  已平{n_done}")
        write_heartbeat(stage="休眠", watching=len(watching), open=n_open,
                        done=n_done, helius=fx.usage_report(), sleep_sec=SCAN_GAP)
        time.sleep(SCAN_GAP)


if __name__ == "__main__":
    main()
