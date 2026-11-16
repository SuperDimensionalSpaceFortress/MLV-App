---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/public-repo-fetch-is-not-auth-proof
rule: a successful fetch, clone or anonymous ls-remote of a PUBLIC repo never proves a credential works, because a public repo answers those with no credential at all. Each credential is proved only by an authenticated call over the transport that will use it: `gh api user` for the gh API token, a push dry-run over the push URL for the push credential. One never proves the other: an SSH push key and a gh token fail independently. A stored credential listing is not proof either.
mechanism: on 2026-11-15 on one hub host the gh OAuth token and the Git Credential Manager token were invalidated between about 05:07Z and 05:20Z (HTTP 401 from 05:19:46Z) while https fetches of the public fork kept working, so the board read healthy. Pushes failed from 06:00Z. Fixed by remote.fork.pushurl over SSH plus the owner's gh sign-in; the hub now probes `gh api user` each beat (LIVE, REVOKED or UNKNOWN; only REVOKED alerts). Prior art, TRAPS.md: "Local credential state reads ALIGNED after the server revoked the token" and "`git ls-remote origin` checks the fetch URL while `git push` uses pushurl". This adds the public-repo case.
applies: mlv-app
check: board-local, not pushed, PowerShell 7 on Windows only: the offline guard hooks/tests/Test-GhAuthProbe.ps1 (sha256 db8ad38a05372f085b13eb394fe2d57068a3a977d4474db3f466387962e8782f at the green run) ran 67 of 67 green on 2026-11-16: 401 and logged out give REVOKED; 403, rate limit, 5xx, network, timeout and gh missing give UNKNOWN; one live `gh api user` gave LIVE. It covers the gh API token only, not the SSH push key. Staged for the owner to install. No tracked test covers this clause.
supersedes: none
evidence: measured
