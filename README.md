# 模拟盘 — 操作手册（三个独立账本）

| 账本 | 脚本 | 策略 | 计划任务 | 仪表盘 |
|---|---|---|---|---|
| 打新 | paper_monitor.py | Launchpool/HODLer/TGE/PoolX 估算收益 | PaperLaunchpadMonitor (6h) | DASHBOARD.md |
| 策略A | newcoin_monitor.py | 无差别买入 Gate 新永续，TP+50/SL-15/72h | PaperNewCoinMonitor (1h) | DASHBOARD_NEWCOIN.md |
| 策略B | dip_monitor.py | 五所(LBank/XT/MEXC/HTX/Gate)新币回撤反转：跌≥10%后弹≥5%入场，TP+50/SL-15/72h | PaperDipMonitor (1h) | DASHBOARD_DIP.md |

策略B含占位价伪影防御（基准=首根真实成交K线）和美股代币化合约过滤（*STOCK*）。
LBank/XT 用交易对列表差分检测新上市（首轮播种不产生信号）。

---
（以下为打新账本的原始说明）


## 系统组成
- `paper_monitor.py` — 监控器主程序，每次运行一个周期（扫公告→记事件→计提收益→写 NAV）
- `config.json` — 虚拟资金与可校准的收益假设参数
- `state.json` — 组合状态（仓位、事件台账、上次运行时间）
- `nav.csv` — NAV 时间序列
- `DASHBOARD.md` — 实时仪表盘（每轮重写）
- `monitor.log` — 运行日志

## 自动运行
Windows 计划任务 `PaperLaunchpadMonitor` 每 6 小时执行一次（电脑开机状态下）。
手动跑一轮：

```bash
python "C:\Users\zhang\OneDrive\Desktop\claude_code_ohanism\listing_research\paper\paper_monitor.py"
```

## 组合结构（虚拟 10,000 USDT，2026-07-24 起）
| 仓位 | 金额 | 策略 |
|---|---|---|
| BNB 主仓 | 6,000（10.58 BNB @567，已模拟对冲） | 自动吃 Launchpool / HODLer / Megadrop |
| TGE 机动仓 | 2,500 | 每期 Wallet TGE 模拟申购（commit 3 BNB，fill 3%，uplift 3x） |
| 卫星仓 | 1,500 | Bitget PoolX / CandyBomb；Gate Startup 手动补录 |

## 监控源
- Binance 公告 catalog 48（上新）/ 93（活动）/ 128（空投），关键词 Launchpool/HODLer/Megadrop/TGE
- Bitget 公告 API 全类别，关键词 PoolX/CandyBomb/Launchpool
- 价格：data-api.binance.vision → Gate 回退

## 收益模型（全部标注 estimated，跑出真实事件后校准）
- Launchpool 单期 = 主仓 × 0.4%；HODLer = ×0.2%；Megadrop = ×0.3%
- TGE 单期 = min(3 BNB, 仓位) × 3% 成交率 × (3x − 1)
- PoolX 单期 = 卫星仓 × 0.2%
- 事件公告 7 天后按"到手即卖"结算入账（扣 0.1% 卖出费）

## 校准流程（跑 2-4 周后）
1. 对照 Binance 官方每期 Launchpool 实际发放（币价 × 每 BNB 奖励），替换 `launchpool_yield_per_event`
2. TGE 用实际中签/收益数据替换 fill_rate 和 uplift
3. 校准后 NAV 曲线自动反映新参数

## 手动补录事件（如 Gate Startup）
在 `state.json` 的 `events` 里加一条，参考已有事件格式，`est_payout` 填预估 USDT 收益。
