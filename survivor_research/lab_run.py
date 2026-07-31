# -*- coding: utf-8 -*-
"""狗庄研究实验室 - 采集编排。2026-07-31建。

两件事循环做:
  发现 —— 从GeckoTerminal扫Solana新池子,登记进库(这步花API额度,一小时一次)
  取证 —— 从库里领待办池子,拉全量链上流水做分析(这步纯Solana RPC,免费)

设计上的几个考虑:
  - 取证是网络IO密集不是CPU密集,开多线程不会像之前跑回测那样把本机烤到99度
  - 优先处理交易量小的池子: 它们跑得快,而且"没钓到鱼的冷清盘"恰恰是我们
    最需要的样本(DISNEY只有303笔)。交易量大的标记成big单独跑,不拖慢流水线
  - 全部状态在SQLite里,随时Ctrl+C随时接着跑

用法:
  python lab_run.py              持续跑(发现+取证)
  python lab_run.py --once       只跑一轮
  python lab_run.py --discover   只发现新样本
  python lab_run.py --big        只处理交易量大的池子
"""
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import cg_client as cg
import lab_db as db
import lab_forensics as fx

DISCOVER_PAGES = 5          # 每轮扫多少页新池子(每页20个)
DISCOVER_GAP = 3600         # 发现的间隔(秒),控制API额度消耗
WORKERS = int(__import__("os").environ.get("LAB_WORKERS", "2"))
_p = threading.Lock()


# 输出统一走UTF-8。有的币故意在名字里塞 U+202E(从右到左覆盖)这类字符,
# 默认GBK控制台直接抛 UnicodeEncodeError 把整个池子的处理打断。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def log(*a):
    with _p:
        print(f"[{time.strftime('%m-%d %H:%M:%S')}]", *a, flush=True)


def discover():
    """扫新池子登记进库。只登记,不分析。"""
    n_new = 0
    for page in range(1, DISCOVER_PAGES + 1):
        d = cg.get("networks/solana/new_pools", {"page": page})
        rows = (d or {}).get("data", [])
        if not rows:
            break
        for r in rows:
            a = r.get("attributes", {})
            rel = r.get("relationships", {})
            addr = a.get("address")
            if not addr:
                continue
            mint = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
            dex = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
            before = db.conn().execute("SELECT 1 FROM pools WHERE addr=?", (addr,)).fetchone()
            db.add_pool(addr, mint=mint.replace("solana_", ""), name=a.get("name"),
                        dex=dex, created_at=a.get("pool_created_at"),
                        found_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            if not before:
                n_new += 1
        time.sleep(0.3)
    log(f"发现: 新增 {n_new} 个样本, 库存 {db.counts()}")
    return n_new


def process(addr, allow_big=False):
    """对一个池子做完整取证。"""
    try:
        sigs = fx.get_signatures(addr, cap=None if allow_big else fx.MAX_FETCH + 200)
        ok = [s for s in sigs if not s["err"] and s.get("ts")]
        db.save_pool_meta(addr, n_sig=len(sigs),
                          first_ts=ok[0]["ts"] if ok else None,
                          last_ts=ok[-1]["ts"] if ok else None)
        if len(ok) < 5:
            db.set_status(addr, "skip", "交易太少")
            return "skip"
        if len(ok) > fx.MAX_FETCH and not allow_big:
            db.set_status(addr, "big", f"{len(ok)}笔,留给专门批次")
            return "big"

        txs = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            for t in ex.map(fx.parse_tx, ok):
                if t:
                    txs.append(t)
        if len(txs) < 5:
            db.set_status(addr, "failed", "明细拉不到")
            return "failed"

        m, rows = fx.analyze(addr, txs, expected=len(ok))
        if not m:
            db.set_status(addr, "pending",
                          f"覆盖率不足({len(txs)}/{len(ok)}),等RPC缓过来重试")
            return "retry"
        db.save_metrics(addr, m)
        db.save_wallets(addr, rows)
        db.save_pool_meta(addr, quote_mint=m["quote_sym"])
        db.set_status(addr, "done")
        log(f"完成 {addr[:10]}..  {m['n_tx']}笔/{m['n_wallet']}钱包  "
            f"集中度{m['top_share']:.0%}  狗庄成本${m['op_cost_usd']:,.0f}  "
            f"鱼${m['fish_in_usd']:,.0f}  盈亏${m['op_pnl_usd']:+,.0f}  [{m['outcome']}]")
        return m["outcome"]
    except Exception as e:
        db.set_status(addr, "failed", f"{type(e).__name__}: {e}"[:300])
        log(f"出错 {addr[:10]}..  {type(e).__name__}: {e}")
        if "--debug" in sys.argv:
            traceback.print_exc()
        return "failed"


def work_loop(allow_big=False, once=False):
    """从库里不断领任务处理。"""
    def worker():
        while True:
            addr = None
            if allow_big:
                c = db.conn()
                r = c.execute("SELECT addr FROM pools WHERE status='big' LIMIT 1").fetchone()
                if r:
                    addr = r["addr"]
                    db.set_status(addr, "running")
            else:
                addr = db.claim_pool()
            if not addr:
                return
            process(addr, allow_big)
    ths = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()


def main():
    db.init()
    if "--discover" in sys.argv:
        discover(); return
    if "--big" in sys.argv:
        log(f"处理大池子, {WORKERS}线程")
        work_loop(allow_big=True)
        return

    last_disc = 0.0
    while True:
        if time.time() - last_disc > DISCOVER_GAP:
            try:
                discover()
            except Exception as e:
                log(f"发现失败: {e}")
            last_disc = time.time()
        pend = db.counts().get("pending", 0)
        if pend:
            log(f"开始取证, 待办 {pend} 个, {WORKERS}线程")
            work_loop()
        c = db.counts()
        log(f"本轮结束 {c}  Helius用量 {fx.usage_report()}")
        if "--once" in sys.argv:
            return
        time.sleep(120)


if __name__ == "__main__":
    main()
