# ADR-0005  Workstation scripts use errexit as a backstop over explicit checks

Date:          2026-10-04
Status:        Accepted
Intents:       None cover scripts that run on the workstation. UD-40, UD-41 and B-13 scope to
               UserData and the stages that run on the box under the stage protocol.

Context:       deploy/push-secrets.sh runs on the operator's Mac to send the two secret files to
               a Droplet and run stock-digest-activate there. The kit rejects
               `set -euo pipefail` "as the whole error strategy" for on-box scripts, because
               bash disables errexit inside conditionals and the stage wrapper (UD-41), and
               binds those scripts to the shared prelude lib/safeErr.sh (UD-40, B-13). The
               workstation script has no stage wrapper, no stage log and no /srv paths.
Decision:      Keep `set -euo pipefail` in deploy/push-secrets.sh, with every meaningful check
               written out explicitly: file tests and placeholder greps each end in their own
               `exit 2`, and the remote half runs under the Droplet's `set -e` behind an
               explicit guard that the stage completed. Errexit covers only what remains, such
               as the `cd` in the default secrets-dir assignment. The script's header cites this
               record.
Alternatives:  (a) Source lib/safeErr.sh from the repository. Rejected: the file is gitignored
               (ADR-0004), so a fresh clone would break; in tty mode the prelude traces every
               command and continues past errors, the opposite of what a script that must refuse
               a bad secrets file needs; and its stage helpers write to paths that exist only on
               the box. Doing this would fuse workstation tooling with the on-box stage protocol.
               (b) Remove errexit and test every result, including the tar|ssh pipeline through
               PIPESTATUS. Same behavior in practice for a longer script; nothing gained.
Consequences:  Workstation-side tooling follows ordinary bash practice while on-box scripts
               follow the kit. A reviewer applying Tutorial §8 should treat item 9 as N/A for
               files under deploy/ that run on the workstation, and this record says why. If
               the kit later adds an intent for operator tooling, revisit.
