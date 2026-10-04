# ADR-0006  One installed script owns the web ports and is called from all three places

Date:          2026-10-04
Status:        Accepted
Supersedes:    ADR-0002
Intents:       UD-31 (write once, invoke as needed), UD-14 and B-7 (open ports by adding set
               elements; never edit the ruleset), UD-41 (check results explicitly), UD-53 and B-25
               (a check asks the one object it names). No intent covers helpers that run outside
               the stage protocol, such as a start hook of a system unit; this record covers that.

Context:       ADR-0002 kept 80 and 443 in TcpOK across reboots with an ExecStartPost line in an
               nftables drop-in, and repeated the get-then-add loop in the stage and in
               stock-digest-activate. A Tutorial §8 review on 2026-10-04 marked item 14 FAIL:
               UD-31's violation line names copy-pasted blocks that drift, and the port list
               lived in three places. The same review marked FW8 under item 12 FAIL, because it
               grepped the drop-in file rather than asking systemd.
Decision:      deploy/stock-digest-ports.sh, installed by the stage to
               /usr/local/sbin/stock-digest-ports, holds the only port list that acts on the
               firewall. For each port it asks the set with `nft get element`, adds the port only
               if absent, asks again, and exits non-zero if any port is still missing. The stage
               runs it in step 6. stock-digest-activate runs it before any restart and refuses on
               failure. The nftables drop-in runs it as ExecStartPost.
               It sources no prelude. A start hook of the firewall unit must not depend on
               /srv/git-ops, which every deploy rewrites, so the script checks every result itself.
               FW4 and FW5 in the stage keep naming 80 and 443: a test that read the list from the
               script would pass whatever the script says. FW8 asks systemd's loaded unit for the
               ExecStartPost line instead of grepping the drop-in file.
Alternatives:  (a) Keep three copies, as ADR-0002 did. Rejected under UD-31.
               (b) One nft file run by all three, using `destroy element` then `add element` in
                   one transaction. No script needed, but it relies on the newer `destroy`
                   command, which nothing here has tested.
               (c) Have the script source lib/safeErr.sh like the stage and the activation helper.
                   Rejected for the dependency above; the prelude's tty mode would also trace and
                   continue, which this script has no use for.
Consequences:  The port list lives in one script. Opening another port means one edit there plus a
               new FWn check in the stage. If the script fails as ExecStartPost, nftables.service
               is marked failed; systemd does not run ExecStop after a failed start, so the
               SSH-only ruleset stays loaded. `systemctl reload nftables` still drops the web
               ports, as under ADR-0002; running stock-digest-ports or stock-digest-activate
               repairs the set. FW8's match string follows systemd's `show` output format and has
               not yet been seen on Rocky 10; confirm it on the first Droplet build.
