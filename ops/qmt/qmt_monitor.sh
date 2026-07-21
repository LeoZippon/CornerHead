#!/usr/bin/env bash
# QMT live monitor lifecycle (sync + Feishu fill notifications).
# Usage: ops/qmt/qmt_monitor.sh {start|stop|status}
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${QUANT_PYTHON:-$HOME/miniconda3/envs/quant/bin/python}"
PID_FILE="$REPO_ROOT/.runtime/qmt/monitor.pid"
LOG_FILE="$REPO_ROOT/logs/qmt/qmt_live_monitor.log"

# The pid must exist AND its cmdline must match the monitor script, so a stale
# pidfile whose pid number was recycled reads as DOWN (webui_stack.sh pattern).
alive() {
  local pid
  [[ -f "$PID_FILE" ]] && pid="$(cat "$PID_FILE")" && [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null && grep -qa qmt_live_monitor "/proc/$pid/cmdline" 2>/dev/null
}

case "${1:-status}" in
  start)
    if alive; then
      echo "monitor: already running (pid $(cat "$PID_FILE"))"
      exit 0
    fi
    mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"
    cd "$REPO_ROOT"
    nohup "$PYTHON" scripts/live/qmt_live_monitor.py >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "monitor: started (pid $(cat "$PID_FILE"), log $LOG_FILE)"
    ;;
  stop)
    if alive; then
      kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE"
      echo "monitor: stopped"
    else
      rm -f "$PID_FILE"
      echo "monitor: not running"
    fi
    ;;
  status)
    if alive; then
      echo "monitor: running (pid $(cat "$PID_FILE"))"
      tail -3 "$LOG_FILE" 2>/dev/null || true
    else
      echo "monitor: stopped"
    fi
    ;;
  *)
    echo "usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
