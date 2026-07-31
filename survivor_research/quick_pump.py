# -*- coding: utf-8 -*-
"""快速通道: 用GT聚合数据看"拉盘幅度"和"钓到的鱼"的关系。

每个池子只花1次API调用,能覆盖几百个样本。虽然拿不到精确的狗庄盈亏,
但能看出方向: 币价拉得越高/成交越活跃, 是不是真的吸引到越多买家。
"""
import calendar, sys, time
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import cg_client as cg

OUT = Path(__file__).parent / "quick_pump.csv"
pools = [l.strip() for l in Path(sys.argv[1]).read_text().splitlines() if len(l.strip()) > 30]
done = set()
if OUT.exists():
    for l in OUT.read_text(encoding="utf-8").splitlines()[1:]:
        done.add(l.split(",")[0])
else:
    OUT.write_text("pool,name,age_h,buyers,sellers,buys,sells,vol24,reserve,fdv,"
                   "chg_h1,chg_h6,chg_h24\n", encoding="utf-8")
todo = [p for p in pools if p not in done]
print(f"待查 {len(todo)} 个 (已有 {len(done)})", flush=True)
for i, P in enumerate(todo, 1):
    d = cg.get(f"networks/solana/pools/{P}")
    if not d:
        continue
    a = d["data"]["attributes"]
    tx = (a.get("transactions") or {}).get("h24") or {}
    ch = a.get("price_change_percentage") or {}
    try:
        age = (time.time() - calendar.timegm(time.strptime(
            (a.get("pool_created_at") or "")[:19], "%Y-%m-%dT%H:%M:%S"))) / 3600
    except ValueError:
        age = 0
    def f(x):
        try: return float(x or 0)
        except (TypeError, ValueError): return 0.0
    row = [P, str(a.get("name") or "").replace(",", " ")[:24], round(age, 2),
           tx.get("buyers") or 0, tx.get("sellers") or 0,
           tx.get("buys") or 0, tx.get("sells") or 0,
           round(f((a.get("volume_usd") or {}).get("h24")), 2),
           round(f(a.get("reserve_in_usd")), 2), round(f(a.get("fdv_usd")), 2),
           f(ch.get("h1")), f(ch.get("h6")), f(ch.get("h24"))]
    with OUT.open("a", encoding="utf-8") as fh:
        fh.write(",".join(str(x) for x in row) + "\n")
    if i % 25 == 0:
        print(f"  {i}/{len(todo)}", flush=True)
print("完成")
