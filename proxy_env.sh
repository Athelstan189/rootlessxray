#!/usr/bin/env bash
# Usage: source /path/to/rootlessxray/proxy_env.sh
# Points the current shell at this user's own rootlessxray ports.

_KIT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "请用 source 加载，直接执行不会改当前终端："
    echo "  source $0"
    exit 1
fi

_STATE="${XRAY_STATE_DIR:-$HOME/rootlessxray}"
_PORTS_FILE="${_STATE}/ports.env"

if [[ ! -f "$_PORTS_FILE" ]]; then
    echo "还没有可用的代理。先把一行订阅地址写进 ${_STATE}/subscription，再执行："
    echo "  source ${_KIT}/start.sh"
    unset _STATE _PORTS_FILE _KIT
    return 1
fi

XRAY_SOCKS_PORT=""
XRAY_HTTP_PORT=""
while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" != *=* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    key="${key// /}"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    val="${val#\"}"; val="${val%\"}"
    val="${val#\'}"; val="${val%\'}"
    case "$key" in
        XRAY_SOCKS_PORT) XRAY_SOCKS_PORT="$val" ;;
        XRAY_HTTP_PORT)  XRAY_HTTP_PORT="$val" ;;
    esac
done < "$_PORTS_FILE"

if [[ -z "$XRAY_SOCKS_PORT" || -z "$XRAY_HTTP_PORT" || "$XRAY_SOCKS_PORT" == "31808" || "$XRAY_HTTP_PORT" == "31809" ]]; then
    echo "端口文件无效。31808/31809 属于另一套个人代理。"
    unset _STATE _PORTS_FILE _KIT line key val
    return 1
fi

export http_proxy="http://127.0.0.1:${XRAY_HTTP_PORT}"
export https_proxy="$http_proxy"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"
export all_proxy="socks5://127.0.0.1:${XRAY_SOCKS_PORT}"
export ALL_PROXY="$all_proxy"
export no_proxy="localhost,127.0.0.1,::1,10.*,172.16.*,172.17.*,172.18.*,172.19.*,172.20.*,172.21.*,172.22.*,172.23.*,172.24.*,172.25.*,172.26.*,172.27.*,172.28.*,172.29.*,172.30.*,172.31.*,192.168.*,*.local,*.lan"
export NO_PROXY="$no_proxy"

echo "Proxy enabled -> HTTP ${XRAY_HTTP_PORT}, SOCKS ${XRAY_SOCKS_PORT}"
unset _STATE _PORTS_FILE _KIT line key val
