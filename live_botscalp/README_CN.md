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
- **这个文件夹完全自包含**：候选币数据直接从GitHub在线拉取(跟看盘页面同一份公开数据)，
  刷量机器人检测逻辑也内联在`live_runner.py`里，不需要跟`paper/`仓库其它文件放在一起，
  可以单独打包、单独部署到任何一台机器上。

## 仓位模式：先小额验证，确认没问题再切换成跟纸盘一样的动态仓位

`config.live.json` 里的 `sizingMode` 两个选项：

- `"fixed"`（**默认，从这个开始**）：每笔固定 `posSizeUsd` 金额，出厂设置是 **$1**，
  专门用来验证整条链路(报价/签名/广播/确认/止盈止损)能不能真实跑通，不追求盈利。
- `"pct_of_bot"`：跟纸盘`bot_scalp_monitor.py`同一套逻辑——仓位=目标机器人钱包场均
  单笔交易金额的`pctOfBot`(默认10%)，下限`minPosUsd`(默认$5)上限`maxPosUsd`(默认$50)。

**建议流程**：先用`fixed`+`$1`跑几笔真实成交，在`live_orders.jsonl`里确认买入卖出都
按预期执行、价格和金额都合理，没有异常报错，再把`config.live.json`里的
`"sizingMode"` 改成 `"pct_of_bot"`——不需要重新部署代码，改完这一个字段、
下一轮定时任务自动生效。

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

## 部署到Windows VPS（美东这类云服务器，Windows Server 2019）

家里网络不稳定的话，实盘建议放VPS上跑，24小时不间断。这个文件夹**完全自包含**
(候选数据在线拉取，刷量检测逻辑已内联)，不需要git、不需要跟其它文件一起搬，
直接把整个`live_botscalp`文件夹传到VPS上解压就行。步骤：

1. VPS上装好 Python 3(3.9+)。

2. 把这个`live_botscalp`文件夹整个传到VPS上(压缩包上传解压，或者任何你习惯的
   传文件方式)，然后：
   ```
   cd live_botscalp
   pip install -r requirements.txt
   ```

3. 复制 `set_env.example.ps1` 为 `set_env.ps1` 并填入私钥。**这一步在VPS上做，
   私钥留在VPS的这个文件里，不会经过Claude、不会传到任何地方**。

4. 先手动跑一次确认没问题：
   ```
   powershell -File .\run_live_vps.ps1
   ```
   默认dry-run(`config.live.json`里`sizingMode`是`fixed`、`posSizeUsd`是$1)，
   看`live_runner.log`确认候选扫描、报价获取都正常，`live_orders.jsonl`里能看到
   `"status": "dry_run"`的记录。

5. 用Windows计划任务让它每2-3分钟自动跑一次（在VPS的PowerShell里执行，用管理员权限）：
   ```
   $action = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"C:\完整路径\live_botscalp\run_live_vps.ps1`""
   schtasks /create /tn "LiveBotscalp" /tr $action /sc minute /mo 3 /f
   ```
   (路径换成VPS上实际解压的文件夹路径)

6. 确认dry-run跑了几轮都正常之后，把`set_env.ps1`里`LIVE_TRADING`改成`"1"`、
   `CONFIRM_LIVE_BOTSCALP`改成`"YES"`，用$1固定仓位跑几笔真实成交，检查
   `live_orders.jsonl`里的成交明细符合预期。

7. 确认$1没问题后，把`config.live.json`的`"sizingMode"`改成`"pct_of_bot"`，
   仓位就会变成跟纸盘一样的"机器人单笔金额的10%"逻辑（$5-$50区间）——这时候
   注意`dailyMaxUsd`(默认$50)可能需要相应调大，否则单笔仓位涨上去之后一天
   跑不了几笔就顶到每日上限了。

**关于实盘交易记录**：这个包默认不会把`live_state.json`/`live_orders.jsonl`
往任何地方推送，纯粹是VPS本地文件，你可以随时SSH进去看，或者用文件传输工具
下载下来自己查看。

## 本地电脑(非VPS)运行

跟部署到VPS的步骤基本一样，把上面的`run_live_vps.ps1`换成任意路径运行、计划任务名
换一个即可；也可以参考`../run_botscalp_local.ps1`的写法自己改一份。

## 文件说明

- `live_runner.py` - 主执行逻辑
- `run_live_vps.ps1` - Windows(含VPS)定时运行的包装脚本(锁文件+git pull+不自动push实盘数据)
- `run_live_vps.sh` - Linux版本(如果以后换成Linux VPS用这个，本项目当前VPS是Windows Server 2019，用不上)
- `config.live.json` - 参数配置(仓位/止盈止损/持仓上限/日限额等)
- `set_env.example.ps1` / `set_env.example.sh` - 环境变量模板，复制成`set_env.ps1`/`set_env.sh`并填入私钥
- `live_state.json` - 运行时生成，记录当前持仓/已实现盈亏(不含私钥)
- `live_orders.jsonl` - 每一笔尝试/成交的审计日志(不含私钥)
- `live_runner.log` - 运行日志
