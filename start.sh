#!/usr/bin/env bash
# Shared program kit. Each person keeps subscription, nodes, and ports in ~/rootlessxray.
# In the terminal you want to use:
#   source /path/to/rootlessxray/start.sh

KIT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    bash "$KIT/start.sh" || return $?
    # shellcheck disable=SC1091
    source "$KIT/proxy_env.sh"
    return 0
fi

set -u

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

XRAY="$KIT/xray"
CONFIG="$STATE/config.json"
NODES="$STATE/nodes.txt"
SUB="$STATE/subscription"
PORTS_FILE="$STATE/ports.env"
TEST_SCRIPT="$KIT/test_ipv4_xray.py"
LOG_ROOT="$KIT/log"
mkdir -p "$LOG_ROOT" 2>/dev/null || true
LOG_DIR="$LOG_ROOT/$USER"
if ! mkdir -p "$LOG_DIR"; then
    echo "ERROR: 不能创建日志目录 $LOG_DIR"
    exit 1
fi
chmod 700 "$LOG_DIR" 2>/dev/null || true
TEST_LOG="$LOG_DIR/test_nodes_pool.log"
RUN_LOG="$LOG_DIR/xray_run.log"
PID_FILE="$STATE/xray.pid"
TEST_URL="${XRAY_PROBE_URL:-https://chatgpt.com/backend-api/codex/responses}"
TEST_POST=1

if [[ ! -f "$SUB" ]]; then
    echo "把一行订阅地址写进："
    echo "  $SUB"
    echo "然后在这个终端执行："
    echo "  source $KIT/start.sh"
    exit 1
fi

_port_open() {
    ss -lntH 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$1$"
}

if [[ ! -f "$PORTS_FILE" ]]; then
    uid_n="$(id -u)"
    socks=$((35000 + (uid_n % 7000) * 2))
    tries=0
    while [[ "$tries" -lt 200 ]]; do
        http=$((socks + 1))
        if [[ "$socks" != "31808" && "$socks" != "31809" && "$http" != "31808" && "$http" != "31809" ]] \
            && ! _port_open "$socks" && ! _port_open "$http"; then
            break
        fi
        socks=$((socks + 2))
        tries=$((tries + 1))
    done
    if [[ "$tries" -ge 200 ]]; then
        echo "ERROR: 没有找到空闲端口。"
        exit 1
    fi
    printf 'XRAY_SOCKS_PORT=%s\nXRAY_HTTP_PORT=%s\n' "$socks" "$http" > "$PORTS_FILE"
    chmod 600 "$PORTS_FILE" 2>/dev/null || true
fi

SOCKS_PORT=""
HTTP_PORT=""
while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ -z "$line" || "$line" != *=* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    key="$(echo "$key" | sed 's/[[:space:]]//g')"
    val="$(echo "$val" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s/^["'\'']//;s/["'\'']$//')"
    case "$key" in
        XRAY_SOCKS_PORT) SOCKS_PORT="$val" ;;
        XRAY_HTTP_PORT) HTTP_PORT="$val" ;;
    esac
done < "$PORTS_FILE"

if [[ -z "$SOCKS_PORT" || -z "$HTTP_PORT" || "$SOCKS_PORT" == "31808" || "$HTTP_PORT" == "31809" ]]; then
    echo "ERROR: 端口无效。31808/31809 是个人代理，rootlessxray 不会使用。"
    exit 1
fi
export XRAY_SOCKS_PORT="$SOCKS_PORT"
export XRAY_HTTP_PORT="$HTTP_PORT"
export XRAY_STATE_DIR="$STATE"
export XRAY_BIN="$XRAY"

if [[ ! -x "$XRAY" ]]; then
    chmod +x "$XRAY" 2>/dev/null || true
fi
if [[ ! -x "$XRAY" ]]; then
    echo "ERROR: Xray binary not executable: $XRAY"
    exit 1
fi

cd "$STATE" || exit 1

echo "Reading $SUB and writing nodes.txt..."
if ! python3 "$KIT/parse_sub.py"; then
    echo "ERROR: subscription was not applied. Xray was not restarted."
    exit 1
fi

echo "[1/6] Checking required files..."
if [[ ! -s "$NODES" ]] || ! grep -qE '^(vless|ss|vmess|trojan)://' "$NODES"; then
    echo "ERROR: $NODES has no usable node URLs."
    echo "把一行订阅地址写进 $SUB 后再运行一次。"
    exit 1
fi

echo "[2/6] Testing nodes and updating config.json if working IPv4 nodes are found..."
echo "      ports: SOCKS=$SOCKS_PORT  HTTP=$HTTP_PORT"
echo "      state: $STATE"
python3 "$TEST_SCRIPT" | tee "$TEST_LOG"
test_rc=${PIPESTATUS[0]}
if [[ "$test_rc" -ne 0 ]]; then
    echo "ERROR: Node test script failed with exit code $test_rc"
    exit "$test_rc"
fi

KEPT_OLD_CONFIG=0
if grep -q '^APPLIED	' "$TEST_LOG"; then
    echo "Config updated from nodes that CAN reach ChatGPT."
elif grep -q '^NO_WORKING_NODE' "$TEST_LOG"; then
    KEPT_OLD_CONFIG=1
    echo "WARNING: No node in nodes.txt can reach ChatGPT."
    echo "         Keeping existing config.json (if any)."
    echo "         → Put a new subscription URL in $SUB, then rerun."
    if [[ ! -f "$CONFIG" ]]; then
        echo "ERROR: No working node and no existing config.json — cannot start."
        exit 1
    fi
else
    KEPT_OLD_CONFIG=1
    echo "No APPLIED marker found. Keeping existing config.json (if any)."
    if [[ ! -f "$CONFIG" ]]; then
        echo "ERROR: No config.json produced."
        exit 1
    fi
fi

echo "[3/6] Validating config.json..."
if ! env -u LD_LIBRARY_PATH "$XRAY" run -test -config "$CONFIG"; then
    echo "ERROR: Generated/current config.json is invalid. Xray was not restarted."
    exit 1
fi

echo "[4/6] Stopping old Xray for this directory..."
if [[ -f "$PID_FILE" ]]; then
    old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
        kill "$old_pid" 2>/dev/null || true
        sleep 1
        kill -9 "$old_pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
fi
if command -v pgrep >/dev/null 2>&1; then
    while read -r pid; do
        [[ -n "$pid" ]] || continue
        kill "$pid" 2>/dev/null || true
    done < <(pgrep -u "$USER" -f "$XRAY run -config $CONFIG" 2>/dev/null || true)
    sleep 1
fi

echo "[5/6] Starting Xray..."
env LD_LIBRARY_PATH="" nohup "$XRAY" run -config "$CONFIG" > "$RUN_LOG" 2>&1 &
echo $! > "$PID_FILE"
sleep 1.5

if ! _port_open "$SOCKS_PORT" || ! _port_open "$HTTP_PORT"; then
    echo "ERROR: Xray did not open ports $SOCKS_PORT/$HTTP_PORT. Last log:"
    tail -n 40 "$RUN_LOG"
    exit 1
fi

echo "[6/6] Verifying proxy against ChatGPT..."
CURL_BIN="/usr/bin/curl"
[[ -x "$CURL_BIN" ]] || CURL_BIN="curl"
_err="/tmp/rootlessxray_verify_err.$$"
_out="/tmp/rootlessxray_verify_out.$$"
set +e
if [[ "${TEST_POST:-1}" == "1" ]]; then
    env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u LD_LIBRARY_PATH \
        "$CURL_BIN" -sS -o /dev/null --max-time 25 \
        --socks5-hostname "127.0.0.1:$SOCKS_PORT" \
        -X POST -H 'content-type: application/json' -d '{}' \
        -w "%{http_code} %{time_total}" \
        "$TEST_URL" >"$_out" 2>"$_err"
else
    env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u LD_LIBRARY_PATH \
        "$CURL_BIN" -sS -o /dev/null --max-time 25 \
        --socks5-hostname "127.0.0.1:$SOCKS_PORT" \
        -w "%{http_code} %{time_total}" \
        "$TEST_URL" >"$_out" 2>"$_err"
fi
curl_rc=$?
set -u
http_line="$(cat "$_out" 2>/dev/null || true)"
http_code="${http_line%% *}"
http_time="${http_line#* }"
echo "ChatGPT probe: HTTP ${http_code:-000}, total ${http_time:-?}s  ($TEST_URL)"

if [[ "$curl_rc" -ne 0 || -z "$http_code" || "$http_code" == "000" ]] || ! [[ "$http_code" =~ ^[2-5][0-9][0-9]$ ]]; then
    echo "ERROR: Proxy verification failed (ChatGPT unreachable)."
    [[ -s "$_err" ]] && cat "$_err" >&2
    echo "Last log:"
    tail -n 40 "$RUN_LOG"
    rm -f "$_err" "$_out"
    exit "${curl_rc:-1}"
fi
rm -f "$_err" "$_out"

echo "Done. Xray is running on:"
echo "  SOCKS: 127.0.0.1:$SOCKS_PORT"
echo "  HTTP : 127.0.0.1:$HTTP_PORT"
echo "  State: $STATE"
echo "  Log:   $LOG_DIR"
echo "  Probe: ChatGPT Codex backend (HTTP $http_code = reachable)"
if [[ "$KEPT_OLD_CONFIG" -eq 1 ]]; then
    echo
    echo "NOTE: Kept old config; ChatGPT probe passed, but no fresh ChatGPT-capable node was selected."
fi
echo
echo "这个终端要翻墙，请执行："
echo "  source $KIT/start.sh"
echo "新开的终端只要："
echo "  source $KIT/proxy_env.sh"
echo "然后测试："
echo "  curl -s https://ipinfo.io"
