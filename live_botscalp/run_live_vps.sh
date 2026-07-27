#!/bin/bash
# Linux VPS runner for live_runner.py, invoked by cron every 2-3 minutes.
# (Current deployment target is Windows Server 2019 - see run_live_vps.ps1 for
# that. Kept here in case of a future Linux VPS.)
#
# This package is self-contained: live_runner.py fetches candidate data over
# HTTPS itself, so there's no git repo dependency - just drop this folder
# anywhere and run it.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="$SCRIPT_DIR/live_vps.lock"

if [ -f "$LOCK_FILE" ]; then
    age=$(($(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || stat -f %m "$LOCK_FILE")))
    if [ "$age" -lt 600 ]; then
        exit 0
    fi
fi
date +%s > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

cd "$SCRIPT_DIR"
# cron doesn't inherit env vars exported in some interactive shell - load them here.
if [ -f "$SCRIPT_DIR/set_env.sh" ]; then
    source "$SCRIPT_DIR/set_env.sh"
fi
export PYTHONIOENCODING=utf-8
python3 live_runner.py
