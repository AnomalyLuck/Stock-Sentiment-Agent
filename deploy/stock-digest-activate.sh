#!/bin/bash
# stock-digest-activate — start Stock Digest once its two secret files are on the box, then verify.
#
# Installed by stages/StockDigest.sh to /usr/local/sbin/stock-digest-activate (UD-31: written once,
# invoked as needed). Invoked by the stage when the secrets already exist, by deploy/push-secrets.sh
# over ssh right after it copies them, and by hand after a key or login changes. Re-running is the
# normal case, so this is not a run-once script and keeps no completion stamp (UD-52 covers run-once
# scripts). It never prints a secret value.
#
# Expects (UD-16: secrets arrive by a separate channel after the baseline; adr/ADR-0003):
#   /etc/stock-digest/app.env       systemd EnvironmentFile for stock-digest.service
#   /etc/caddy/stock-digest.users   Caddy basic_auth users: "<name> <bcrypt-hash>" per line
# IDs: UD-n and B-n refer to the UserData kit's intents and bindings; ADR-n to this repo's adr/.

. /srv/git-ops/lib/safeErr.sh; SafeErr                      # UD-40: Pass/Fail helpers; stop on error when no tty
StageName=activate                                          # label for the VERIFY summary line only
trap '[ $? -eq 0 ] || echo "activation did not complete; inspect: journalctl -u stock-digest -u caddy -n 50" >&2' EXIT

AppEnv=/etc/stock-digest/app.env
Users=/etc/caddy/stock-digest.users
CaddyEnv=/etc/caddy/stock-digest.env                        # written by the stage; non-secret

############ 1. Secret files present, well-formed, root-only. Values are never echoed.
Hint="run deploy/push-secrets.sh root@<this host> from your workstation"
[ -s "$AppEnv" ] || { echo "missing $AppEnv: $Hint" >&2; exit 2; }
[ -s "$Users" ]  || { echo "missing $Users: $Hint" >&2; exit 2; }
chown root:root "$AppEnv";  chmod 600 "$AppEnv"
chown root:caddy "$Users";  chmod 640 "$Users"              # caddy runs as user caddy and must read it

Bad=0
Complain() { echo "$1" >&2; Bad=1; }
grep -qE '^OPENAI_API_KEY=.*[A-Za-z0-9]' "$AppEnv"      || Complain "app.env: OPENAI_API_KEY is missing or empty"
grep -qE "^[A-Z_]+=['\"]?your-" "$AppEnv"               && Complain "app.env: a 'your-...' placeholder from .env.example is still present"
grep -qE "^STOCK_DIGEST_CACHE_DIR=['\"]*\$" "$AppEnv"   && Complain "app.env: STOCK_DIGEST_CACHE_DIR is blank and overrides the unit's /var/cache/stock-digest; set it or delete the line"
grep -qE '^[[:space:]]*export[[:space:]]' "$AppEnv"     && Complain "app.env: 'export' lines are invalid in a systemd EnvironmentFile"
grep -qvE '^[[:space:]]*(#|$)' "$Users"                  || Complain "stock-digest.users: no user lines"
grep -vE '^[[:space:]]*(#|$)' "$Users" | grep -qvE '^[^[:space:]]+[[:space:]]+\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}[[:space:]]*$' \
    && Complain "stock-digest.users: every line must be '<name> <bcrypt hash>'; make the hash with: caddy hash-password --algorithm bcrypt"
[ "$Bad" -eq 0 ] || exit 2

############ 2. Web ports in TcpOK; re-add if an nftables reload dropped them         (UD-14, B-7, ADR-0006)
/usr/local/sbin/stock-digest-ports || { echo "could not put the web ports in TcpOK; nothing restarted" >&2; exit 2; }   # UD-41: explicit

############ 3. Validate Caddy against the real files, then (re)start both services
if ! caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile --envfile "$CaddyEnv" >/dev/null; then
    echo "Caddyfile does not validate; nothing restarted" >&2   # UD-41: an explicit gate; the prelude turns errexit off in tty mode (UD-40)
    exit 2                                                      # same code as this helper's other refusals
fi
systemctl restart stock-digest                              # restart, not reload: EnvironmentFile is read at start
systemctl restart caddy

############ 4. Verify: ask each object directly; include the negatives               (UD-53, B-24, B-25)
Host=$(sed -nE 's/^STOCK_DIGEST_DOMAINS=//p' "$CaddyEnv" | cut -d, -f1 | tr -d '[:space:]')   # never source that file: its value holds a space
Code=000
for _ in $(seq 1 60); do                                    # the app imports its model and market libraries before it binds
    Code=$(curl -s --max-time 2 -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/ || true)
    [ "$Code" = 200 ] && break
    sleep 1
done
[ "$Code" = 200 ] && Pass AP4 "app answers 200 on 127.0.0.1:8765" || Fail AP4 "app returned '$Code' on 127.0.0.1:8765 after 60s"
systemctl is-active --quiet stock-digest && Pass SV3 "stock-digest.service active" || Fail SV3 "stock-digest.service not active"
systemctl is-active --quiet caddy        && Pass SV4 "caddy.service active"        || Fail SV4 "caddy.service not active"
if ! Socks=$(ss -ltnH 'sport = :8765' 2>&1); then             # UD-41: explicit check, not errexit
    Fail NET1 "ss could not list port 8765: $Socks"
elif [ -z "$Socks" ]; then
    Fail NET1 "nothing listens on 8765"                        # absence of a wildcard is not "loopback only"
elif printf '%s\n' "$Socks" | awk '{print $4}' | grep -qvE '^(127\.[0-9.]+|\[::1\]):8765$'; then
    Fail NET1 "8765 listens beyond loopback: $(printf '%s\n' "$Socks" | awk '{print $4}' | tr '\n' ' ')"
else
    Pass NET1 "8765 listens on loopback only"                  # UD-53, B-25: asks only port 8765's sockets
fi
Http=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' -H "Host: $Host" http://127.0.0.1/ || true)
case "$Http" in
    301|302|307|308) Pass WEB1 "caddy answers on 80 for $Host ($Http to https)" ;;
    *)               Fail WEB1 "caddy on 80 for $Host returned '$Http'" ;;
esac
Https=$(curl -sk --max-time 10 -o /dev/null -w '%{http_code}' --resolve "$Host:443:127.0.0.1" "https://$Host/" || true)
[ "$Https" = 401 ] && Pass WEB2 "https://$Host/ asks for a login (401)" \
    || Fail WEB2 "https://$Host/ returned '$Https'. 000 means no certificate yet: DNS must point here and 80/443 be reachable from the internet; see journalctl -u caddy"
VerifySummary || exit 70

############ 5. Console status                                                       (UD-25)
printf 'Stock Digest: ACTIVE since %s. Site https://%s/  Services: stock-digest, caddy  Logs: journalctl -u stock-digest -u caddy\n' \
    "$(date -u +%FT%TZ)" "$Host" >/etc/motd
