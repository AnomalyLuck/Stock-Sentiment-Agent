#!/bin/bash
# stock-digest-ports — put Stock Digest's web ports in the baseline's TcpOK set. The one copy of the list.
#
# Installed by stages/StockDigest.sh to /usr/local/sbin/stock-digest-ports (UD-31: written once, invoked
# as needed). Invoked by the stage, by stock-digest-activate before it restarts anything, and by
# nftables.service after every start through deploy/nftables.service.d/stock-digest.conf (ADR-0006).
# Adds set elements only and never edits the ruleset (UD-14, B-7). Safe to re-run.
#
# Sources no prelude: it runs as a start hook of the firewall unit, which must not depend on the git
# checkout in /srv/git-ops that every deploy rewrites. It checks every result itself (UD-41).
# IDs: UD-n and B-n refer to the UserData kit's intents and bindings; ADR-n to this repo's adr/.

Ports="80 443"                                              # ADR-0006: the only list that acts; the stage's FW4/FW5 restate it as tests
Missing=0
for Port in $Ports; do
    nft get element inet filter TcpOK "{ $Port }" >/dev/null 2>&1 \
        || nft add element inet filter TcpOK "{ $Port }" \
        || echo "stock-digest-ports: nft could not add $Port" >&2    # get first: a re-run never re-adds an element (ADR-0006)
    nft get element inet filter TcpOK "{ $Port }" >/dev/null 2>&1 \
        || { echo "stock-digest-ports: $Port is not in TcpOK" >&2; Missing=$((Missing+1)); }   # B-25: exit status is membership
done
[ "$Missing" -eq 0 ] || exit 1                              # UD-41: callers check this; activate refuses, the stage's FW4/FW5 report
echo "stock-digest-ports: TcpOK holds $Ports"
