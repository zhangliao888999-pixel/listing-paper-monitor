# -*- coding: utf-8 -*-
"""标准尸检报告。2026-08-01建。

用户定的规格: 每个钓鱼币都按同一格式出全量数据,进标准库,便于横向对比。

  1. 狗庄控制多少钱包,每个各多少钱
  2. 总资金规模
  3. 买家何时上钩 —— 时间点分布
  4. 狗庄最终利润 / 成本
  5. 买家逃出来多少 / 被套住多少

三张表:
  coin_report    每个币一行的汇总
  coin_wallets   每个币每个钱包一行(角色/进出/是否逃掉)
  coin_timeline  每个币按分钟的资金流(狗庄撒饵 vs 鱼进场)

"逃出来"的定义: 卖出金额 >= 买入金额,即真的把本金换回来了。没卖出的一律
算被套住,不按最后价格估值 —— 流动性归零的币,账面市值是假的。

两个性能上必须做的事(第一版都踩了):
  - 探测陌生账户要约150次RPC。USWR有3,039个买家,全探要18小时,第一版就是
    这么卡死的。只探"有分量"的账户,其余直接当交易者 —— 要剔除的那几类
    (协议费/托管平台/创建者费)本来就都是高频高额的,不会漏。
  - 解析结果落盘。2万笔要50分钟,调一次参数就重来一遍不可接受。

用法:
  python lab_report.py <pool>          分析并入库,打印报告
  python lab_report.py --show <pool>   只打印已入库的报告
  python lab_report.py --list          列出库里所有报告
"""
import json
import sys
import threading
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
import lab_registry as reg   # noqa: E402

SEP = "|"                  # flow/held 复合键的分隔符; base58 地址不含 |
PROBE_MIN_HITS = 0.02      # 出现在这个比例以上的交易里才值得探测
PROBE_MIN_USD = 200.0      # 或者资金量超过这个

DDL = """
CREATE TABLE IF NOT EXISTS coin_report (
  pool TEXT PRIMARY KEY, name TEXT, mint TEXT, quote TEXT, dex TEXT,
  created_at TEXT, analyzed_at INTEGER,
  n_tx INTEGER, n_wallet INTEGER, life_min REAL, idle_min REAL,
  data_quality TEXT, attribution_ok INTEGER, custodial_share REAL,
  op_n_wallet INTEGER, op_cost_usd REAL, op_out_usd REAL, op_gas_usd REAL,
  op_pnl_usd REAL, op_tok_held REAL, op_top_wallet TEXT,
  total_in_usd REAL, total_out_usd REAL, init_reserve_usd REAL,
  peak_res_usd REAL, end_res_usd REAL, drained_usd REAL,
  fish_n INTEGER, fish_in_usd REAL, fish_out_usd REAL,
  fish_escaped_n INTEGER, fish_escaped_usd REAL,
  fish_trapped_n INTEGER, fish_trapped_usd REAL,
  fish_first_min REAL, fish_median_min REAL, fish_last_min REAL,
  dump_sec REAL, danger_at_dump REAL, ratchet_usd_min REAL, outcome TEXT
);
CREATE TABLE IF NOT EXISTS coin_wallets (
  pool TEXT, addr TEXT, role TEXT, n_tx INTEGER, n_buy INTEGER, n_sell INTEGER,
  in_usd REAL, out_usd REAL, pnl_usd REAL, gas_usd REAL, tok_held REAL,
  first_min REAL, last_min REAL, escaped INTEGER,
  PRIMARY KEY (pool, addr)
);
CREATE TABLE IF NOT EXISTS coin_timeline (
  pool TEXT, minute INTEGER, op_in_usd REAL, op_out_usd REAL,
  fish_in_usd REAL, fish_out_usd REAL, n_new_fish INTEGER, n_tx INTEGER,
  PRIMARY KEY (pool, minute)
);
"""


def init():
    c = db.conn()
    c.executescript(DDL)
    c.commit()


def lookup(a, pool, inn, out, hits, ntx):
    worth = (hits >= max(ntx * PROBE_MIN_HITS, 3)) or (inn + out >= PROBE_MIN_USD)
    return reg.role_of(a, pool=pool, inn=inn, out=out, hits=hits, ntx=ntx,
                       allow_probe=worth)


def _pack(t):
    d = dict(t)
    d["flow"] = {SEP.join(k): v for k, v in t["flow"].items()}
    d["held"] = {SEP.join(k): v for k, v in t["held"].items()}
    return d


def _unpack(d):
    d["flow"] = {tuple(k.split(SEP)): v for k, v in d["flow"].items()}
    d["held"] = {tuple(k.split(SEP)): v for k, v in d["held"].items()}
    return d


def fetch_txs(pool, max_sigs=None):
    """拉全量交易,结果落盘缓存,断了或调参不用重来。"""
    sigs = [s for s in fx.get_signatures(pool, cap=max_sigs)
            if not s["err"] and s.get("ts")]
    cache = HERE / ("parsed_" + pool[:10] + ".jsonl")
    have = {}
    if cache.exists():
        with cache.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    t = _unpack(json.loads(line))
                    have[t["sig"]] = t
                except (ValueError, KeyError):
                    pass
        print("  缓存命中 %d 笔" % len(have), flush=True)
    need = [s for s in sigs if s["sig"] not in have]
    print("  签名 %d 笔,需解析 %d 笔" % (len(sigs), len(need)), flush=True)
    txs = list(have.values())
    if need:
        lk = threading.Lock()
        done = [0]
        fh = cache.open("a", encoding="utf-8")

        def one(s):
            t = fx.parse_tx(s)
            with lk:
                done[0] += 1
                if t:
                    txs.append(t)
                    fh.write(json.dumps(_pack(t)))
                    fh.write("\n")
                if done[0] % 2000 == 0:
                    print("    %d/%d" % (done[0], len(need)), flush=True)
                    fh.flush()
            return t

        with ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(one, need))
        fh.close()
    return txs


def analyze_coin(pool, max_sigs=None):
    """全量取证 + 按用户规格汇总。返回 (report, wallets, timeline)。"""
    info = cg.get("networks/solana/pools/" + pool)
    a = (info or {}).get("data", {}).get("attributes", {}) or {}
    rel = (info or {}).get("data", {}).get("relationships", {}) or {}
    mint = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    mint = mint.replace("solana_", "")
    dex = ((rel.get("dex") or {}).get("data") or {}).get("id", "")

    txs = fetch_txs(pool, max_sigs)
    if len(txs) < 10:
        return None, None, None
    print("  可用 %d 笔,开始分析" % len(txs), flush=True)
    m, wr = fx.analyze(pool, txs, expected=len(txs), role_lookup=lookup)
    if not m:
        return None, None, None
    txs.sort(key=lambda x: x["ts"])
    t0 = txs[0]["ts"]

    wallets = []
    for w in wr:
        wallets.append({
            "addr": w["addr"], "role": w["role"], "n_tx": w["n_tx"],
            "n_buy": w["n_buy"], "n_sell": w["n_sell"],
            "in_usd": w["in_usd"], "out_usd": w["out_usd"],
            "pnl_usd": w["out_usd"] - w["in_usd"],
            "gas_usd": w["gas_usd"], "tok_held": w["tok_held"],
            "first_min": (w["first_ts"] - t0) / 60 if w["first_ts"] else 0,
            "last_min": (w["last_ts"] - t0) / 60 if w["last_ts"] else 0,
            "escaped": 1 if w["out_usd"] >= w["in_usd"] and w["in_usd"] > 0 else 0,
        })
    op_w = [w for w in wallets if w["role"] in ("operator", "creator", "cluster")]
    fish_w = [w for w in wallets if w["role"] == "fish"]
    esc = [w for w in fish_w if w["escaped"]]
    trap = [w for w in fish_w if not w["escaped"] and w["in_usd"] > 0]
    fm = sorted(w["first_min"] for w in fish_w if w["in_usd"] > 0)

    op_set = set(w["addr"] for w in op_w)
    idx = dict((w["addr"], w) for w in wallets)
    tl = defaultdict(lambda: {"op_in": 0.0, "op_out": 0.0, "fish_in": 0.0,
                              "fish_out": 0.0, "new_fish": 0, "n": 0})
    seen_fish = set()
    for t in txs:
        s = t["signer"]
        w = idx.get(s)
        if not w:
            continue
        b = tl[int((t["ts"] - t0) / 60)]
        b["n"] += 1
        if (s not in op_set and w["role"] == "fish"
                and s not in seen_fish and w["in_usd"] > 0):
            seen_fish.add(s)
            b["new_fish"] += 1
    for w in wallets:
        b = tl[int(w["first_min"])]
        if w["addr"] in op_set:
            b["op_in"] += w["in_usd"]
            b["op_out"] += w["out_usd"]
        elif w["role"] == "fish":
            b["fish_in"] += w["in_usd"]
            b["fish_out"] += w["out_usd"]

    rep = {
        "pool": pool, "name": a.get("name"), "mint": mint,
        "quote": m["quote_sym"], "dex": dex,
        "created_at": a.get("pool_created_at"), "analyzed_at": int(time.time()),
        "n_tx": m["n_tx"], "n_wallet": m["n_wallet"], "life_min": m["life_min"],
        "idle_min": m["idle_min"], "data_quality": m.get("data_quality"),
        "attribution_ok": m.get("attribution_ok", 1),
        "custodial_share": m.get("custodial_share", 0),
        "op_n_wallet": len(op_w), "op_cost_usd": m["op_cost_usd"],
        "op_out_usd": m["op_out_usd"], "op_gas_usd": m["op_gas_usd"],
        "op_pnl_usd": m["op_pnl_usd"], "op_tok_held": m["op_tok_held"],
        "op_top_wallet": m["op_addr"],
        "total_in_usd": sum(w["in_usd"] for w in wallets),
        "total_out_usd": sum(w["out_usd"] for w in wallets),
        "init_reserve_usd": m.get("init_reserve_usd", 0),
        "peak_res_usd": m["peak_res_usd"], "end_res_usd": m["end_res_usd"],
        "drained_usd": m["drained_usd"],
        "fish_n": len(fish_w), "fish_in_usd": m["fish_in_usd"],
        "fish_out_usd": m["fish_out_usd"],
        "fish_escaped_n": len(esc),
        "fish_escaped_usd": sum(w["out_usd"] for w in esc),
        "fish_trapped_n": len(trap),
        "fish_trapped_usd": sum(w["in_usd"] - w["out_usd"] for w in trap),
        "fish_first_min": fm[0] if fm else None,
        "fish_median_min": fm[len(fm) // 2] if fm else None,
        "fish_last_min": fm[-1] if fm else None,
        "dump_sec": m["dump_sec"],
        "danger_at_dump": (m["fish_in_usd"] / m["op_cost_usd"]
                           if m["op_cost_usd"] > 1 else None),
        "ratchet_usd_min": m["ratchet_usd_min"], "outcome": m["outcome"],
    }
    timeline = [{"minute": k, "op_in_usd": v["op_in"], "op_out_usd": v["op_out"],
                 "fish_in_usd": v["fish_in"], "fish_out_usd": v["fish_out"],
                 "n_new_fish": v["new_fish"], "n_tx": v["n"]}
                for k, v in sorted(tl.items())]
    return rep, wallets, timeline


def save(rep, wallets, timeline):
    c = db.conn()
    cols = list(rep)
    c.execute("INSERT OR REPLACE INTO coin_report (%s) VALUES (%s)"
              % (",".join(cols), ",".join("?" * len(cols))),
              [rep[k] for k in cols])
    c.execute("DELETE FROM coin_wallets WHERE pool=?", (rep["pool"],))
    c.executemany("INSERT INTO coin_wallets (pool,addr,role,n_tx,n_buy,n_sell,"
                  "in_usd,out_usd,pnl_usd,gas_usd,tok_held,first_min,last_min,"
                  "escaped) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  [(rep["pool"], w["addr"], w["role"], w["n_tx"], w["n_buy"],
                    w["n_sell"], w["in_usd"], w["out_usd"], w["pnl_usd"],
                    w["gas_usd"], w["tok_held"], w["first_min"], w["last_min"],
                    w["escaped"]) for w in wallets])
    c.execute("DELETE FROM coin_timeline WHERE pool=?", (rep["pool"],))
    c.executemany("INSERT INTO coin_timeline (pool,minute,op_in_usd,op_out_usd,"
                  "fish_in_usd,fish_out_usd,n_new_fish,n_tx) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  [(rep["pool"], t["minute"], t["op_in_usd"], t["op_out_usd"],
                    t["fish_in_usd"], t["fish_out_usd"], t["n_new_fish"],
                    t["n_tx"]) for t in timeline])
    c.commit()


def money(v):
    return "$" + format(v or 0, ",.2f")


def show(pool):
    c = db.conn()
    r = c.execute("SELECT * FROM coin_report WHERE pool=?", (pool,)).fetchone()
    if not r:
        print("库里没有这个币的报告")
        return
    W = "=" * 78
    print(W)
    print("  %s   %s" % (r["name"], pool))
    print(W)
    print("  发行 %s   %s   计价 %s" % (r["created_at"], r["dex"], r["quote"]))
    print("  存活 %.1f小时   交易 %s笔   钱包 %d个   静止 %.0f分钟"
          % (r["life_min"] / 60, format(r["n_tx"], ","), r["n_wallet"],
             r["idle_min"]))
    print("  数据质量 %s   归属可信 %s   托管平台占比 %.0f%%"
          % (r["data_quality"], "是" if r["attribution_ok"] else "否",
             (r["custodial_share"] or 0) * 100))

    print("")
    print("  【1】狗庄控制的钱包")
    print("    共 %d 个,主力 %s" % (r["op_n_wallet"], str(r["op_top_wallet"])[:44]))
    rows = c.execute("SELECT * FROM coin_wallets WHERE pool=? AND role!='fish' "
                     "ORDER BY in_usd DESC LIMIT 15", (pool,)).fetchall()
    print("    %-16s%-10s%6s%13s%13s%13s%8s"
          % ("钱包", "角色", "笔数", "投入", "取出", "净", "进场"))
    for w in rows:
        print("    %-16s%-10s%6d%13s%13s%13s%7.0f分"
              % (w["addr"][:14], w["role"], w["n_tx"], money(w["in_usd"]),
                 money(w["out_usd"]), money(w["pnl_usd"]), w["first_min"]))

    print("")
    print("  【2】资金规模")
    for lbl, v in (("全场投入", r["total_in_usd"]),
                   ("全场取出", r["total_out_usd"]),
                   ("建池铺底", r["init_reserve_usd"]),
                   ("池子峰值", r["peak_res_usd"]),
                   ("最终抽走", r["drained_usd"])):
        print("    %-10s %15s" % (lbl, money(v)))

    print("")
    print("  【3】买家上钩的时间点")
    if r["fish_first_min"] is not None:
        print("    第一条鱼   第 %.0f 分钟" % r["fish_first_min"])
        print("    中位       第 %.0f 分钟" % r["fish_median_min"])
        print("    最后一条   第 %.0f 分钟" % r["fish_last_min"])
    tl = c.execute("SELECT * FROM coin_timeline WHERE pool=? AND "
                   "(n_new_fish>0 OR fish_in_usd>0) ORDER BY minute",
                   (pool,)).fetchall()
    if tl:
        peak = max(x["fish_in_usd"] for x in tl) or 1
        print("    %6s%10s%13s  分布" % ("分钟", "新增买家", "鱼投入"))
        step = max(len(tl) // 25, 1)
        for x in tl[::step]:
            bar = "#" * int(x["fish_in_usd"] / peak * 32)
            print("    %6d%10d%13s  %s"
                  % (x["minute"], x["n_new_fish"], money(x["fish_in_usd"]), bar))

    print("")
    print("  【4】狗庄的账")
    print("    投入买货   %15s" % money(r["op_cost_usd"]))
    print("    gas/优先费 %15s" % money(r["op_gas_usd"]))
    print("    砸盘拿回   %15s" % money(r["op_out_usd"]))
    print("    " + "-" * 42)
    print("    净盈亏     %15s" % money(r["op_pnl_usd"]))
    print("    手里还压着 %15s 个币" % format(r["op_tok_held"] or 0, ",.0f"))
    print("    撒饵速度   %15s/分钟" % money(r["ratchet_usd_min"]))
    if r["danger_at_dump"] is not None:
        print("    收网时危险度 %13.2f  (鱼的钱/他的成本)" % r["danger_at_dump"])
    print("    砸盘持续   %15.0f 秒" % (r["dump_sec"] or 0))

    print("")
    print("  【5】买家")
    print("    共 %d 个   投入 %s   取出 %s"
          % (r["fish_n"], money(r["fish_in_usd"]), money(r["fish_out_usd"])))
    print("    逃出来的   %4d 个   拿回 %s"
          % (r["fish_escaped_n"], money(r["fish_escaped_usd"])))
    print("    被套住的   %4d 个   亏掉 %s"
          % (r["fish_trapped_n"], money(r["fish_trapped_usd"])))
    if r["fish_n"]:
        print("    逃脱率     %4.0f%%" % (r["fish_escaped_n"] / r["fish_n"] * 100))
    print("")
    print("  结局判定: %s" % r["outcome"])


def main():
    args = sys.argv[1:]
    init()
    reg.init()
    if "--list" in args:
        c = db.conn()
        print("  %-20s%7s%13s%13s%13s%8s"
              % ("币", "存活h", "狗庄成本", "鱼投入", "狗庄盈亏", "逃脱率"))
        for r in c.execute("SELECT * FROM coin_report ORDER BY analyzed_at DESC"):
            esc = r["fish_escaped_n"] / r["fish_n"] if r["fish_n"] else 0
            print("  %-20s%7.1f%13s%13s%13s%7.0f%%"
                  % (str(r["name"])[:18], r["life_min"] / 60,
                     money(r["op_cost_usd"]), money(r["fish_in_usd"]),
                     money(r["op_pnl_usd"]), esc * 100))
        return
    if "--show" in args:
        show(args[args.index("--show") + 1])
        return
    for p in [a for a in args if len(a) > 30]:
        print("分析 %s" % p, flush=True)
        rep, wallets, timeline = analyze_coin(p)
        if not rep:
            print("  引擎拒绝(覆盖率或历史不完整)")
            continue
        save(rep, wallets, timeline)
        print("")
        show(p)


if __name__ == "__main__":
    main()
