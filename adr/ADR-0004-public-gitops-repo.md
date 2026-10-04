# ADR-0004  This repository is the git-ops repo, public, with kit files kept local

Date:          2026-10-04
Status:        Accepted
Intents:       UD-51, B-18 (source host is a variable; private stages off third-party hosts),
               B-19 (stages/<Project>.sh at the repo root), UD-16. Also the kit's license, which
               forbids placing kit files, original or modified, on GitHub.

Context:       The loader fetches `stages/<Project>.sh` from GitOpsUrl with no credentials. The
               app repository was private on GitHub. A token in UserData would violate UD-16.
               Self-hosted git is not available today.
Decision:      The app repository doubles as the git-ops repository, with `stages/` at its root,
               and is made public. Kit files and their derivatives stay on the workstation only
               and are gitignored: CLAUDE.md, UserData.sh, lib/, stages/README.md, Intent.md,
               intents/, BINDINGS.md, Tutorial.md. UserData.sh is pasted into the Droplet by hand
               and is never fetched. The loader's bootstrap copy of lib/safeErr.sh serves the
               stage on the box; B-19 notes the repository need not track it.
Alternatives:  (a) A read-only token in GitOpsUrl. Violates UD-16; also readable by any process
               on the box through the metadata service.
               (b) A second, public git-ops repository. Two repositories to keep in step, and the
               stage still needs the app source.
               (c) Self-hosted git (B-18's plan). Not available now; nothing here prevents
               switching GitOpsUrl to it later.
Consequences:  Everything tracked becomes readable by anyone. The history was scanned on
               2026-10-04: no keys, tokens, hashes or env files were ever committed. The local
               lib/safeErr.sh and the heredoc copy inside UserData.sh must change together, as
               the kit requires. A fresh clone lacks CLAUDE.md and the kit documents, by design.
