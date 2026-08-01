# -*- coding: utf-8 -*-
"""四个被侦察模块标记的币,现在各是什么结局。

用户观察: MOKI/ASTEROID/CONTRA 都收网了,只有 Jimothy 还活着。
如果属实,说明四条判据能识别"大网",但大网也会快速收网 —— 那么"埋伏"
策略的窗口比预想的窄得多,必须再找出区分"快收"和"长期经营"的特征。
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
import lab_forensics as fx   # noqa: E402
import lab_launch as ll      # noqa: E402

COINS = [
    ("CWDm1aHzdaziXJJvGnHrxanD47fQbAJmmwnqUpUkRaoy", "MOKI", "4/4"),
    ("9B7AApg26wNQxckZEcvhePKjZfy8ETJ9kpFtDHwdtmQw", "Jimothy", "4/4"),
    ("Rnazy9ZTv44v3QR6kkPZu5JQUpT7u1JTCdJ4AddYs5Q", "CONTRA", "3/4"),
    ("HhNirUmCxXx8rjkxseQWBDucnwNwUbYaXtce1R4tCFrV", "ASTEROID", "3/4"),
]
SOL = 75.0


def state(pool, name, score):
    a = cg.get("networks/solana/pools/" + pool)
    if not a:
        print("  %-10s 查不到" % name)
        return None
    a = a["data"]["attributes"]
    tx = (a.get("transactions") or {}).get("h24") or {}
    ch = a.get("price_change_percentage") or {}

    def f(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0

    sigs = [s for s in fx.get_signatures(pool) if not s["err"] and s.get("ts")]
    idle = (time.time() - sigs[-1]["ts"]) / 60 if sigs else 0
    life = (sigs[-1]["ts"] - sigs[0]["ts"]) / 60 if len(sigs) > 1 else 0
    return {"name": name, "score": score, "pool": pool,
            "res": f(a.get("reserve_in_usd")), "vol": f((a.get("volume_usd") or {}).get("h24")),
            "fdv": f(a.get("fdv_usd")), "h1": f(ch.get("h1")), "h6": f(ch.get("h6")),
            "h24": f(ch.get("h24")), "buys": tx.get("buys") or 0,
            "buyers": tx.get("buyers") or 0, "sells": tx.get("sells") or 0,
            "sellers": tx.get("sellers") or 0, "n_sig": len(sigs),
            "idle": idle, "life": life, "sigs": sigs}


def money_split(pool, sigs, cap=6000):
    """金库净额口径拆分。返回 (流入, 流出, 外部钱包明细)。"""
    use = sigs if len(sigs) <= cap else sigs[:cap]
    txs = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for t in ex.map(fx.parse_tx, use):
            if t:
                txs.append(t)
    if len(txs) < 10:
        return None
    txs.sort(key=lambda x: x["ts"])
    quote = ll.detect_quote(txs)
    qpx = fx.QUOTES[quote][1]
    vault = ll.find_vault(txs, pool, quote)
    if not vault:
        return None
    t0 = txs[0]["ts"]
    per = defaultdict(lambda: {"in": 0.0, "out": 0.0, "n": 0, "first": None})
    vin = vout = 0.0
    for t in txs:
        vd = ll.qdelta(t, vault, quote)
        if abs(vd) < 1e-9:
            continue
        src = (t["sol_delta"].items() if quote == fx.WSOL
               else [(o, v) for (o, m), v in t["flow"].items() if m == quote])
        best, bd = None, 0.0
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
            vin += vd
        else:
            p["out"] += -vd
            vout += -vd
    return {"vin": vin * qpx, "vout": vout * qpx, "per": per, "t0": t0,
            "qpx": qpx, "quote": fx.QUOTES[quote][0], "n_tx": len(txs)}


def main():
    print("=" * 92)
    print("  四个被标记的币,现在的结局")
    print("=" * 92)
    print("  %-10s%6s%9s%11s%11s%9s%9s%8s%8s"
          % ("币", "评分", "存活分", "流动性", "24h量", "1h涨跌", "24h涨跌", "买家", "卖家"))
    got = []
    for pool, name, score in COINS:
        s = state(pool, name, score)
        if not s:
            continue
        got.append(s)
        print("  %-10s%6s%9.0f%11s%11s%8.0f%%%8.0f%%%8d%8d"
              % (name, score, s["life"], "$" + format(s["res"], ",.0f"),
                 "$" + format(s["vol"], ",.0f"), s["h1"], s["h24"],
                 s["buyers"], s["sellers"]))
    print("")
    for s in got:
        print("=" * 92)
        print("  %s  (%s)  %d笔交易  静止%.0f分钟" % (s["name"], s["score"], s["n_sig"], s["idle"]))
        print("=" * 92)
        m = money_split(s["pool"], s["sigs"])
        if not m:
            print("    解析不足")
            continue
        per, t0, qpx = m["per"], m["t0"], m["qpx"]
        early = {x for x, v in per.items()
                 if v["first"] and v["first"] - t0 <= 180 and v["n"] >= 5}
        late = {x: v for x, v in per.items() if x not in early}
        e_in = sum(per[x]["in"] for x in early) * qpx
        e_out = sum(per[x]["out"] for x in early) * qpx
        l_in = sum(v["in"] for v in late.values()) * qpx
        l_out = sum(v["out"] for v in late.values()) * qpx
        print("    金库(%s口径) 流入 $%s  流出 $%s  净 $%s   解析%d笔"
              % (m["quote"], format(m["vin"], ",.0f"), format(m["vout"], ",.0f"),
                 format(m["vin"] - m["vout"], ",.0f"), m["n_tx"]))
        print("    开盘3分钟内的 %d 个钱包: 投入 $%s  取出 $%s  净 $%s"
              % (len(early), format(e_in, ",.0f"), format(e_out, ",.0f"),
                 format(e_in - e_out, ",.0f")))
        print("    之后进场的 %d 个钱包:   投入 $%s  取出 $%s  净 $%s"
              % (len(late), format(l_in, ",.0f"), format(l_out, ",.0f"),
                 format(l_in - l_out, ",.0f")))
        sizes = sorted(v["in"] * qpx for v in late.values())
        real = [x for x in sizes if x >= 10]
        print("    后进场里投入>=$10的: %d 个,合计 $%s   <- 这才是真鱼"
              % (len(real), format(sum(real), ",.0f")))
        big = sorted(late.items(), key=lambda kv: -(kv[1]["in"]))[:6]
        for x, v in big:
            if v["in"] * qpx < 5:
                continue
            print("      %-14s 第%.1f分钟  投$%s 取$%s  净$%s  %d笔"
                  % (x[:12], (v["first"] - t0) / 60,
                     format(v["in"] * qpx, ",.0f"), format(v["out"] * qpx, ",.0f"),
                     format((v["in"] - v["out"]) * qpx, ",.0f"), v["n"]))


if __name__ == "__main__":
    main()
