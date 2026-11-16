---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/public-repo-fetch-is-not-auth-proof
rule: a successful fetch, clone or anonymous ls-remote of a PUBLIC repo is never proof that a credential works, because a public repo answers those without one. Only an authenticated call (`gh api user`) or a push dry-run over the push transport proves the credential that the next push or API call will use. A local status read (`gh auth status`, a credential helper listing) is not proof either: it reads what is stored, not what the server accepts.
mechanism: on 2026-11-15 on one hub host both the gh OAuth token and the Git Credential Manager token were invalidated between about 05:07Z and 05:20Z (gh API calls returned HTTP 401 from 05:19:46Z). Fetches of the public fork over https kept working, so the board read healthy. Pushes failed from 06:00Z. The fix was remote.fork.pushurl over SSH plus the owner's own gh sign-in. The hub now probes `gh api user` each beat: LIVE, REVOKED (401 or logged out) or UNKNOWN, and only REVOKED alerts.
prior art: TRAPS.md "Local credential state reads ALIGNED after the server revoked the token" (local state is not server state) and TRAPS.md "`git ls-remote origin` checks the fetch URL while `git push` uses pushurl" (a read over one transport says nothing about the other). This entry adds the public-repo case: an anonymous read succeeds with no credential at all.
applies: mlv-app
check: board-local, not pushed, PowerShell 7 on Windows only: the offline guard hooks/tests/Test-GhAuthProbe.ps1 (sha256 db8ad38a05372f085b13eb394fe2d57068a3a977d4474db3f466387962e8782f at the green run) ran 67 of 67 green on 2026-11-16: 401 and logged out give REVOKED; 403, rate limit, 5xx, network, timeout and gh missing give UNKNOWN; one live `gh api user` gave LIVE. It is staged for the owner to install, not yet at that path. No tracked test covers this clause.
supersedes: none
evidence: measured
