# ADR-0001  Install the app under /opt/stock-digest

Date:          2026-10-04
Status:        Accepted
Intents:       None cover the install prefix of application software. UD-20 and UD-21 govern
               build state; B-19 fixes where the fetched repository lands (/srv/git-ops).

Context:       The service needs a virtual environment and a working directory. The repository is
               fetched to /srv/git-ops as the build and provenance record (B-19), and /srv is the
               tree whose contents later migrate to block storage (UD-20 rationale). Neither
               describes installed software. deploy/stock-digest.service and the README already
               used /opt/stock-digest from the earlier Ubuntu instructions.
Decision:      stages/StockDigest.sh creates the venv at /opt/stock-digest/.venv and installs the
               package from /srv/git-ops with requirements.lock as constraints. The unit file is
               unchanged: WorkingDirectory and ExecStart point at /opt/stock-digest.
Alternatives:  (a) A venv inside /srv/git-ops. Rejected: it mixes installed software into the
               fetched repository and into the tree meant to migrate. Under SELinux enforcing,
               files under /srv carry var_t, which systemd will not execute; /opt carries usr_t,
               which runs as unconfined_service_t.
               (b) /usr/local/lib/stock-digest. Also FHS-conformant; rejected because it buys
               nothing over the path the unit and README already use.
Consequences:  Stage, unit and README agree. Updating code is: re-fetch in /srv/git-ops, pip
               install into /opt/stock-digest/.venv, restart. The kit's "your defaults that are
               wrong here" row about /opt concerns build workspace, not this.
