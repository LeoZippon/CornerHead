#!/usr/bin/env bash
# CornerHead console stack manager on the compute hub (docs/deployment_documentation.md).
#
#   webui_stack.sh start        start console API (Unix socket) + reverse tunnel
#   webui_stack.sh stop         stop both
#   webui_stack.sh status       show component states + end-to-end health
#   webui_stack.sh ensure       start whatever is down (cron keepalive target)
#   webui_stack.sh sync         push static SPA assets to the frontend server
#   webui_stack.sh deploy       sync SPA + recycle API, preserving tunnel/workers
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
PYCACHE_DIR="$RUN_DIR/pycache"
CRON_BEGIN="# BEGIN CornerHead webui stack"
CRON_END="# END CornerHead webui stack"
mkdir -p "$RUN_DIR" "$LOG_DIR" "$PYCACHE_DIR"
chmod 700 "$RUN_DIR" "$LOG_DIR" "$PYCACHE_DIR"   # socket and logs may contain experiment details

# alive PIDFILE PATTERN — the pid must exist AND its cmdline must match PATTERN,
# so a stale pidfile whose pid number was recycled after a reboot reads as DOWN.
alive() {
    local pid
    [ -f "$1" ] && pid="$(cat "$1")" && [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null && grep -qa "$2" "/proc/$pid/cmdline" 2>/dev/null
}

QUIET=0
say() { [ "$QUIET" = 1 ] || echo "$@"; }

code_current() {
    [[ "$1" == *'"code_current":true'* ]]
}

# Copy-truncate rotation, one generation; keeps the cron/uvicorn append fds valid.
rotate_log() {
    local f="$1" max=$((10 * 1024 * 1024))
    [ -f "$f" ] && [ "$(stat -c %s "$f")" -gt "$max" ] || return 0
    cp "$f" "$f.1" && : > "$f"
    echo "rotated $(basename "$f") (>10MB, one generation kept)"
}

start_console() {
    local pid health=""
    if alive "$CONSOLE_PID" run_webui; then say "console: already running (pid $(cat "$CONSOLE_PID"))"; return; fi
    rm -f "$SOCK"   # a stale socket file from a crashed console blocks the bind
    # 9>&-: long-lived children must not inherit the ensure.lock fd, or they
    # hold the lock forever and every later stack operation times out.
    # An empty external cache prefix plus -B makes the console and every worker
    # it spawns compile repository source, never read/write repo-local .pyc.
    PYTHONPYCACHEPREFIX="$PYCACHE_DIR" PYTHONDONTWRITEBYTECODE=1 \
    nohup "$PY" -B "$REPO/scripts/webui/run_webui.py" --uds "$SOCK" \
        >> "$LOG_DIR/console.log" 2>&1 9>&- &
    pid=$!
    echo "$pid" > "$CONSOLE_PID"
    for _ in {1..50}; do
        if alive "$CONSOLE_PID" run_webui \
            && health="$(curl -sf -m 5 --unix-socket "$SOCK" "http://console/api/health")"; then
            if code_current "$health"; then
                echo "console: started with current code (pid $(cat "$CONSOLE_PID"), uds $SOCK)"
                return
            fi
            echo "console: source changed during startup; refusing an unverifiable process" >&2
            break
        fi
        alive "$CONSOLE_PID" run_webui || break
        sleep 0.1
    done
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    rm -f "$CONSOLE_PID"
    echo "console: FAILED health check (see $LOG_DIR/console.log)"
    return 1
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

restart_console() {
    local pid=""
    if alive "$CONSOLE_PID" run_webui; then
        pid="$(cat "$CONSOLE_PID")"
        kill "$pid" 2>/dev/null || true
        for _ in {1..50}; do
            alive "$CONSOLE_PID" run_webui || break
            sleep 0.1
        done
        if alive "$CONSOLE_PID" run_webui; then
            echo "console: FAILED to stop gracefully (pid $pid)" >&2
            return 1
        fi
    fi
    rm -f "$CONSOLE_PID"
    start_console
}

status() {
    local health=""
    alive "$CONSOLE_PID" run_webui && echo "console: running (pid $(cat "$CONSOLE_PID"))" || echo "console: DOWN"
    alive "$TUNNEL_PID" autossh && echo "tunnel:  running (pid $(cat "$TUNNEL_PID"))" || echo "tunnel:  DOWN"
    if health="$(curl -sf -m 5 --unix-socket "$SOCK" "http://console/api/health")"; then
        echo "local API: ok"
        code_current "$health" \
            && echo "console code: current" \
            || echo "console code: STALE or unverifiable (run deploy after finalizing code)"
    else
        echo "local API: unreachable"
    fi
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
        # Crontab backups are operational maintenance artifacts, not runtime
        # logs; the full crontab may reference unrelated jobs, so keep them
        # owner-only like the TuShare installer's backups.
        mkdir -p "$REPO/archive/crontab"
        chmod 700 "$REPO/archive/crontab"
        ( umask 077; printf '%s\n' "$current" > "$REPO/archive/crontab/crontab-$(date +%Y%m%d-%H%M%S).bak" )
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
    # records actual starts, rotations, and failures (timestamped). A held
    # lock means another stack operation is mid-flight — exactly what the
    # keepalive must not disturb — so it skips silently instead of appending
    # the same lock message every two minutes.
    ensure) exec 9>"$RUN_DIR/ensure.lock"; flock -n 9 || exit 0
            QUIET=1
            { rotate_log "$LOG_DIR/console.log"; rotate_log "$LOG_DIR/keepalive.log"
              start_console; start_tunnel
            } 2>&1 | while IFS= read -r line; do echo "$(date -Is) $line"; done ;;
    sync)   sync_static ;;
    deploy) grab_lock; sync_static; restart_console; start_tunnel ;;
    install-cron) install_cron ;;
    *) echo "usage: $0 {start|stop|status|ensure|sync|deploy|install-cron}"; exit 2 ;;
esac
