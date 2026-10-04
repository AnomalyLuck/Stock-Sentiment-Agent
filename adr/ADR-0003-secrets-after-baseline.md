# ADR-0003  Secrets arrive over SSH after the build and gate service start

Date:          2026-10-04
Status:        Accepted
Intents:       UD-16 (no secrets in UserData; they arrive by a separate channel after the
               baseline), UD-31 (install a helper once, invoke it), UD-25, UD-53. No intent
               names the channel.

Context:       The app needs OPENAI_API_KEY and optional provider keys; Caddy needs bcrypt login
               hashes. None may appear in UserData.sh, the repository, or the Droplet's metadata.
               The user still wants one UserData paste to produce a running site.
Decision:      The stage creates /etc/stock-digest (mode 700), installs and enables both units
               without starting them, and installs /usr/local/sbin/stock-digest-activate. From
               the workstation, deploy/push-secrets.sh sends app.env and stock-digest.users over
               one SSH session as a tar stream, installs them with modes 600 root:root and 640
               root:caddy, and runs the helper. The helper rejects placeholder values, a blank
               STOCK_DIGEST_CACHE_DIR, `export` lines and malformed hashes; re-adds the web
               ports; validates Caddy; restarts both services; verifies a 200 on loopback, a
               loopback-only bind, a redirect on 80 and a 401 on 443; and rewrites the MOTD. It
               is re-runnable by design and keeps no completion stamp, since UD-52 covers
               run-once scripts.
Alternatives:  (a) Secrets in UserData. Violates UD-16.
               (b) A systemd path unit that starts the services when app.env appears. Starts
               automatically but skips verification and hides a bad file until the first
               request fails.
               (c) Fetch secrets from a provider store at boot. Adds a provider dependency and a
               credential to fetch the credentials.
Consequences:  One manual step per Droplet after the build. `EnvironmentFile=` without a `-`
               prefix makes systemd refuse to start the app while app.env is missing, which is
               the intended state. On later boots both services start on their own because they
               are enabled and the files persist.
