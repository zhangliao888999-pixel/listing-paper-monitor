# -*- coding: utf-8 -*-
"""母钱包追踪。2026-08-01建。

为什么必须做这个: 惯犯监控按"作案钱包"追踪对 4/4 大网无效 —— 实测58(本机)
/95(VPS)个作案钱包里,**跨币复用为0**。这些团队每开一个新盘就换一整批
一次性钱包。

但钱不会凭空出现。一次性钱包的第一笔SOL必然来自某个上游地址,而攒SOL、
过CEX、维护资金池都有成本,**上游会复用**。找到它就等于给团队打了标记。

做法: 对每个作案钱包,翻到它最早的交易,找出"谁给它转的第一笔钱"。
然后统计哪些地址给多个币的多个钱包供过资 —— 那就是母钱包。

用法:
  python lab_funder.py            追踪库里所有作案钱包的资金来源
  python lab_funder.py --report   只看统计
"""
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import lab_db as db          # noqa: E402
import lab_forensics as fx   # noqa: E402

SOL = 75.0
DDL = """
CREATE TABLE IF NOT EXISTS wallet_funders (
  wallet TEXT PRIMARY KEY, funder TEXT, amount_sol REAL,
  funded_at INTEGER, wallet_born INTEGER, n_sig INTEGER, checked_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_wf_funder ON wallet_funders(funder);
"""


def init():
    c = db.conn()
    c.executescript(DDL)
    c.commit()


def first_funder(addr):
    """翻到钱包最早的交易,找出第一笔转入SOL的来源。

    一次性钱包交易不多,翻到底成本可控。翻不到底就用能拿到的最早一笔 ——
    宁可标记为不确定,也不能拿中途的某笔当成源头。
    """
    sigs = fx.get_signatures(addr, cap=3000)
    ok = [s for s in sigs if not s["err"] and s.get("ts")]
    if not ok:
        return None
    for s in ok[:6]:            # 最早几笔里找第一笔真正收到钱的
        r = fx.rpc("getTransaction", [s["sig"],
                                      {"maxSupportedTransactionVersion": 0,
                                       "encoding": "jsonParsed"}])
        if not r:
            continue
        keys = fx.account_keys(r)
        meta = r.get("meta") or {}
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if addr not in keys:
            continue
        i = keys.index(addr)
        if i >= len(pre) or i >= len(post):
            continue
        got = (post[i] - pre[i]) / 1e9
        if got <= 0.001:
            continue
        # 同笔里掉钱最多的那个账户就是出资方
        src, sd = None, 0.0
        for j, k in enumerate(keys):
            if k == addr or j >= len(pre) or j >= len(post):
                continue
            d = (post[j] - pre[j]) / 1e9
            if d < sd:
                src, sd = k, d
        if src:
            return {"funder": src, "amount": got, "at": s["ts"],
                    "born": ok[0]["ts"], "n": len(ok)}
    return {"funder": None, "amount": 0.0, "at": None,
            "born": ok[0]["ts"], "n": len(ok)}


def report():
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM wallet_funders").fetchone()["n"]
    print("已追踪 %d 个作案钱包的资金来源" % n)
    rows = c.execute("""
        SELECT f.funder, COUNT(DISTINCT f.wallet) nw, COUNT(DISTINCT o.pool) np,
               SUM(f.amount_sol) amt
        FROM wallet_funders f LEFT JOIN operator_wallets o ON o.addr = f.wallet
        WHERE f.funder IS NOT NULL
        GROUP BY f.funder ORDER BY nw DESC LIMIT 30""").fetchall()
    print("")
    print("=== 给多个作案钱包供过资的地址(母钱包) ===")
    print("  %-46s%8s%8s%14s" % ("母钱包", "供资钱包", "涉及币", "供资总额SOL"))
    for r in rows:
        if (r["nw"] or 0) < 2:
            continue
        print("  %-46s%8d%8d%14.3f"
              % (r["funder"], r["nw"], r["np"] or 0, r["amt"] or 0))
    multi = [r for r in rows if (r["nw"] or 0) >= 2]
    if not multi:
        print("  (还没有给2个以上钱包供资的地址)")
        return
    print("")
    print("=== 最大的母钱包,它供资的钱包都在哪些币上 ===")
    for r in multi[:3]:
        print("  %s  (供资 %d 个钱包)" % (r["funder"], r["nw"]))
        ws = c.execute("""SELECT f.wallet, f.amount_sol, o.pool, l.name, l.score
                          FROM wallet_funders f
                          LEFT JOIN operator_wallets o ON o.addr=f.wallet
                          LEFT JOIN launch_fp l ON l.pool=o.pool
                          WHERE f.funder=? LIMIT 12""", (r["funder"],)).fetchall()
        for x in ws:
            print("     %-46s %.3f SOL  %s (%s/4)"
                  % (x["wallet"], x["amount_sol"] or 0,
                     str(x["name"])[:16], x["score"] or 0))


def main():
    init()
    if "--report" in sys.argv:
        report()
        return
    c = db.conn()
    todo = [r["addr"] for r in c.execute(
        "SELECT DISTINCT addr FROM operator_wallets WHERE addr NOT IN "
        "(SELECT wallet FROM wallet_funders)")]
    print("待追踪 %d 个作案钱包" % len(todo), flush=True)
    done = [0]

    def work(a):
        try:
            r = first_funder(a)
        except Exception:
            r = None
        done[0] += 1
        if done[0] % 10 == 0:
            print("  %d/%d" % (done[0], len(todo)), flush=True)
        return a, r

    with ThreadPoolExecutor(max_workers=5) as ex:
        for a, r in ex.map(work, todo):
            if not r:
                continue
            c.execute("INSERT OR REPLACE INTO wallet_funders "
                      "(wallet,funder,amount_sol,funded_at,wallet_born,n_sig,checked_at) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (a, r["funder"], r["amount"], r["at"], r["born"], r["n"],
                       int(time.time())))
            c.commit()
    print("")
    report()


if __name__ == "__main__":
    main()
