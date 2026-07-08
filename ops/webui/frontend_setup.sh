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
# Designated-keys-only sshd: all key material lives in a root-owned central
# store, so no account (including a compromised one) can grant itself SSH
# access by editing user dotfiles; AllowUsers refuses every other account.
install -d -m 755 /etc/ssh/authorized_keys.d
for u in root admin; do
  h=\$(eval echo ~\$u)
  [ -f "/etc/ssh/authorized_keys.d/\$u" ] || install -m 644 -o root -g root "\$h/.ssh/authorized_keys" "/etc/ssh/authorized_keys.d/\$u"
done
cat > /etc/ssh/authorized_keys.d/cornerhead <<'KEYS'
# compute hub: reverse tunnel only (exposes the console API on loopback)
restrict,port-forwarding,permitlisten="127.0.0.1:38889" ${HUB_PUBKEY}
# researcher MacBook: local forward to the console only
restrict,port-forwarding,permitopen="127.0.0.1:8080" ${MAC_PUBKEY}
KEYS
chmod 644 /etc/ssh/authorized_keys.d/cornerhead
cat > /etc/ssh/sshd_config.d/10-designated-keys.conf <<'CONF'
# Designated-keys-only access: key material lives in a root-owned directory,
# so no account (including a compromised one) can grant itself SSH access
# by editing user dotfiles. Managed by ops/webui/frontend_setup.sh.
AuthorizedKeysFile /etc/ssh/authorized_keys.d/%u
AllowUsers root admin cornerhead
AuthenticationMethods publickey
CONF
sshd -t
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
# WebUI local-user isolation: loopback TCP is otherwise reachable by every
# local account; owner-match rules confine the console to designated users.
CH_UID=\$(id -u cornerhead) ADMIN_UID=\$(id -u admin) WWW_UID=\$(id -u www-data)
cat > /etc/nftables.conf <<NFT
#!/usr/sbin/nft -f
# WebUI local-user isolation (managed by ops/webui/frontend_setup.sh):
# only designated local users may reach the console on loopback.
#   :8080  (nginx entry)  <- root (vendor console/health), admin (researcher),
#                            cornerhead (sshd forwards for the MacBook)
#   :38889 (raw API hop)  <- root, www-data (nginx proxy)
# Numeric uids: name resolution failing at boot would fail the whole load
# and leave the ports open (policy accept = fail-open).
flush ruleset
table inet cornerhead {
    chain local_out {
        type filter hook output priority filter; policy accept;
        oifname "lo" tcp dport 38889 meta skuid != { 0, \$WWW_UID } reject with tcp reset
        oifname "lo" tcp dport 8080 meta skuid != { 0, \$ADMIN_UID, \$CH_UID } reject with tcp reset
    }
}
NFT
nft -c -f /etc/nftables.conf && nft -f /etc/nftables.conf
systemctl enable --now nftables >/dev/null 2>&1
echo "frontend user + designated-keys sshd + keepalive + local-user isolation ready"
REMOTE

scp -q "$HERE/nginx-cornerhead.conf" "$FRONTEND:/etc/nginx/sites-available/cornerhead"
ssh "$FRONTEND" 'ln -sf /etc/nginx/sites-available/cornerhead /etc/nginx/sites-enabled/cornerhead && nginx -t && systemctl reload nginx && echo "nginx configured (loopback :8080)"'
echo "frontend setup complete — now run: ops/webui/webui_stack.sh sync && ops/webui/webui_stack.sh start"
