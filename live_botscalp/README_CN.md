# 策略D 实盘执行器（Solana / 薅机器人羊毛）

## 安全边界（必读）

- 这份代码**从不**把你的私钥发送给任何人，也不会写进日志或提交到git。私钥只从环境变量
  `WALLET_PRIVATE_KEY` 读进当前进程的内存，用完即弃。
- **私钥必须由你自己在自己的电脑/服务器上设置**。我(Claude)不持有你的私钥，不会在
  我这边运行这份代码去下真实单——你部署、你启动、你负责按下"真正开始"这个开关。
- 默认是 **DRY-RUN 模式**（只打印"本来会做什么"，不会真的广播交易）。要下真实单，
  必须同时把 `LIVE_TRADING` 和 `CONFIRM_LIVE_BOTSCALP` 两个环境变量都设置成开启状态
  ——两道开关都要手动打开，防止手滑。
- 三道风控闸门：单笔金额上限(`posSizeUsd`)、每日累计开仓金额上限(`dailyMaxUsd`)、
  每日已实现亏损熔断(`dailyLossKillUsd`，亏到这个数就停止开新仓，但不影响已开仓位
  正常止盈止损平仓)。
- 入场/出场前都会重新拉一次实时报价，不会用几秒钟前的缓存价格下单——这是从别的项目
  真实实盘踩过的坑里学来的教训(用了过期报价下单，等广播出去的时候价格已经跌穿了)。

## 部署步骤

1. 装依赖：
   ```
   pip install -r requirements.txt
   ```

2. 复制 `set_env.example.ps1` 为 `set_env.ps1`（这个文件已经在`.gitignore`里，不会被
   提交），把你的私钥填进去。**这一步只在你自己的电脑上做**。

3. 每次开新的PowerShell窗口，先加载环境变量（注意前面有个点，dot-source）：
   ```
   . .\set_env.ps1
   ```

4. **先跑几轮dry-run**，确认它扫到的候选、判断的入场/出场逻辑符合预期：
   ```
   python live_runner.py
   ```
   这时 `LIVE_TRADING=0`，只会在`live_runner.log`里打印"[DRY-RUN] 本来会 BUY/SELL..."，
   不会花一分钱。

5. 观察dry-run几个小时到一天，确认没有异常（比如报价一直失败、候选质量差等），
   再考虑开真实单。

6. 真正开始下真实单前，把 `config.live.json` 的 `posSizeUsd` 改成很小的金额
   （比如$1），把 `set_env.ps1` 里两个环境变量都改成开启：
   ```
   $env:LIVE_TRADING = "1"
   $env:CONFIRM_LIVE_BOTSCALP = "YES"
   ```
   重新 `. .\set_env.ps1` 加载，再跑 `python live_runner.py`。

7. 用最小金额观察真实成交是否符合预期(`live_orders.jsonl`里能看到每一笔的详情)，
   确认没问题后再逐步调大 `posSizeUsd`。

## 定时运行

跟模拟盘一样，这个策略是"快进快出"逻辑，需要频繁检查持仓才有意义。用Windows计划
任务每2-3分钟跑一次（做法可以参考 `../run_botscalp_local.ps1` 的锁文件+git提交模式，
把里面 `python bot_scalp_monitor.py` 换成 `python live_runner.py`，去掉git提交部分
——实盘的状态文件不建议提交到公开仓库）。

## 文件说明

- `live_runner.py` - 主执行逻辑
- `config.live.json` - 参数配置(仓位/止盈止损/持仓上限/日限额等)
- `set_env.example.ps1` - 环境变量模板，复制成`set_env.ps1`并填入私钥
- `live_state.json` - 运行时生成，记录当前持仓/已实现盈亏(不含私钥，可以提交)
- `live_orders.jsonl` - 每一笔尝试/成交的审计日志(不含私钥，可以提交)
- `live_runner.log` - 运行日志
