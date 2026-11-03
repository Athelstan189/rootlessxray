#!/usr/bin/env bash
# Open the node screen. It does not change the current shell's proxy variables.
# After leaving the screen, run: source /path/to/rootlessxray/proxy_env.sh

KIT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATE="${XRAY_STATE_DIR:-$HOME/rootlessxray}"
mkdir -p "$STATE"
chmod 700 "$STATE" 2>/dev/null || true
STATE="$(cd -- "$STATE" && pwd)"

case "$STATE" in
    /disk227/lixingqi/software/xray|/disk227/lixingqi/software/xray4all|/disk227/lixingqi/software/rootlessxray)
        echo "ERROR: 订阅和配置不能放在公共程序目录里。"
        echo "请使用自己的目录：\$HOME/rootlessxray"
        exit 1
        ;;
esac

export XRAY_STATE_DIR="$STATE"
export XRAY_BIN="$KIT/xray"
export XRAY_LOG_DIR="$KIT/log/${USER:-user}"
exec python3 "$KIT/tui.py"
