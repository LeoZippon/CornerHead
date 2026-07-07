#!/usr/bin/env bash
# CornerHead console stack manager on the compute hub (docs/deployment_documentation.md).
#
#   webui_stack.sh start        start console API (loopback :38888) + reverse tunnel
#   webui_stack.sh stop         stop both
#   webui_stack.sh status       show component states + end-to-end health
#   webui_stack.sh ensure       start whatever is down (cron keepalive target)
#   webui_stack.sh sync         push static SPA assets to the frontend server
#   webui_stack.sh install-cron install the keepalive crontab block (ensure + @reboot)
#
# The console API stays loopback-only here; autossh keeps a reverse tunnel that
# exposes it on the FRONTEND's loopback (127.0.0.1:38889), where nginx serves
# the static SPA and proxies /api. Experiment workers are detached processes:
# console/tunnel restarts never touch running experiments.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${PY:-$HOME/miniconda3/envs/quant/bin/python}"
PORT="${PORT:-38888}"
FRONTEND_HOST="${FRONTEND_HOST:-121.41.5.179}"
TUNNEL_USER="${TUNNEL_USER:-cornerhead}"
REMOTE_PORT="${REMOTE_PORT:-38889}"
RUN_DIR="$REPO/.runtime/webui"
LOG_DIR="$REPO/logs/webui"
CONSOLE_PID="$RUN_DIR/console.pid"
TUNNEL_PID="$RUN_DIR/tunnel.pid"
CRON_BEGIN="# BEGIN CornerHead webui stack"
CRON_END="# END CornerHead webui stack"
mkdir -p "$RUN_DIR" "$LOG_DIR"

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

start_console() {
    if alive "$CONSOLE_PID"; then echo "console: already running (pid $(cat "$CONSOLE_PID"))"; return; fi
    nohup "$PY" "$REPO/scripts/webui/run_webui.py" --port "$PORT" \
        >> "$LOG_DIR/console.log" 2>&1 &
    echo $! > "$CONSOLE_PID"
    echo "console: started (pid $!, 127.0.0.1:$PORT)"
}

start_tunnel() {
    if alive "$TUNNEL_PID"; then echo "tunnel: already running (pid $(cat "$TUNNEL_PID"))"; return; fi
    AUTOSSH_PIDFILE="$TUNNEL_PID" AUTOSSH_GATETIME=0 autossh -M 0 -f -N \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new \
        -o ConnectTimeout=10 \
        -R "127.0.0.1:${REMOTE_PORT}:127.0.0.1:${PORT}" \
        "${TUNNEL_USER}@${FRONTEND_HOST}"
    sleep 1
    if alive "$TUNNEL_PID"; then echo "tunnel: started (pid $(cat "$TUNNEL_PID"), R:${REMOTE_PORT} -> :${PORT})"
    else echo "tunnel: FAILED to start (see ssh output above)"; return 1; fi
}

stop_one() { # name pidfile
    if alive "$2"; then kill "$(cat "$2")" 2>/dev/null || true; echo "$1: stopped"; else echo "$1: not running"; fi
    rm -f "$2"
}

status() {
    alive "$CONSOLE_PID" && echo "console: running (pid $(cat "$CONSOLE_PID"))" || echo "console: DOWN"
    alive "$TUNNEL_PID" && echo "tunnel:  running (pid $(cat "$TUNNEL_PID"))" || echo "tunnel:  DOWN"
    curl -sf -m 5 "http://127.0.0.1:${PORT}/api/health" > /dev/null \
        && echo "local API: ok" || echo "local API: unreachable"
    ssh -o ConnectTimeout=8 "root@${FRONTEND_HOST}" \
        "curl -sf -m 5 http://127.0.0.1:8080/api/health > /dev/null" 2>/dev/null \
        && echo "frontend end-to-end: ok (nginx -> tunnel -> local API)" \
        || echo "frontend end-to-end: unreachable"
}

sync_static() {
    echo "syncing static SPA to ${FRONTEND_HOST}:/opt/cornerhead/static ..."
    tar -C "$REPO/src/autotrade/webui/static" -cz . \
        | ssh "root@${FRONTEND_HOST}" \
        'tar -xz -C /opt/cornerhead/static --no-same-owner --no-same-permissions \
         && chmod -R a+rX /opt/cornerhead/static && echo "static synced"'
}

install_cron() {
    local self="$REPO/ops/webui/webui_stack.sh"
    local block
    block="$CRON_BEGIN
*/2 * * * * flock -n $RUN_DIR/ensure.lock $self ensure >> $LOG_DIR/keepalive.log 2>&1
@reboot sleep 30 && $self ensure >> $LOG_DIR/keepalive.log 2>&1
$CRON_END"
    ( crontab -l 2>/dev/null | sed "/^${CRON_BEGIN}\$/,/^${CRON_END}\$/d"; echo "$block" ) | crontab -
    echo "keepalive cron installed (every 2 min + @reboot):"
    crontab -l | sed -n "/^${CRON_BEGIN}\$/,/^${CRON_END}\$/p"
}

case "${1:-}" in
    start)  start_console; start_tunnel ;;
    stop)   stop_one tunnel "$TUNNEL_PID"; stop_one console "$CONSOLE_PID" ;;
    status) status ;;
    ensure) start_console; start_tunnel ;;
    sync)   sync_static ;;
    install-cron) install_cron ;;
    *) echo "usage: $0 {start|stop|status|ensure|sync|install-cron}"; exit 2 ;;
esac
