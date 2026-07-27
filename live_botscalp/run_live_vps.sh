#!/bin/bash
# VPS(Linux)上跑live_runner.py的包装脚本，用cron每2-3分钟调用一次。
#
# 流程: git pull拿到screener最新扫描的候选币数据(云端workflow+你家里电脑本地
# 任务都会往同一个git仓库推送，VPS只要pull就能拿到两边汇总后的最新候选)，
# 然后跑live_runner.py。
#
# 注意: 默认不会把live_state.json/live_orders.jsonl这些真实交易记录推回git仓库
# ——这个仓库是公开的，真实交易记录里的tx签名可以在Solscan上查到你的钱包地址，
# 虽然Solana本身就是公链、这些数据本来就查得到，但没必要额外把它们集中汇总到
# 一个公开仓库里让人更容易搜到。想同步到看盘页面的话，把最下面git push那几行
# 取消注释，或者换一个私有仓库单独存这些文件。
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"   # live_botscalp的上一级 = paper/ (git仓库根目录)
LOCK_FILE="$SCRIPT_DIR/live_vps.lock"

# 锁文件: 防止上一轮还没跑完、cron又触发下一轮
if [ -f "$LOCK_FILE" ]; then
    age=$(($(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || stat -f %m "$LOCK_FILE")))
    if [ "$age" -lt 600 ]; then
        exit 0
    fi
fi
date +%s > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

cd "$REPO_DIR"
git pull --rebase --quiet || true   # 拉最新候选数据；网络抖动导致pull失败也不阻断下面的执行

cd "$SCRIPT_DIR"
# cron任务不会继承你交互式shell里export过的环境变量，必须在这里显式source
# (set_env.sh要自己复制set_env.example.sh改好放在这里，已经在.gitignore里)
if [ -f "$SCRIPT_DIR/set_env.sh" ]; then
    source "$SCRIPT_DIR/set_env.sh"
fi
export PYTHONIOENCODING=utf-8
python3 live_runner.py

# 默认不推送真实交易记录回公开仓库，见上面注释。想开启的话取消下面几行注释：
# cd "$REPO_DIR"
# git add live_botscalp/live_state.json live_botscalp/live_orders.jsonl live_botscalp/live_runner.log
# git commit -m "live botscalp cycle $(date -u +%Y-%m-%dT%H:%MZ)" --quiet || true
# git push --quiet || true
