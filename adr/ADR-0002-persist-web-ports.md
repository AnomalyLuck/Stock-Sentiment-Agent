# ADR-0002  Re-add the web ports to TcpOK after every nftables start

Date:          2026-10-04
Status:        Superseded by ADR-0006 (2026-10-04)
Intents:       UD-14, B-7 (open ports by adding set elements; never edit the ruleset). No intent
               covers keeping those elements across a ruleset reload or a reboot.

Context:       /etc/nftables.conf is loaded at every boot with `elements = { 22 }`. Ports 80 and
               443 added with `nft add element` live only in the kernel, so the first reboot
               would close the site while every log said the build passed.
Decision:      deploy/nftables.service.d/stock-digest.conf, installed by the stage, sets
               `ExecStartPost=/usr/sbin/nft add element inet filter TcpOK { 80, 443 }`. The stage
               adds the ports now, one `get element` then `add element` per port so a re-run on an
               interval set does not fail on a duplicate, and installs the drop-in.
               stock-digest-activate repeats the get-then-add, so an activation also repairs
               the set.
Alternatives:  (a) Edit the ruleset's element list. Forbidden by UD-14.
               (b) A separate oneshot unit ordered after nftables.service. Same effect, one more
               unit, no gain.
               (c) An `include` of a per-project file from the ruleset. Needs one edit to the
               baseline ruleset; rejected for the same reason as (a).
Consequences:  A reboot keeps the site reachable. `systemctl reload nftables` runs ExecReload,
               not ExecStartPost, so a reload closes 80 and 443 until the next restart or
               activation; the drop-in says so. The port list appears in the drop-in, the stage
               and the activation helper; changing it means changing all three.
