# -*- coding: utf-8 -*-
"""开盘侦察 + 惯犯监控。2026-08-01建。两个模块合在一个进程里跑。

模块A 开盘侦察: 扫3-8分钟的新币,算开盘指纹四条,评分>=3的判为"大网"。
  1. 头2分钟净流入 >= $5,000
  2. 30秒窗口内同时启动 >= 8 个钱包
  3. 同秒交易占比 >= 30%
  4. 至少1笔 >= $1,000 的铺底单
  实测区分度: DeepSeek4 4/4($11,726) 活11小时涨10倍;
              Speed 3/4($7,686) 在跑; GDWR 2/4($1,333) 已归零-99.996%。

模块B 惯犯监控: 已入库的作案钱包一旦出现在新币开盘,立刻报警。
  这条已经验证有效 —— DISNEY 的操盘钱包 GbTRN4aKUdaA 后来又出现在
  Speed/USDC 上开新盘,手法一致(USDC计价、一笔大额铺底、单钱包高频撒饵)。

成本控制: 指纹要拉+解析约200笔交易,Helius限速约10请求/秒,所以先用GT的
成交额做预筛(大网币开盘2分钟就有几千刀成交),每轮最多指纹6个。

用法: python lab_scout.py
"""
import calendar
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import cg_client as cg       # noqa: E402
import lab_db as db          # noqa: E402
import lab_launch as ll      # noqa: E402

MIN_AGE = 3 * 60         # 太新的交易还不够判
MAX_AGE = 10 * 60        # 太老的错过窗口
MIN_VOL_PREFILTER = 1500  # GT成交额预筛,省RPC
MAX_FP_PER_ROUND = 6     # 每轮最多指纹几个
CYCLE = 150              # 每轮间隔秒

EXTRA_DDL = """
CREATE TABLE IF NOT EXISTS scout_alerts (
  ts INTEGER, pool TEXT, name TEXT, kind TEXT, detail TEXT,
  PRIMARY KEY (ts, pool, kind)
);
"""


def log(*a):
    print("[%s]" % time.strftime("%m-%d %H:%M:%S"), *a, flush=True)


def age_min(a):
    c = a.get("pool_created_at") or ""
    try:
        return (time.time() - calendar.timegm(
            time.strptime(c[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def fresh_pools():
    """GT 的 new_pools 只覆盖 0-7 分钟,正好是我们要的窗口。"""
    out = []
    for page in (1, 2, 3):
        d = cg.get("networks/solana/new_pools", {"page": page})
        for r in (d or {}).get("data", []):
            a = r.get("attributes", {})
            addr = a.get("address")
            sec = age_min(a)
            if not addr or sec is None:
                continue
            if not (MIN_AGE <= sec <= MAX_AGE):
                continue
            vol = 0.0
            for k in ("h1", "h24"):
                try:
                    vol = max(vol, float((a.get("volume_usd") or {}).get(k) or 0))
                except (TypeError, ValueError):
                    pass
            out.append((addr, a.get("name") or "", sec, vol))
        time.sleep(0.25)
    return out


def alert(c, pool, name, kind, detail):
    c.execute("INSERT OR REPLACE INTO scout_alerts (ts,pool,name,kind,detail) "
              "VALUES (?,?,?,?,?)", (int(time.time()), pool, name, kind, detail))
    c.commit()
    log("*** %s *** %s  %s" % (kind, name or pool[:12], detail))


def main():
    db.init()
    ll.init()
    c = db.conn()
    c.executescript(EXTRA_DDL)
    c.commit()
    known = {r["addr"] for r in c.execute("SELECT DISTINCT addr FROM operator_wallets")}
    log("侦察启动。已知作案钱包 %d 个" % len(known))
    log("判据: 头2分钟>=$%d, 同时启动>=%d个, 同秒>=%d%%, 铺底>=$%d"
        % (ll.MIN_CAP2, ll.MIN_BURST, ll.MIN_SAMESEC * 100, ll.MIN_SEED))

    while True:
        try:
            cands = fresh_pools()
            done = {r["pool"] for r in c.execute("SELECT pool FROM launch_fp")}
            todo = [x for x in cands if x[0] not in done]
            todo.sort(key=lambda x: -x[3])       # 成交额大的先看
            hot = [x for x in todo if x[3] >= MIN_VOL_PREFILTER][:MAX_FP_PER_ROUND]
            log("窗口内新币 %d 个,未指纹 %d 个,本轮检查 %d 个"
                % (len(cands), len(todo), len(hot)))
            for addr, name, sec, vol in hot:
                try:
                    out = ll.fingerprint(addr, verbose=False)
                except Exception as e:
                    log("  %s 指纹失败 %s" % (name[:14], type(e).__name__))
                    continue
                if not out:
                    continue
                res, bots, t0, qpx = out
                ll.save(res, bots, t0, qpx)
                tag = "%d/4 %s  头2分钟$%s 同时%d个 同秒%.0f%% 铺底$%s" % (
                    res["score"], res["verdict"],
                    format(res["cap_2min"], ",.0f"), res["burst_wallets"],
                    res["samesec_ratio"] * 100, format(res["seed_max"], ",.0f"))
                if res["score"] >= 3:
                    alert(c, addr, res["name"], "大网", tag)
                    for x, _ in bots:
                        known.add(x)
                else:
                    log("  %s  %s" % ((res["name"] or addr[:12])[:18], tag))
                # 惯犯
                hit = [x for x, _ in bots if x in known]
                if hit:
                    alert(c, addr, res["name"], "惯犯出现",
                          "已知作案钱包 %d 个: %s" % (len(hit), hit[0][:20]))
        except Exception as e:
            log("本轮出错 %s: %s" % (type(e).__name__, e))
        time.sleep(CYCLE)


if __name__ == "__main__":
    main()
