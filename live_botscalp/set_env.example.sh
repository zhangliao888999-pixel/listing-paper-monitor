#!/bin/bash
# 复制这个文件为 set_env.sh (已经在.gitignore里，不会被提交)，填入你自己的私钥，
# 用 source 加载(注意是 source ./set_env.sh 或 . ./set_env.sh，不能直接./set_env.sh
# 执行，否则环境变量只在子shell里生效，退出就没了；cron任务需要在crontab里显式
# 指定环境变量或者让run_live_vps.sh在开头source这个文件)。

# 你的钱包私钥(base58编码字符串，从Phantom/Solflare导出)。
# 这一步只在你自己的VPS上执行，私钥不会经过Claude/任何第三方。
export WALLET_PRIVATE_KEY="在这里填你的私钥"

# 默认保持关闭(dry-run)。真的要下真实单的时候，两个都改成下面的值：
export LIVE_TRADING="0"                # 改成 "1" 才会真实广播交易
export CONFIRM_LIVE_BOTSCALP="NO"      # 改成 "YES" 才会真实广播交易(双重确认，防手滑)
