# -*- coding: utf-8 -*-
"""一次性修正脚本 v2：把live_state.json里CXMT那几笔被坏报价数据污染的平仓记录，
改成Solscan上逐笔核实过的真实卖出到账金额。

背景：GeckoTerminal对CXMT/SOL这个池子返回的价格每次读到的都不一样、差好几个
数量级(同一小时内先后读到过$0.00006/$0.32/$0.045/$0.12/$0.16这种完全不搭边的
"价格")，导致try_exit()算出来的止盈止损比例和部分pnl_usd是假的。链上交易本身
没问题，钱是安全的，只是记录的数字被污染了。这一版按sell_sig精确匹配(不是按
名字，因为记录里存的是"CXMT / SOL"不是"CXMT")，每笔都用用户在Solscan上核对过
的真实到账金额来改。

用法：在VPS上，跟live_state.json同一个文件夹下运行：
    python fix_cxmt_state.py
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
STATE_F = HERE / "live_state.json"

# sell_sig -> Solscan核实的真实到账USD金额
REAL_PROCEEDS_USD = {
    "nZ8h2BBeQLnPd3nAzydrub7WBEyKfs4pYHRpmAJ6fmAUt4LwDyHcaTvXPaUhmKAT2TrBvwErW7gzzFxoBRC4k2Y": 0.008332,
    "4P5sWuwnHqJhpV5e7x3QxWLkrVkwfBhNHBZYMTkSW29c51WS1TxPtYZb7WrHJJmiLoWH3aLPTN5gGwoWYT9GJNis": 0.001464,
    "3JizAG6bpVQLkczRfoPsKBmLj3h4vyvHUrckmfThgsHBiA1rhym6g1KCWTqFJfUDNcJGspgTQUuT5xXCerXhruA4": 0.009038,
    "4ASyy4c2U89Ekn9J3ey3r1FNWd9BEY6NHtPEUEhKyAR7BRUuXSab1b2cFqBFN3Uz1LNShsdViNpWteyP5ugCtGgC": 0.2539,
}

def main():
    state = json.loads(STATE_F.read_text(encoding="utf-8"))
    fixed = 0
    for rec in state.get("closed", []):
        sig = rec.get("sell_sig")
        if sig in REAL_PROCEEDS_USD:
            cost = rec.get("usd", 1.0)
            old_pnl = rec["pnl_usd"]
            new_pnl = round(REAL_PROCEEDS_USD[sig] - cost, 4)
            if abs(new_pnl - old_pnl) < 0.0001:
                print(f"{rec['name']} sig={sig[:8]}... 本来就准(旧pnl={old_pnl} 新pnl={new_pnl})，跳过")
                continue
            print(f"修正 {rec['name']} sig={sig[:8]}...: 旧pnl_usd={old_pnl} -> 新pnl_usd={new_pnl}")
            state["realized_pnl_usd"] = round(state["realized_pnl_usd"] - old_pnl + new_pnl, 4)
            rec["pnl_usd"] = new_pnl
            rec["_corrected_note"] = "原始记录被GeckoTerminal坏报价污染,已用Solscan真实到账金额修正"
            fixed += 1
    if fixed == 0:
        print("没有需要修正的记录。")
    STATE_F.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n共修正{fixed}条记录。最终 realized_pnl_usd = {state['realized_pnl_usd']}")

if __name__ == "__main__":
    main()
