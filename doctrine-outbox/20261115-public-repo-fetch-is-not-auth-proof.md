---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/public-repo-fetch-is-not-auth-proof
rule: a successful HTTPS fetch, HTTPS clone or anonymous HTTPS ls-remote of a PUBLIC repo never proves a credential works, because a public repo answers those with no credential at all. An SSH fetch does authenticate the key, and proves only the key. Each credential is proved only by an authenticated call over the transport that will use it: `gh api user` for the gh API token, a push dry-run over the push URL for the push credential. One never proves the other: an SSH push key and a gh token fail independently. A stored credential listing is not proof either.
mechanism: on 2026-11-15 on one hub host the gh OAuth token was invalidated between about 05:07Z and 05:20Z (HTTP 401 from 05:19:46Z) and the Git Credential Manager token by 06:00Z (inferred), while https fetches of the public fork kept working, so the board read healthy. Pushes failed from 06:00Z. The first fix, remote.fork.pushurl over SSH plus the owner's gh sign-in, lasted about 4.5 hours. A hub meter that probes `gh api user` (LIVE, REVOKED or UNKNOWN) is staged and stays dormant until the owner installs GhAuthProbe.psm1; once armed, REVOKED alerts at once and UNKNOWN alerts after more than 2 beats. Prior art, TRAPS.md: "Local credential state reads ALIGNED after the server revoked the token". This adds the public-repo case.
applies: mlv-app
check: board-local, not pushed, PowerShell 7 on Windows only: the offline guard hooks/tests/Test-GhAuthProbe.ps1 (sha256 db8ad38a05372f085b13eb394fe2d57068a3a977d4474db3f466387962e8782f at the green run) ran 66 of 66 green offline on 2026-11-16 (67 of 67 with one live call): 401 and logged out give REVOKED; 403, rate limit, 5xx, network, timeout and gh missing give UNKNOWN; the live `gh api user` gave LIVE. It covers the gh API token only, not the SSH push key. Staged for the owner to install. No tracked test covers this clause.
supersedes: none
evidence: measured
