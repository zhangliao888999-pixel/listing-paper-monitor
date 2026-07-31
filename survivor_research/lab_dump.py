# -*- coding: utf-8 -*-
"""收网事件采集。2026-07-31建。

策略成不成立,全押在一个问题上:
    **危险度信号是在砸盘之前多久亮起来的?**
如果它只在砸盘的同时或之后才亮,这套策略就是废的——我们永远跑不掉。
所以每一次收网都必须留下完整记录,而且要**回溯**快照历史,找出危险度第一次
越过离场线的时刻,算出提前量。

除了提前量,还要记下这些才能回答"怎么执行、成功率多少":
  - 砸盘那一刻他的成本、鱼的钱、危险度 -> 校准离场线到底该设多少
  - 砸盘前5分钟最大的一笔外部买入     -> 多大的买单会触发收网(安全仓位上限)
  - 砸盘持续了几秒                     -> 留给我们的反应窗口
  - 价格崩了多少、池子被抽走多少       -> 没跑掉的话会亏多少

假警报也必须记(危险度越线了但他没砸): 只统计成功案例会得出虚高的胜率。
"""
import time

DDL = """
CREATE TABLE IF NOT EXISTS dump_events (
  pool            TEXT PRIMARY KEY,
  name            TEXT,
  detected_ts     INTEGER,   -- 我们观测到的时刻
  dump_ts         INTEGER,   -- 链上砸盘起始
  dump_sec        REAL,      -- 砸盘持续秒数 = 我们的反应窗口
  drained_usd     REAL,
  op_cost_usd     REAL,      -- 砸盘时他的成本
  fish_in_usd     REAL,      -- 砸盘时鱼的钱
  danger_at_dump  REAL,      -- 两者之比 <- 用来校准离场线
  trigger_buy_usd REAL,      -- 砸盘前5分钟最大的一笔外部买入
  max_fish_ignored REAL,     -- 他明确没理会的最大买单 <- 安全仓位上限
  n_wallet        INTEGER,
  top_share       REAL,
  life_min        REAL,      -- 从开盘到砸盘活了多久
  ratchet_usd_min REAL,      -- 撒饵速度
  peak_res_usd    REAL,
  price_peak      REAL,
  price_after     REAL,
  crash_pct       REAL,
  -- 策略验收: 信号提前量
  cross_ts        INTEGER,   -- 危险度第一次越过离场线的时刻
  lead_sec        REAL,      -- dump_ts - cross_ts  <- 最关键的数
  price_at_cross  REAL,      -- 越线那一刻的价格(我们本该在这里卖出)
  escaped         INTEGER,   -- 1=信号提前亮了跑得掉  0=没跑掉
  n_snapshots     INTEGER    -- 有多少快照可回溯,太少说明观测不充分
);
CREATE TABLE IF NOT EXISTS false_alarms (
  pool TEXT PRIMARY KEY, name TEXT, cross_ts INTEGER,
  danger REAL, op_cost_usd REAL, fish_in_usd REAL,
  price_at_cross REAL, price_later REAL, minutes_after REAL,
  missed_gain_pct REAL      -- 越线后价格还涨了多少 = 我们早跑损失的收益
);
CREATE INDEX IF NOT EXISTS ix_dump_escaped ON dump_events(escaped);
"""


def record_dump(c, pool, name, m, price_now, exit_threshold):
    """记录一次收网。回溯快照历史算信号提前量。

    m 是 lab_forensics.analyze 的结果。dump_ts 用链上检测出的砸盘起点,
    不是我们观测到的时刻——观测有最多一个扫描周期的延迟,用观测时刻会把
    提前量算少(甚至算成负的)。
    """
    if c.execute("SELECT 1 FROM dump_events WHERE pool=?", (pool,)).fetchone():
        return False

    snaps = c.execute("SELECT ts, danger, price, op_cost_usd, fish_in_usd "
                      "FROM snapshots WHERE pool=? ORDER BY ts", (pool,)).fetchall()
    # 链上砸盘时刻: analyze 给的是相对开盘的分钟数换算不出绝对时间,
    # 所以用 dump_sec 配合最后一笔交易时间反推(life_min 是开盘到最后一笔)
    now = int(time.time())
    dump_ts = now - int((m.get("idle_min") or 0) * 60)

    cross_ts = price_at_cross = None
    for s in snaps:
        if (s["danger"] or 0) >= exit_threshold:
            cross_ts, price_at_cross = s["ts"], s["price"]
            break
    lead = (dump_ts - cross_ts) if cross_ts else None
    # 跑得掉的条件: 信号在砸盘前就亮了,而且至少领先一个扫描周期
    escaped = 1 if (lead is not None and lead > 0) else 0

    prices = [s["price"] for s in snaps if s["price"]]
    peak = max(prices) if prices else None
    crash = ((price_now / peak - 1) * 100) if (peak and price_now) else None

    c.execute("""INSERT OR REPLACE INTO dump_events
        (pool,name,detected_ts,dump_ts,dump_sec,drained_usd,op_cost_usd,fish_in_usd,
         danger_at_dump,trigger_buy_usd,max_fish_ignored,n_wallet,top_share,life_min,
         ratchet_usd_min,peak_res_usd,price_peak,price_after,crash_pct,
         cross_ts,lead_sec,price_at_cross,escaped,n_snapshots)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (pool, name, now, dump_ts, m.get("dump_sec"), m.get("drained_usd"),
               m.get("op_cost_usd"), m.get("fish_in_usd"),
               (m.get("fish_in_usd") or 0) / max(m.get("op_cost_usd") or 1, 1),
               m.get("trigger_buy_usd"), m.get("max_fish_ignored"),
               m.get("n_wallet"), m.get("top_share"), m.get("life_min"),
               m.get("ratchet_usd_min"), m.get("peak_res_usd"),
               peak, price_now, crash,
               cross_ts, lead, price_at_cross, escaped, len(snaps)))
    c.commit()
    return True


def sweep_false_alarms(c, exit_threshold, min_minutes=45):
    """找出"危险度越线了但他一直没砸"的池子。

    只统计跑掉的成功案例会得出虚高的胜率: 每一次早跑都放弃了后面的涨幅,
    这个成本必须记进去,否则策略评估是自欺欺人。
    """
    n = 0
    rows = c.execute("""
        SELECT s.pool, w.name, MIN(s.ts) AS cross_ts
        FROM snapshots s JOIN watchlist w ON w.pool = s.pool
        WHERE s.danger >= ?
          AND s.pool NOT IN (SELECT pool FROM dump_events)
        GROUP BY s.pool""", (exit_threshold,)).fetchall()
    for r in rows:
        later = c.execute("SELECT ts, price, danger, op_cost_usd, fish_in_usd "
                          "FROM snapshots WHERE pool=? ORDER BY ts DESC LIMIT 1",
                          (r["pool"],)).fetchone()
        at = c.execute("SELECT price, danger, op_cost_usd, fish_in_usd FROM snapshots "
                       "WHERE pool=? AND ts=?", (r["pool"], r["cross_ts"])).fetchone()
        if not later or not at:
            continue
        mins = (later["ts"] - r["cross_ts"]) / 60
        if mins < min_minutes:
            continue          # 还没观察够久,现在下结论太早
        miss = ((later["price"] / at["price"] - 1) * 100
                if (at["price"] and later["price"]) else None)
        c.execute("""INSERT OR REPLACE INTO false_alarms
            (pool,name,cross_ts,danger,op_cost_usd,fish_in_usd,
             price_at_cross,price_later,minutes_after,missed_gain_pct)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (r["pool"], r["name"], r["cross_ts"], at["danger"],
                   at["op_cost_usd"], at["fish_in_usd"], at["price"],
                   later["price"], mins, miss))
        n += 1
    c.commit()
    return n
