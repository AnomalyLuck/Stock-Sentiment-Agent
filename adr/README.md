# adr/

Decision records for deploying Stock Digest with the UserData kit. A record is written the day a
decision is made that the kit's intents (`UD-n`) and bindings (`B-n`) do not cover. Numbers are
never reused or renumbered; a replaced record is marked `Superseded by ADR-NNNN` and kept.

Each record states: date, status, the IDs it touches (or "none cover this"), context, decision,
alternatives considered, and consequences.

| ADR | Title | Date | Status |
|---|---|---|---|
| [0001](ADR-0001-install-prefix-opt.md) | Install the app under /opt/stock-digest | 2026-10-04 | Accepted |
| [0002](ADR-0002-persist-web-ports.md) | Re-add the web ports to TcpOK after every nftables start | 2026-10-04 | Superseded by 0006 |
| [0003](ADR-0003-secrets-after-baseline.md) | Secrets arrive over SSH after the build and gate service start | 2026-10-04 | Accepted |
| [0004](ADR-0004-public-gitops-repo.md) | This repository is the git-ops repo, public, with kit files kept local | 2026-10-04 | Accepted |
| [0005](ADR-0005-workstation-script-errexit.md) | Workstation scripts use errexit as a backstop over explicit checks | 2026-10-04 | Accepted |
| [0006](ADR-0006-one-port-script.md) | One installed script owns the web ports and is called from all three places | 2026-10-04 | Accepted |
