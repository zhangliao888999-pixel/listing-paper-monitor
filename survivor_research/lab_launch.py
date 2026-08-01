# -*- coding: utf-8 -*-
"""开盘指纹: 在币诞生后几分钟内判定"这是不是一张大网"。2026-08-01建。

从 DeepSeek4 的开盘挖出来的四个特征,全部可在开盘3分钟内算出,且互相独立:

  1. 头2分钟净流入 >= $5,000      —— 重仓建底
  2. 30秒窗口内同时启动 >= 8 个钱包 —— 协同
  3. 同秒交易占比 >= 30%           —— 机器人(真人做不到,放慢就拉不动价格)
  4. 至少1笔 >= $1,000 的铺底单     —— 有本钱

DeepSeek4 实测: $11,726 / 19个 / >50% / 2笔,四条全中。
它开盘90秒铺了约5万,活了11小时,价格涨10倍,而真实外部买家只有 $4,623 ——
正是"大网 + 好饵 + 没钓到鱼"的组合,理论上给了从容进出的窗口。

注意单笔金额是**刻意随机化**的(离散度4.89,不是固定金额),所以不能用
"金额整齐"当判据 —— 那条已经被他们规避了。同秒下单躲不掉,因为放慢就
达不到拉盘效果。

同时把参与的机器人钱包入库(operator_wallets),以后这些地址再出现在新币
开盘时可以立刻报警 —— 同一个团队会复用钱包和出资地址。

用法:
  python lab_launch.py <pool>           分析一个池子的开盘
  python lab_launch.py --known          列出库里已记录的作案钱包
"""
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import cg_client as cg       # noqa: E402
import lab_db as db          # noqa: E402
import lab_forensics as fx   # noqa: E402

SOL = 75.0
WIN_SEC = 180            # 判定窗口: 开盘头3分钟
BURST_SEC = 30           # "同时启动"的窗口
MIN_CAP2 = 5000.0        # 头2分钟净流入门槛
MIN_BURST = 8            # 同时启动的钱包数门槛
MIN_SAMESEC = 0.30       # 同秒交易占比门槛
MIN_SEED = 1000.0        # 铺底单门槛

DDL = """
CREATE TABLE IF NOT EXISTS launch_fp (
  pool TEXT PRIMARY KEY, name TEXT, mint TEXT, created_at TEXT, checked_at INTEGER,
  n_tx INTEGER, cap_2min REAL, burst_wallets INTEGER, samesec_ratio REAL,
  seed_max REAL, n_seed INTEGER, score INTEGER, verdict TEXT
);
CREATE TABLE IF NOT EXISTS operator_wallets (
  addr TEXT, pool TEXT, role TEXT, n_tx INTEGER, net_usd REAL,
  first_sec REAL, seen_at INTEGER,
  PRIMARY KEY (addr, pool)
);
CREATE INDEX IF NOT EXISTS ix_opw_addr ON operator_wallets(addr);
"""


def init():
    c = db.conn()
    c.executescript(DDL)
    c.commit()


def detect_quote(txs):
    """判定计价币。不预设SOL —— Speed/USDC 那个池子的钱全走USDC代币流水,
    只看原生SOL会认不出金库,整个池子被判成"数据不足"。"""
    hits = defaultdict(int)
    for t in txs:
        for (_, mint) in t["flow"]:
            if mint in fx.QUOTES:
                hits[mint] += 1
    q = max(fx.QUOTES, key=lambda m: hits.get(m, 0))
    return q if hits.get(q, 0) >= len(txs) * 0.25 else fx.WSOL


def find_vault(txs, pool, quote):
    """金库 = 出现在最多交易里、计价币变动最大的非签名账户。"""
    hits = defaultdict(int)
    mag = defaultdict(float)
    signers = {t["signer"] for t in txs if t["signer"]}
    for t in txs:
        if quote == fx.WSOL:
            src = t["sol_delta"].items()
        else:
            src = [(o, v) for (o, m), v in t["flow"].items() if m == quote]
        for a, d in src:
            if a == pool or a in signers:
                continue
            hits[a] += 1
            mag[a] += abs(d)
    cand = [(mag[a], a) for a in hits if hits[a] > len(txs) * 0.5]
    return max(cand)[1] if cand else None


def qdelta(t, acct, quote):
    """账户在这笔交易里的计价币变化(SOL用lamport, 代币用flow)。"""
    if quote == fx.WSOL:
        return t["sol_delta"].get(acct, 0.0)
    return t["flow"].get((acct, quote), 0.0)


def fingerprint(pool, verbose=True):
    info = cg.get("networks/solana/pools/" + pool)
    a = (info or {}).get("data", {}).get("attributes", {}) or {}
    rel = (info or {}).get("data", {}).get("relationships", {}) or {}
    mint = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    mint = mint.replace("solana_", "")

    sigs = [s for s in fx.get_signatures(pool, cap=4000)
            if not s["err"] and s.get("ts")]
    if len(sigs) < 20:
        return None
    t0 = sigs[0]["ts"]
    win = [s for s in sigs if s["ts"] - t0 <= WIN_SEC]
    txs = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for t in ex.map(fx.parse_tx, win):
            if t:
                txs.append(t)
    if len(txs) < 15:
        return None
    txs.sort(key=lambda x: x["ts"])
    quote = detect_quote(txs)
    qpx = fx.QUOTES[quote][1]
    vault = find_vault(txs, pool, quote)
    if not vault:
        return None

    per = defaultdict(lambda: {"in": 0.0, "out": 0.0, "n": 0, "first": None})
    seeds = []
    for t in txs:
        vd = qdelta(t, vault, quote)
        if abs(vd) < 1e-9:
            continue
        best, bd = None, 0.0
        src = (t["sol_delta"].items() if quote == fx.WSOL
               else [(o, v) for (o, m), v in t["flow"].items() if m == quote])
        for x, d in src:
            if x in (vault, pool):
                continue
            if vd > 0 and d < 0 and abs(d) > abs(bd):
                best, bd = x, d
            elif vd < 0 and d > 0 and abs(d) > abs(bd):
                best, bd = x, d
        if not best:
            continue
        p = per[best]
        p["n"] += 1
        if p["first"] is None:
            p["first"] = t["ts"]
        if vd > 0:
            p["in"] += vd
            if vd * qpx >= MIN_SEED:
                seeds.append(vd * qpx)
        else:
            p["out"] += -vd

    cap2 = sum(v["in"] - v["out"] for v in per.values()
               if v["first"] and v["first"] - t0 <= 120) * qpx
    starts = sorted(v["first"] - t0 for v in per.values() if v["first"])
    burst = 0
    for i, s in enumerate(starts):
        j = i
        while j < len(starts) and starts[j] - s <= BURST_SEC:
            j += 1
        burst = max(burst, j - i)
    sec_cnt = defaultdict(int)
    for t in txs:
        sec_cnt[t["ts"]] += 1
    samesec = sum(n for n in sec_cnt.values() if n > 1) / max(len(txs), 1)

    score = ((1 if cap2 >= MIN_CAP2 else 0) + (1 if burst >= MIN_BURST else 0)
             + (1 if samesec >= MIN_SAMESEC else 0) + (1 if seeds else 0))
    verdict = {4: "大网(四条全中)", 3: "疑似大网", 2: "中等", 1: "弱", 0: "无迹象"}[score]

    res = {"pool": pool, "name": a.get("name"), "mint": mint,
           "created_at": a.get("pool_created_at"), "checked_at": int(time.time()),
           "n_tx": len(txs), "cap_2min": round(cap2, 2), "burst_wallets": burst,
           "samesec_ratio": round(samesec, 4),
           "seed_max": round(max(seeds), 2) if seeds else 0.0,
           "n_seed": len(seeds), "score": score, "verdict": verdict}
    _qpx = qpx

    bots = [(x, v) for x, v in per.items()
            if v["n"] >= 5 and v["first"] and v["first"] - t0 <= 120]
    if verbose:
        print("=" * 76)
        print("  %s   %s" % (a.get("name"), pool))
        print("=" * 76)
        print("  发行 %s   开盘头 %d 秒 %d 笔"
              % (a.get("pool_created_at"), WIN_SEC, len(txs)))
        print("")
        print("  %-26s%16s%12s" % ("判据", "实测", "门槛"))
        print("  %-26s%16s%12s  %s"
              % ("头2分钟净流入", "$" + format(cap2, ",.0f"),
                 "$" + format(MIN_CAP2, ",.0f"), "OK" if cap2 >= MIN_CAP2 else "-"))
        print("  %-26s%16d%12d  %s"
              % ("30秒内同时启动钱包", burst, MIN_BURST, "OK" if burst >= MIN_BURST else "-"))
        print("  %-26s%15.0f%%%11.0f%%  %s"
              % ("同秒交易占比", samesec * 100, MIN_SAMESEC * 100,
                 "OK" if samesec >= MIN_SAMESEC else "-"))
        print("  %-26s%16s%12s  %s"
              % ("铺底大单(>=$1000)", "%d笔 最大$%s" % (len(seeds), format(max(seeds), ",.0f") if seeds else 0),
                 ">=1笔", "OK" if seeds else "-"))
        print("")
        print("  评分 %d/4 -> %s" % (score, verdict))
        print("")
        print("  开盘参与的钱包 (>=5笔, 头2分钟进场):")
        print("  %-46s%7s%12s%9s" % ("钱包", "笔数", "净USD", "进场秒"))
        for x, v in sorted(bots, key=lambda kv: -(kv[1]["in"] - kv[1]["out"]))[:16]:
            print("  %-46s%7d%12s%9.0f"
                  % (x, v["n"], "$" + format((v["in"] - v["out"]) * qpx, ",.0f"),
                     v["first"] - t0))
    return res, bots, t0, qpx


def save(res, bots, t0, qpx=1.0):
    c = db.conn()
    cols = list(res)
    c.execute("INSERT OR REPLACE INTO launch_fp (%s) VALUES (%s)"
              % (",".join(cols), ",".join("?" * len(cols))), [res[k] for k in cols])
    if res["score"] >= 3:
        c.executemany("INSERT OR REPLACE INTO operator_wallets "
                      "(addr,pool,role,n_tx,net_usd,first_sec,seen_at) "
                      "VALUES (?,?,?,?,?,?,?)",
                      [(x, res["pool"], "launch_bot", v["n"],
                        (v["in"] - v["out"]) * qpx, v["first"] - t0, int(time.time()))
                       for x, v in bots])
    c.commit()


def main():
    init()
    args = sys.argv[1:]
    if "--known" in args:
        c = db.conn()
        print("  %-46s%8s%14s%8s" % ("钱包", "出现币数", "累计净USD", "总笔数"))
        for r in c.execute("SELECT addr, COUNT(*) n, SUM(net_usd) s, SUM(n_tx) t "
                           "FROM operator_wallets GROUP BY addr "
                           "ORDER BY n DESC, s DESC LIMIT 40"):
            print("  %-46s%8d%14s%8d"
                  % (r["addr"], r["n"], "$" + format(r["s"] or 0, ",.0f"), r["t"] or 0))
        return
    for p in [x for x in args if len(x) > 30]:
        out = fingerprint(p)
        if not out:
            print("%s 数据不足" % p[:12])
            continue
        res, bots, t0, qpx = out
        save(res, bots, t0, qpx)
        print("")


if __name__ == "__main__":
    main()
