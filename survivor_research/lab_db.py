# -*- coding: utf-8 -*-
"""狗庄研究实验室 - 数据层。2026-07-31建。

为什么用SQLite不用jsonl: 之前每个币一个脚本一堆散落的json,想做跨样本统计
就得每次重新解析。这套系统要跑几百上千个币、连续跑很多天,必须能随时中断
续跑、随时查询"所有已完成的样本里,钓到鱼的占几成"。

三张表:
  pools    —— 样本清单和采集状态(断点续传靠它)
  wallets  —— 每个池子里每个钱包的完整流水,角色标注
  metrics  —— 每个池子一行的汇总指标,跨样本分析直接查这张表

金额一律存USD,链上原始单位(SOL/USDC)在forensics里就换算掉,避免分析时
还要处理不同计价币。
"""
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).parent / "operator_lab.db"
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (
  addr        TEXT PRIMARY KEY,
  mint        TEXT,
  name        TEXT,
  dex         TEXT,           -- pumpswap / meteora / raydium ...
  quote_mint  TEXT,
  created_at  TEXT,
  found_at    TEXT,           -- 我们什么时候发现它的
  n_sig       INTEGER,        -- 链上签名总数
  first_ts    INTEGER,
  last_ts     INTEGER,
  status      TEXT DEFAULT 'pending',   -- pending/running/done/failed/skip
  err         TEXT
);

CREATE TABLE IF NOT EXISTS wallets (
  pool      TEXT,
  addr      TEXT,
  role      TEXT,      -- operator / cluster / fish / creator
  n_tx      INTEGER,
  n_buy     INTEGER,
  n_sell    INTEGER,
  in_usd    REAL,      -- 投进去的钱
  out_usd   REAL,      -- 拿走的钱
  gas_usd   REAL,      -- gas+优先费+Jito小费(按原生SOL余额首末差算)
  tok_held  REAL,      -- 结束时还持有多少标的币
  first_ts  INTEGER,
  last_ts   INTEGER,
  PRIMARY KEY (pool, addr)
);

CREATE TABLE IF NOT EXISTS metrics (
  pool            TEXT PRIMARY KEY,
  quote_sym       TEXT,
  n_tx            INTEGER,
  n_wallet        INTEGER,   -- 签名钱包数(PDA/程序账户不算)
  life_min        REAL,
  idle_min        REAL,      -- 距最后一笔多久了
  top_share       REAL,      -- 最活跃单钱包占总交易笔数比例
  hhi             REAL,      -- 交易笔数的赫芬达尔指数,越高越集中
  op_addr         TEXT,
  op_cost_usd     REAL,      -- 操盘方一伙砸进去的
  op_out_usd      REAL,      -- 操盘方一伙拿走的
  op_gas_usd      REAL,
  op_pnl_usd      REAL,      -- = out - cost - gas
  op_tok_held     REAL,
  fish_n          INTEGER,   -- 真实外部钱包数
  fish_in_usd     REAL,
  fish_out_usd    REAL,
  peak_res_usd    REAL,
  end_res_usd     REAL,
  drained_usd     REAL,
  t_first_fish    REAL,      -- 开盘到第一条鱼买入(分钟)
  t_fish_to_dump  REAL,      -- 第一条鱼到砸盘(分钟) <- 最关键的数
  dump_sec        REAL,      -- 砸盘持续秒数
  max_drawdown    REAL,
  outcome         TEXT,      -- no_fish / caught / abandoned / running
  updated_at      TEXT,

  -- 下面这组专门服务于"寄生策略": 小额进场蹭狗庄自己的拉盘,
  -- 看到真买家出现就跑。要回答的核心问题是: 多大的买单会触发他收网?
  trigger_buy_usd   REAL,    -- 砸盘前5分钟内最大的一笔外部买入
  op_cost_at_dump   REAL,    -- 砸盘那一刻他已经沉没了多少成本
  trigger_ratio     REAL,    -- trigger_buy / op_cost_at_dump, 阈值就藏在这里
  fish_in_at_dump   REAL,    -- 砸盘前外部资金累计流入
  ratchet_usd_min   REAL,    -- 撒饵速度: 每分钟自买多少美元
  max_fish_ignored  REAL     -- 没触发砸盘的最大单笔外部买入 <- 安全仓位上限
);

CREATE INDEX IF NOT EXISTS ix_pools_status ON pools(status);
CREATE INDEX IF NOT EXISTS ix_metrics_outcome ON metrics(outcome);
CREATE INDEX IF NOT EXISTS ix_wallets_role ON wallets(role);
"""


def conn():
    """每个线程一个连接。SQLite的连接不能跨线程共享。"""
    if not hasattr(_local, "c"):
        c = sqlite3.connect(str(DB_PATH), timeout=60)
        c.execute("PRAGMA journal_mode=WAL")      # 允许一边写一边查
        c.execute("PRAGMA synchronous=NORMAL")
        c.row_factory = sqlite3.Row
        _local.c = c
    return _local.c


def init():
    c = conn()
    c.executescript(SCHEMA)
    c.commit()


def add_pool(addr, **kw):
    """登记一个待分析的池子。已存在就不动,保护已完成的采集结果。"""
    c = conn()
    cols = ["addr"] + list(kw)
    q = f"INSERT OR IGNORE INTO pools ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})"
    c.execute(q, [addr] + list(kw.values()))
    c.commit()


def claim_pool():
    """原子地领一个待办池子,防止多个worker抢同一个。"""
    c = conn()
    cur = c.execute("SELECT addr FROM pools WHERE status='pending' "
                    "ORDER BY COALESCE(n_sig, 999999) ASC LIMIT 1")
    r = cur.fetchone()
    if not r:
        return None
    n = c.execute("UPDATE pools SET status='running' WHERE addr=? AND status='pending'",
                  (r["addr"],)).rowcount
    c.commit()
    return r["addr"] if n else claim_pool()


def set_status(addr, status, err=None):
    c = conn()
    c.execute("UPDATE pools SET status=?, err=? WHERE addr=?", (status, err, addr))
    c.commit()


def save_pool_meta(addr, **kw):
    if not kw:
        return
    c = conn()
    c.execute(f"UPDATE pools SET {','.join(k+'=?' for k in kw)} WHERE addr=?",
              list(kw.values()) + [addr])
    c.commit()


def save_wallets(pool, rows):
    c = conn()
    c.execute("DELETE FROM wallets WHERE pool=?", (pool,))
    c.executemany(
        "INSERT INTO wallets (pool,addr,role,n_tx,n_buy,n_sell,in_usd,out_usd,"
        "gas_usd,tok_held,first_ts,last_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(pool, r["addr"], r["role"], r["n_tx"], r["n_buy"], r["n_sell"],
          r["in_usd"], r["out_usd"], r["gas_usd"], r["tok_held"],
          r["first_ts"], r["last_ts"]) for r in rows])
    c.commit()


def save_metrics(pool, m):
    c = conn()
    m = dict(m, pool=pool)
    cols = list(m)
    c.execute(f"INSERT OR REPLACE INTO metrics ({','.join(cols)}) "
              f"VALUES ({','.join('?'*len(cols))})", [m[k] for k in cols])
    c.commit()


def counts():
    c = conn()
    return {r["status"]: r["n"] for r in
            c.execute("SELECT status, COUNT(*) n FROM pools GROUP BY status")}


if __name__ == "__main__":
    init()
    print(f"建库完成: {DB_PATH}")
    print(counts())
