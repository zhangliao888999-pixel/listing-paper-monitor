# 复制这个文件为 set_env.ps1 (不要提交到git，.gitignore已经排除)，填入你自己的私钥，
# 然后每次开新的PowerShell窗口都要先 . .\set_env.ps1 (注意前面的点，是dot-source，
# 不能直接./set_env.ps1运行，否则环境变量设置在子进程里不会生效)

# 你的钱包私钥(base58编码字符串，从Phantom/Solflare导出)。
# 这一步只在你自己的机器上执行，私钥不会经过Claude/任何第三方。
$env:WALLET_PRIVATE_KEY = "在这里填你的私钥"

# 默认保持关闭(dry-run)。真的要下真实单的时候，两个都设成下面的值：
$env:LIVE_TRADING = "0"                # 改成 "1" 才会真实广播交易
$env:CONFIRM_LIVE_BOTSCALP = "NO"      # 改成 "YES" 才会真实广播交易(双重确认，防手滑)
