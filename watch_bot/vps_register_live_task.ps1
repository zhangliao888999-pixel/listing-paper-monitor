# -*- coding: utf-8 -*-
# 2026-07-30新增: 注册"实盘"版本的lifecycle_runner_loop.py计划任务——跟纸盘那个
# 用的是同一个脚本文件,靠vps_run_forever.ps1的-LiveMode开关切换行为,不需要
# 另外写一份脚本、也不会有两份不同代码将来各自漂移不一致的风险。
#
# 安全说明: 就算没有先创建watch_bot\.live_wallet_key这个文件,启动这个任务也
# 是安全的——lifecycle_logger.py里已经写死"SNIPE_LIVE_MODE=1但没有
# WALLET_PRIVATE_KEY就拒绝真实下单,只跑crash_watch/insider_sell_watch监控"，
# 不会因为忘记放钥匙就误以为在跑真钱、实际上在裸奔跑纸盘。
#
# 用法: 先跑一次vps_register_tasks.ps1里对应的Stop步骤把纸盘的
# watchbot_lifecycle_runner_loop.py停掉(避免两份lifecycle_runner_loop同时
# 抢同一份pump_lifecycle.json),再跑这个脚本。真正开始下真钱之前,记得先在
# watch_bot\.live_wallet_key里放好私钥(一行,不要有多余空格/换行)。
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

# 2026-07-30再补: 实盘首跑10分钟无开仓,排查发现纸盘时代真正高产的发现腿是
# mcap_scanner(90秒一轮,42笔snipe成交的来源),lifecycle只是慢速兜底腿。
# mcap_scanner.py内部就是调lifecycle_logger.deploy_full_stack,实盘开关/名额
# 限制/私钥检查全部自动生效,所以把它也注册成live任务——两条腿共用同一个
# MAX_CONCURRENT_LIVE=1名额(标记文件是全局的),不会因为多一条腿就多开仓。
# 2026-07-31再补: 加pregrad实盘腿——用户用数据拍板(pregrad纸盘565笔/天 vs
# snipe腿35笔/天),要快速攒实盘样本必须靠它。执行通道走PumpPortal(毕业前的
# 币不在Jupiter路由里),三条腿共用同一个实盘名额标记,全局同时最多1个真实仓位。
$liveTasks = @(
    @{ Name = "watchbot_live_runner_loop";  Script = "lifecycle_runner_loop.py" },
    @{ Name = "watchbot_live_mcap_loop";    Script = "mcap_scanner_loop.py" },
    @{ Name = "watchbot_live_pregrad_loop"; Script = "pregrad_scanner_loop.py" }
)

foreach ($t in $liveTasks) {
    Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue

    $argStr = "-NoProfile -ExecutionPolicy Bypass -File `"$dir\vps_run_forever.ps1`" -ScriptName $($t.Script) -LiveMode"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argStr -WorkingDirectory $dir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Days 0) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $t.Name
    Write-Host "已注册并启动: $($t.Name)"
}

$keyFile = Join-Path $dir ".live_wallet_key"
if (Test-Path $keyFile) {
    Write-Host "检测到.live_wallet_key已存在,这个任务会真实下单"
} else {
    Write-Host "*** 还没有.live_wallet_key,这个任务现在只会监控、不会真实下单 ***"
    Write-Host "*** 准备好下单时: 把私钥写进 $keyFile (一行),然后跑一遍本脚本重启任务 ***"
}
