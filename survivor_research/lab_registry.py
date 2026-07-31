# -*- coding: utf-8 -*-
"""账户角色注册表(持久化+缓存)。2026-07-31建。

lab_roles.py 证明了分类逻辑可行,但一个池子要跑10分钟——每个候选账户都要
全量扫历史。VPS上40个观察对象每3分钟一轮,这个开销完全不可行。

关键观察: **同一个账户在所有池子里角色相同**。pump.fun的协议费账户
GesfTA3X2ari 在每个币里都是协议费账户,探测一次就能一直用。托管平台
BwWK17cbHxwW 也一样。所以把探测结果落库,以后查表即可。

命中率会非常高: 协议费账户和几个大托管平台会出现在绝大多数池子里,
只有每个币自己的创建者费账户和真实交易者是新面孔。

为什么这件事非做不可: 不剔除这几类账户,"狗庄成本"和"鱼的钱"就是错的。
$GATE 那个池子里资金流量最大的两个账户都是托管平台,把它们算成交易者,
算出来的是平台代表一堆用户的进出总和,跟任何一个操盘方都没关系。
"""
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import lab_db as db          # noqa: E402
import lab_forensics as fx   # noqa: E402

TTL_SEC = 7 * 24 * 3600      # 角色一周内不会变,过期才重探
PROBE_TX = 150               # 每个账户探测多少笔(够判定即可,别浪费额度)
CUSTODIAL_SOL = 500.0
CUSTODIAL_SIGNERS = 2
CUSTODIAL_MINTS = 5
FEE_PAY_RATIO = 0.02
FEE_MIN_HITS = 0.10

DDL = """
CREATE TABLE IF NOT EXISTS accounts (
  addr       TEXT PRIMARY KEY,
  role       TEXT,      -- protocol_fee / creator_fee / custodial / curve / trader
  reason     TEXT,
  bal_sol    REAL,
  rate_min   REAL,      -- 每分钟交易笔数
  n_mints    INTEGER,   -- 涉及多少种标的币
  n_signers  INTEGER,   -- 多少个不同签名人用它
  probed_at  INTEGER
);
CREATE INDEX IF NOT EXISTS ix_acct_role ON accounts(role);
"""


def init():
    c = db.conn()
    c.executescript(DDL)
    c.commit()


def _probe(addr):
    info = fx.rpc("getAccountInfo", [addr, {"encoding": "jsonParsed"}])
    v = (info or {}).get("value") or {}
    bal = v.get("lamports", 0) / 1e9
    sigs = fx.get_signatures(addr, cap=PROBE_TX + 50)
    ok = [s for s in sigs if not s["err"] and s.get("ts")]
    if not ok:
        return {"bal": bal, "rate": 0.0, "mints": 0, "signers": 0}
    span = max((ok[-1]["ts"] - ok[0]["ts"]) / 60, 0.01)
    mints, signers = set(), Counter()

    def one(s):
        r = fx.rpc("getTransaction", [s["sig"], {"maxSupportedTransactionVersion": 0,
                                                 "encoding": "jsonParsed"}])
        if not r:
            return None
        k = fx.account_keys(r)
        ms = {b.get("mint") for b in ((r.get("meta") or {}).get("postTokenBalances") or [])
              if b.get("mint") not in fx.QUOTES}
        return (k[0] if k else None), ms

    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(one, ok[-PROBE_TX:]):
            if not r:
                continue
            s, ms = r
            if s:
                signers[s] += 1
            mints |= ms
    return {"bal": bal, "rate": len(ok) / span,
            "mints": len(mints), "signers": len(signers)}


def role_of(addr, pool=None, inn=0.0, out=0.0, hits=0, ntx=1, allow_probe=True):
    """返回 (角色, 理由)。池内统计只用来判费用账户,其余靠账户自身特征。"""
    if pool and addr == pool:
        return "curve", "池子账户本身"
    c = db.conn()
    r = c.execute("SELECT * FROM accounts WHERE addr=?", (addr,)).fetchone()
    fresh = r and (time.time() - (r["probed_at"] or 0) < TTL_SEC)
    if fresh and r["role"] in ("protocol_fee", "custodial"):
        return r["role"], r["reason"]     # 这两类与池子无关,直接用缓存

    # 费用账户的判据依赖池内统计,每个池子都要重算一次(便宜,不用探测)
    if out > 0 and inn < out * FEE_PAY_RATIO and hits >= ntx * FEE_MIN_HITS:
        if fresh:
            f = {"bal": r["bal_sol"], "rate": r["rate_min"],
                 "mints": r["n_mints"], "signers": r["n_signers"]}
        elif allow_probe:
            f = _probe(addr)
        else:
            return "trader", "未探测"
        # 角色必须落库。第一版这里传了 role=None,导致协议费账户每次都要重探,
        # 缓存等于没起作用。
        if f["mints"] > 3:
            role, why = "protocol_fee", f"只收不付,跨{f['mints']}个币"
        else:
            role, why = "creator_fee", "只收不付,仅服务本币"
        _save(addr, role, why, f)
        return role, why

    if fresh:
        return r["role"] or "trader", r["reason"] or ""
    if not allow_probe:
        return "trader", "未探测"
    f = _probe(addr)
    if f["bal"] >= CUSTODIAL_SOL and (f["signers"] >= CUSTODIAL_SIGNERS
                                      or f["mints"] >= CUSTODIAL_MINTS):
        role = "custodial"
        why = (f"余额{f['bal']:,.0f} SOL, {f['signers']}个签名人, "
               f"跨{f['mints']}个币, {f['rate']:.0f}笔/分")
    else:
        role, why = "trader", ""
    _save(addr, role, why, f)
    return role, why


def _save(addr, role, why, f):
    c = db.conn()
    old = c.execute("SELECT role, reason FROM accounts WHERE addr=?", (addr,)).fetchone()
    if role is None and old:
        role, why = old["role"], old["reason"]
    c.execute("INSERT OR REPLACE INTO accounts (addr,role,reason,bal_sol,rate_min,"
              "n_mints,n_signers,probed_at) VALUES (?,?,?,?,?,?,?,?)",
              (addr, role, why, f["bal"], f["rate"], f["mints"], f["signers"],
               int(time.time())))
    c.commit()


def stats():
    c = db.conn()
    return {r["role"]: r["n"] for r in
            c.execute("SELECT role, COUNT(*) n FROM accounts GROUP BY role")}


if __name__ == "__main__":
    init()
    args = [a for a in sys.argv[1:] if len(a) > 30]
    if not args:
        print("注册表统计:", stats())
        c = db.conn()
        for r in c.execute("SELECT * FROM accounts WHERE role!='trader' "
                           "ORDER BY bal_sol DESC LIMIT 20"):
            print(f"  {r['role']:<14}{r['addr']}  {r['reason']}")
    else:
        for a in args:
            print(a, "->", role_of(a))
