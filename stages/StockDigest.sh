#!/bin/bash
# stages/StockDigest.sh — project stage: install and wire the Stock Digest web app on Rocky Linux 10.
#
# Called by the loader after the security baseline and the fetch (UD-30: called, never chained);
# also a re-entry point by hand (UD-33). Runs once per box (UD-52): a re-run exits early.
# Inherits from the loader: GitOpsDir, UDdir, StockDigestDomains; the defaults below cover a hand run.
# Writes no secrets (UD-16). API keys and login hashes arrive later through deploy/push-secrets.sh,
# which runs /usr/local/sbin/stock-digest-activate (installed here) to start and verify the services.
# IDs: UD-n and B-n refer to the UserData kit's intents and bindings; ADR-n to this repo's adr/.

. /srv/git-ops/lib/safeErr.sh; SafeErr                      # UD-40: one prelude for every stage

: "${GitOpsDir:=/srv/git-ops}"                              # B-19: the repository is fetched here, in place
: "${StockDigestDomains:=stocksentimentdigest.com, www.stocksentimentdigest.com}"   # non-secret (UD-16)
AppDir=/opt/stock-digest                                    # install prefix: no intent covers this; ADR-0001
AppUser=stock-digest                                        # matches deploy/stock-digest.service
Deploy="$GitOpsDir/deploy"                                  # versioned unit, drop-ins, Caddyfile, helper

StageDone stockdigest && { echo "stockdigest stage already complete ($UDdir/stockdigest.log); refusing to re-run" >&2; exit 0; }   # UD-52
StageBegin stockdigest                                      # UD-23, UD-24, UD-25
{
############ 1. System packages: the most stable layer first                         (UD-34)
dnf install -y epel-release                                 # Caddy 2.10 is in EPEL 10; the Caddyfile needs >= 2.8
dnf install -y python3 python3-pip caddy                    # python3 is 3.12 on Rocky 10; plain packages (B-21 style)

############ 2. Service account: no shell, home holds the cache fallback
if id -u "$AppUser" >/dev/null 2>&1; then
    echo "user $AppUser already exists"
else
    useradd --system --user-group --create-home --home-dir "/var/lib/$AppUser" --shell /usr/sbin/nologin "$AppUser"
fi

############ 3. Virtual environment and the app, pinned by requirements.lock          (ADR-0001)
install -d -m 755 "$AppDir"
[ -x "$AppDir/.venv/bin/python" ] || python3 -m venv "$AppDir/.venv"                  # UD-31: create once
"$AppDir/.venv/bin/python" -m pip install --quiet -c "$GitOpsDir/requirements.lock" "$GitOpsDir"
"$AppDir/.venv/bin/python" -m pip show stock-digest | sed -n 's/^Version: /installed stock-digest /p'
echo "from $GitOpsDir at commit $(git -C "$GitOpsDir" rev-parse HEAD)"               # what was installed (UD-50 spirit)

############ 4. Units and Caddy configuration from deploy/. No secret file is created here. (UD-16)
install -m 644 "$Deploy/stock-digest.service" /etc/systemd/system/stock-digest.service
install -D -m 644 "$Deploy/caddy.service.d/stock-digest.conf" /etc/systemd/system/caddy.service.d/stock-digest.conf   # loads the domain file; drops --environ
install -o root -g caddy -m 640 "$Deploy/Caddyfile" /etc/caddy/Caddyfile
( umask 027; printf 'STOCK_DIGEST_DOMAINS=%s\n' "$StockDigestDomains" >/etc/caddy/stock-digest.env )   # read by systemd, never sourced: the value holds a space
chown root:caddy /etc/caddy/stock-digest.env
install -d -m 700 /etc/stock-digest                        # app.env lands here later, mode 600 (ADR-0003)

############ 5. Helpers, installed once and invoked as needed                        (UD-31, ADR-0003, ADR-0006)
install -m 755 "$Deploy/stock-digest-activate.sh" /usr/local/sbin/stock-digest-activate
install -m 755 "$Deploy/stock-digest-ports.sh"    /usr/local/sbin/stock-digest-ports      # holds the web-port list (ADR-0006)

############ 6. Ingress: add the web ports to the set; never edit the ruleset         (UD-14, B-7, ADR-0006)
/usr/local/sbin/stock-digest-ports                          # FW4 and FW5 verify; in tty mode a failure continues to them (UD-40)
install -D -m 644 "$Deploy/nftables.service.d/stock-digest.conf" /etc/systemd/system/nftables.service.d/stock-digest.conf   # runs stock-digest-ports after each start; ADR-0006

############ 7. Enable both services; start them only once the secrets exist          (UD-16, ADR-0003)
systemctl daemon-reload
systemctl enable stock-digest caddy
if [ -s /etc/stock-digest/app.env ] && [ -s /etc/caddy/stock-digest.users ]; then
    /usr/local/sbin/stock-digest-activate
else
    echo "secrets not on the box yet: services enabled, not started. Next: deploy/push-secrets.sh root@<this host> from your workstation."
fi

############ Verification tail: ask each object directly; include the negatives       (UD-53, B-24, B-25)
"$AppDir/.venv/bin/python" -I -c 'import sys, stock_digest.web as m; sys.exit(0 if m.__file__.startswith(sys.prefix + "/") else 1)' 2>/dev/null \
    && Pass AP1 "venv imports stock_digest.web from its own site-packages" \
    || Fail AP1 "venv cannot import stock_digest.web from its own site-packages"   # -I: the cwd is the source tree, keep it off sys.path (B-25)
PyV=$("$AppDir/.venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0.0)
"$AppDir/.venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null \
    && Pass AP2 "venv python $PyV >= 3.11 (pyproject requires-python)" || Fail AP2 "venv python $PyV < 3.11"
id -u "$AppUser" >/dev/null 2>&1 && Pass AP3 "service account $AppUser exists" || Fail AP3 "service account $AppUser missing"
[ "$(systemctl is-enabled stock-digest 2>/dev/null)" = enabled ] && Pass SV1 "stock-digest.service enabled" || Fail SV1 "stock-digest.service not enabled"
[ "$(systemctl is-enabled caddy 2>/dev/null)" = enabled ]        && Pass SV2 "caddy.service enabled"        || Fail SV2 "caddy.service not enabled"
read -r CMaj CMin < <(caddy version 2>/dev/null | sed -nE 's/^v?([0-9]+)\.([0-9]+).*/\1 \2/p') || { CMaj=0; CMin=0; }
if [ "$CMaj" -gt 2 ] || { [ "$CMaj" -eq 2 ] && [ "$CMin" -ge 8 ]; }; then
    Pass CD0 "caddy $CMaj.$CMin >= 2.8 (basic_auth directive)"
else
    Fail CD0 "caddy $CMaj.$CMin < 2.8"
fi
Probe=$(mktemp -d)                                          # the real users file is a secret (UD-16): validate against a throwaway one
cp /etc/caddy/Caddyfile "$Probe/Caddyfile"
printf 'probe %s\n' "$(caddy hash-password --plaintext probe-only 2>/dev/null)" >"$Probe/stock-digest.users"
caddy validate --config "$Probe/Caddyfile" --adapter caddyfile --envfile /etc/caddy/stock-digest.env >/dev/null 2>&1 \
    && Pass CD1 "Caddyfile validates for '$StockDigestDomains'" || Fail CD1 "Caddyfile does not validate with /etc/caddy/stock-digest.env"
rm -rf "$Probe"
nft get element inet filter TcpOK '{ 80 }'   >/dev/null 2>&1 && Pass FW4 "TcpOK contains 80"  || Fail FW4 "TcpOK missing 80"
nft get element inet filter TcpOK '{ 443 }'  >/dev/null 2>&1 && Pass FW5 "TcpOK contains 443" || Fail FW5 "TcpOK missing 443"
nft get element inet filter TcpOK '{ 8765 }' >/dev/null 2>&1 && Fail FW6 "TcpOK exposes 8765: the app must stay behind Caddy" || Pass FW6 "8765 not in TcpOK"
nft get element inet filter TcpOK '{ 2019 }' >/dev/null 2>&1 && Fail FW7 "TcpOK exposes 2019: Caddy's admin API"            || Pass FW7 "2019 not in TcpOK"
systemctl show -p ExecStartPost --value nftables | grep -qF 'argv[]=/usr/local/sbin/stock-digest-ports ;' \
    && Pass FW8 "nftables.service runs stock-digest-ports after each start" \
    || Fail FW8 "nftables.service has no stock-digest-ports step"   # the loaded unit, not the file (B-25); match string not yet seen on Rocky 10
nft list chain inet filter input | grep -q 'hook input .*policy drop;' && Pass FW9 "input policy still drop" || Fail FW9 "input policy is not drop"
if [ -e /etc/stock-digest/app.env ]; then
    [ "$(stat -c %a /etc/stock-digest/app.env)" = 600 ] && Pass SEC1 "app.env is mode 600" || Fail SEC1 "app.env is mode $(stat -c %a /etc/stock-digest/app.env), want 600"
else
    Pass SEC1 "no app.env on the box yet; it arrives through push-secrets (UD-16)"
fi
Scan=("$GitOpsDir/stages")
[ -f "$UDdir/UserData.sh" ] && Scan+=("$UDdir/UserData.sh")  # the self-copy is absent on the paste and pipe entry paths
KeyRe='(sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|^[[:space:]]*(export[[:space:]]+)?[A-Z_]*API_KEY=[^[:space:]]+)'   # known shapes, or any *API_KEY assignment
Rc=0; grep -rlE "$KeyRe" "${Scan[@]}" >/dev/null 2>&1 || Rc=$?   # UD-41: keep the status; a bare exit 1 would stop the stage under errexit
case $Rc in
    0) Fail SEC2 "key-like string in ${Scan[*]} (UD-16)" ;;
    1) Pass SEC2 "no key-like strings in ${Scan[*]}" ;;
    *) Fail SEC2 "could not scan ${Scan[*]}: grep exit $Rc" ;;   # grep exits 2 on an unreadable input even when another matched
esac
VerifySummary || { echo "stockdigest verification failed; the stage log stays .partial" >&2; exit 70; }
} >>"$StageLog" 2>&1
StageEnd stockdigest
if ! { [ -s /etc/stock-digest/app.env ] && [ -s /etc/caddy/stock-digest.users ]; }; then
    printf 'Stock Digest: built, AWAITING SECRETS. From your workstation: deploy/push-secrets.sh root@<this host>\n' >>/etc/motd   # UD-25
fi
