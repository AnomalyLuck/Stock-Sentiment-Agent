#!/bin/bash
# deploy/push-secrets.sh — copy Stock Digest's two secret files to a Droplet and activate the site.
#
# Runs on your workstation, never on the Droplet. It is the separate channel UD-16 requires: API keys
# and login hashes are in neither UserData.sh, nor this repository, nor the Droplet's metadata.
# The Droplet must have finished the StockDigest stage first (its login banner says so).
#
# Usage:  deploy/push-secrets.sh root@DROPLET_IP [secrets-dir]
#   secrets-dir defaults to deploy/secrets/ (gitignored). It must hold:
#     app.env             systemd EnvironmentFile for the app: KEY=value lines and # comments, no
#                         "export", no trailing comments. Start from .env or .env.example; set
#                         STOCK_DIGEST_CACHE_DIR=/var/cache/stock-digest and SEC_USER_AGENT; delete
#                         unused provider keys and every "your-..." placeholder.
#     stock-digest.users  one "<name> <bcrypt-hash>" per line; hash from: caddy hash-password --algorithm bcrypt
#   Re-run after changing either file: the services restart with the new values.
#
# Workstation script, not a kit stage. Every meaningful check below is explicit; errexit is only a
# backstop, so UD-41's caution does not apply. No intent covers workstation-side tooling: adr/ADR-0005.
set -euo pipefail

Target=${1:?usage: deploy/push-secrets.sh root@DROPLET_IP [secrets-dir]}
Dir=${2:-$(cd "$(dirname "$0")" && pwd)/secrets}
for File in app.env stock-digest.users; do
    [ -s "$Dir/$File" ] || { echo "missing $Dir/$File (see the header of this script)" >&2; exit 2; }
done
if grep -qE "^[A-Z_]+=['\"]?your-" "$Dir/app.env"; then
    echo "$Dir/app.env still has a 'your-...' placeholder from .env.example" >&2; exit 2
fi
if grep -q REPLACE_WITH_BCRYPT_HASH "$Dir/stock-digest.users"; then
    echo "$Dir/stock-digest.users still has the example placeholder; run: caddy hash-password --algorithm bcrypt" >&2; exit 2
fi

echo "Copying app.env and stock-digest.users to $Target, then activating..."
# One SSH session: the two files travel as a tar stream on stdin and land with their final owner
# and mode. COPYFILE_DISABLE stops macOS tar from adding ._* metadata entries. The remote command
# is single-quoted here so that it expands on the Droplet, not on this machine.
COPYFILE_DISABLE=1 tar -cf - -C "$Dir" app.env stock-digest.users | ssh "$Target" '
    set -e
    [ -f /srv/BldTmp/UserData/stockdigest.log ] || {
        echo "the StockDigest stage has not completed on this box; see /etc/motd and /srv/BldTmp/UserData/" >&2; exit 3; }
    Tmp=$(mktemp -d)
    tar -xf - -C "$Tmp"
    install -o root -g root  -m 600 "$Tmp/app.env"            /etc/stock-digest/app.env
    install -o root -g caddy -m 640 "$Tmp/stock-digest.users" /etc/caddy/stock-digest.users
    rm -rf "$Tmp"
    /usr/local/sbin/stock-digest-activate
'
