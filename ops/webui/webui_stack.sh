#!/usr/bin/env bash
# CornerHead console stack manager on the compute hub (docs/deployment_documentation.md).
#
#   webui_stack.sh start        start console API (Unix socket) + reverse tunnel
#   webui_stack.sh stop         stop both
#   webui_stack.sh status       show component states + end-to-end health
#   webui_stack.sh ensure       start whatever is down (cron keepalive target)
#   webui_stack.sh sync         push static SPA assets to the frontend server
#   webui_stack.sh install-cron install the keepalive crontab block (ensure + @reboot)
#
# The console API binds a Unix socket inside the 0700 run dir — the hub is a
# shared multi-user machine, and loopback TCP would be reachable by every
# local user; the socket directory's permissions make the API lzp-only,
# kernel-enforced. autossh keeps a reverse tunnel that exposes the socket as
# TCP on the FRONTEND's loopback (127.0.0.1:38889), where nginx serves the
# static SPA and proxies /api. Experiment workers are detached processes:
# console/tunnel restarts never touch running experiments.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${PY:-$HOME/miniconda3/envs/quant/bin/python}"
FRONTEND_HOST="${FRONTEND_HOST:-121.41.5.179}"
TUNNEL_USER="${TUNNEL_USER:-cornerhead}"
REMOTE_PORT="${REMOTE_PORT:-38889}"
RUN_DIR="$REPO/.runtime/webui"
LOG_DIR="$REPO/logs/webui"
SOCK="$RUN_DIR/console.sock"
CONSOLE_PID="$RUN_DIR/console.pid"
TUNNEL_PID="$RUN_DIR/tunnel.pid"
CRON_BEGIN="# BEGIN CornerHead webui stack"
CRON_END="# END CornerHead webui stack"
mkdir -p "$RUN_DIR" "$LOG_DIR"
chmod 700 "$RUN_DIR"   # the access-control boundary for the console socket

# alive PIDFILE PATTERN — the pid must exist AND its cmdline must match PATTERN,
# so a stale pidfile whose pid number was recycled after a reboot reads as DOWN.
alive() {
    local pid
    [ -f "$1" ] && pid="$(cat "$1")" && [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null && grep -qa "$2" "/proc/$pid/cmdline" 2>/dev/null
}

QUIET=0
say() { [ "$QUIET" = 1 ] || echo "$@"; }

# Copy-truncate rotation, one generation; keeps the cron/uvicorn append fds valid.
rotate_log() {
    local f="$1" max=$((10 * 1024 * 1024))
    [ -f "$f" ] && [ "$(stat -c %s "$f")" -gt "$max" ] || return 0
    cp "$f" "$f.1" && : > "$f"
    echo "rotated $(basename "$f") (>10MB, one generation kept)"
}

start_console() {
    if alive "$CONSOLE_PID" run_webui; then say "console: already running (pid $(cat "$CONSOLE_PID"))"; return; fi
    rm -f "$SOCK"   # a stale socket file from a crashed console blocks the bind
    # 9>&-: long-lived children must not inherit the ensure.lock fd, or they
    # hold the lock forever and every later stack operation times out.
    nohup "$PY" "$REPO/scripts/webui/run_webui.py" --uds "$SOCK" \
        >> "$LOG_DIR/console.log" 2>&1 9>&- &
    echo $! > "$CONSOLE_PID"
    sleep 1
    if alive "$CONSOLE_PID" run_webui; then echo "console: started (pid $(cat "$CONSOLE_PID"), uds $SOCK)"
    else rm -f "$CONSOLE_PID"; echo "console: FAILED to start (died immediately — see $LOG_DIR/console.log)"; return 1; fi
}

start_tunnel() {
    if alive "$TUNNEL_PID" autossh; then say "tunnel: already running (pid $(cat "$TUNNEL_PID"))"; return; fi
    AUTOSSH_PIDFILE="$TUNNEL_PID" AUTOSSH_GATETIME=0 autossh -M 0 -f -N \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new \
        -o ConnectTimeout=10 \
        -R "127.0.0.1:${REMOTE_PORT}:${SOCK}" \
        "${TUNNEL_USER}@${FRONTEND_HOST}" 9>&-
    sleep 1
    if alive "$TUNNEL_PID" autossh; then echo "tunnel: started (pid $(cat "$TUNNEL_PID"), R:${REMOTE_PORT} -> ${SOCK})"
    else echo "tunnel: FAILED to start (see ssh output above)"; return 1; fi
}

stop_one() { # name pidfile pattern
    if alive "$2" "$3"; then kill "$(cat "$2")" 2>/dev/null || true; echo "$1: stopped"; else echo "$1: not running"; fi
    rm -f "$2"
}

status() {
    alive "$CONSOLE_PID" run_webui && echo "console: running (pid $(cat "$CONSOLE_PID"))" || echo "console: DOWN"
    alive "$TUNNEL_PID" autossh && echo "tunnel:  running (pid $(cat "$TUNNEL_PID"))" || echo "tunnel:  DOWN"
    curl -sf -m 5 --unix-socket "$SOCK" "http://console/api/health" > /dev/null \
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
    local block current
    block="$CRON_BEGIN
*/2 * * * * $self ensure >> $LOG_DIR/keepalive.log 2>&1
@reboot sleep 30 && $self ensure >> $LOG_DIR/keepalive.log 2>&1
$CRON_END"
    # Fail fast on a real crontab read error: treating it as an empty table
    # would wipe every unrelated job. Only a genuine "no crontab" reads as empty.
    if ! current="$(crontab -l 2>/tmp/webui_crontab_err.$$)"; then
        if grep -qi "no crontab for" /tmp/webui_crontab_err.$$; then
            current=""
        else
            echo "FAILED: crontab -l error: $(cat /tmp/webui_crontab_err.$$)" >&2
            rm -f /tmp/webui_crontab_err.$$
            exit 1
        fi
    fi
    rm -f /tmp/webui_crontab_err.$$
    if [ -n "$current" ]; then
        mkdir -p "$LOG_DIR"
        printf '%s\n' "$current" > "$LOG_DIR/crontab-$(date +%Y%m%d-%H%M%S).bak"
    fi
    ( printf '%s\n' "$current" | sed "/^${CRON_BEGIN}\$/,/^${CRON_END}\$/d"; echo "$block" ) | crontab -
    crontab -l | grep -qF "$CRON_BEGIN" || { echo "FAILED: managed block missing after install" >&2; exit 1; }
    echo "keepalive cron installed (every 2 min + @reboot):"
    crontab -l | sed -n "/^${CRON_BEGIN}\$/,/^${CRON_END}\$/p"
}

# Mutating subcommands share one lock with the cron ensure (flock -n there),
# so a manual start/stop can never race the keepalive into double spawns.
grab_lock() { exec 9>"$RUN_DIR/ensure.lock"; flock -w 30 9 || { echo "another stack operation holds the lock"; exit 1; }; }

case "${1:-}" in
    start)  grab_lock; start_console; start_tunnel ;;
    stop)   grab_lock; stop_one tunnel "$TUNNEL_PID" autossh; stop_one console "$CONSOLE_PID" run_webui ;;
    status) status ;;
    # Cron target: silent when everything is already up, so keepalive.log only
    # records actual starts, rotations, and failures.
    ensure) grab_lock; QUIET=1
            rotate_log "$LOG_DIR/console.log"; rotate_log "$LOG_DIR/keepalive.log"
            start_console; start_tunnel ;;
    sync)   sync_static ;;
    install-cron) install_cron ;;
    *) echo "usage: $0 {start|stop|status|ensure|sync|install-cron}"; exit 2 ;;
esac
