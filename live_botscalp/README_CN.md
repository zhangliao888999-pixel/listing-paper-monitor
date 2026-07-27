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

## 部署到Windows VPS（美东这类云服务器，Windows Server 2019）

家里网络不稳定的话，实盘建议放VPS上跑，24小时不间断。步骤：

1. VPS上装好 Python 3 + git，把这个仓库 clone 到VPS上(跟本地电脑一样的目录结构，
   `live_botscalp/` 是 `paper/` 仓库下的一个子目录)。

2. 跟本地一样：`pip install -r requirements.txt`，复制 `set_env.example.ps1` 为
   `set_env.ps1` 并填入私钥。**这一步在VPS上做，私钥留在VPS的这个文件里，不会
   经过Claude、不会提交到git**。

3. 先手动跑一次确认没问题：
   ```
   cd live_botscalp
   powershell -File .\run_live_vps.ps1
   ```
   默认dry-run，看`live_runner.log`确认候选扫描、报价获取都正常。

4. 用Windows计划任务让它每2-3分钟自动跑一次（在VPS的PowerShell里执行，用管理员权限）：
   ```
   $action = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"C:\完整路径\live_botscalp\run_live_vps.ps1`""
   schtasks /create /tn "LiveBotscalp" /tr $action /sc minute /mo 3 /f
   ```
   (路径换成VPS上实际的仓库路径)

5. `run_live_vps.ps1` 每次运行会先 `git pull` 拿到screener最新扫描的候选币数据——
   这个数据是你家里电脑的本地任务和GitHub云端workflow共同产生、推到同一个仓库的，
   VPS只要能访问外网git就能拿到，不需要在VPS上重新跑一遍screener。

6. 默认**不会**把`live_state.json`/`live_orders.jsonl`这些真实交易记录推回git——
   这个仓库是公开的，真实交易的tx签名能在Solscan上查到你的钱包地址，虽然Solana本身
   就是公链、这些数据本来就查得到，但没必要额外把它们集中汇总到一个公开仓库里让人
   更容易搜到。想让实盘状态也显示在看盘页面上的话，把`run_live_vps.ps1`最下面
   git push那几行取消注释。

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
