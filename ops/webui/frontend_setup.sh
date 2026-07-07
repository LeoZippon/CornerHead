#!/usr/bin/env bash
# One-time (idempotent) frontend-server provisioning for the CornerHead console.
# Run FROM the compute hub:  bash ops/webui/frontend_setup.sh
# Provisions on the frontend (root over SSH):
#   - dedicated no-shell user `cornerhead` whose authorized_keys allow ONLY
#     port-forwarding: the compute hub may reverse-listen on 127.0.0.1:38889,
#     the researcher's MacBook may locally forward to 127.0.0.1:8080;
#   - nginx site on 127.0.0.1:8080 (static SPA + /api proxy to the tunnel);
#     the stock default site is removed so nothing listens publicly.
set -euo pipefail

FRONTEND="${FRONTEND:-root@121.41.5.179}"
HERE="$(cd "$(dirname "$0")" && pwd)"
HUB_PUBKEY="$(cat "${HUB_PUBKEY_FILE:-$HOME/.ssh/id_ed25519.pub}")"
MAC_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEQeydwT+R05m4vwfgWN3Sw0PdQILVqqvgbDpdGogx/q lzp2002@icloud.com"

ssh "$FRONTEND" bash -s <<REMOTE
set -euo pipefail
id cornerhead >/dev/null 2>&1 || useradd --create-home --shell /usr/sbin/nologin cornerhead
install -d -m 700 -o cornerhead -g cornerhead /home/cornerhead/.ssh
cat > /home/cornerhead/.ssh/authorized_keys <<'KEYS'
# compute hub: reverse tunnel only (exposes the console API on loopback)
restrict,port-forwarding,permitlisten="127.0.0.1:38889" ${HUB_PUBKEY}
# researcher MacBook: local forward to the console only
restrict,port-forwarding,permitopen="127.0.0.1:8080" ${MAC_PUBKEY}
KEYS
chown cornerhead:cornerhead /home/cornerhead/.ssh/authorized_keys
chmod 600 /home/cornerhead/.ssh/authorized_keys
install -d -m 755 /opt/cornerhead/static
rm -f /etc/nginx/sites-enabled/default
systemctl enable --now nginx >/dev/null 2>&1 || true
# Reap dead SSH connections within ~90s; otherwise an uncleanly dropped reverse
# tunnel keeps 127.0.0.1:38889 bound for hours and blocks autossh from rebinding.
cat > /etc/ssh/sshd_config.d/98-cornerhead-tunnel.conf <<'SSHD'
ClientAliveInterval 30
ClientAliveCountMax 3
SSHD
systemctl reload ssh 2>/dev/null || systemctl reload sshd
echo "frontend user + dirs + sshd keepalive ready"
REMOTE

scp -q "$HERE/nginx-cornerhead.conf" "$FRONTEND:/etc/nginx/sites-available/cornerhead"
ssh "$FRONTEND" 'ln -sf /etc/nginx/sites-available/cornerhead /etc/nginx/sites-enabled/cornerhead && nginx -t && systemctl reload nginx && echo "nginx configured (loopback :8080)"'
echo "frontend setup complete — now run: ops/webui/webui_stack.sh sync && ops/webui/webui_stack.sh start"
