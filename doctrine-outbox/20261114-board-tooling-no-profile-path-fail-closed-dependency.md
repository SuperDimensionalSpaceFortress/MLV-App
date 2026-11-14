---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/board-tooling-no-profile-path-fail-closed-dependency
rule: board tooling carries no user-profile path literal; it resolves the profile root from the environment when it launches. A missing optional dependency may fall back only when it is provably absent (the probe throws ItemNotFound and nothing else). A dependency that is present but wrong (hash drift, an empty directory, a file where a directory belongs, an unset or relative profile variable, access denied) fails closed and never falls back, because drift is the security signal.
mechanism: on 2026-11-14 the queue drain's three launch sites hard-coded one account's profile path to a pinned launcher package. On a second host and account the path did not exist, so every site held and nothing launched. One resolver now serves all three sites: path absent gives a headless fallback, path present runs the pinned code unchanged, any other outcome throws. Unknown is never read as absent.
applies: mlv-app
check: board-local, not pushed, PowerShell 7 on Windows only: the offline guard hub-autopilot/Test-QueueHeadlessTransport.ps1 (sha256 25fb78deaf6994109ee9f2563eaee905c6df825f512f5ec4031a4037a0f75b5b at the green run) ran 19 of 19 green on 2026-11-14, including S1 (no drive-letter Users literal in the script) and C1 to C4 (drift, empty directory, a file, an unusable profile variable all fail closed). Its live check needs a desktop and was not run here. No tracked test covers this clause.
supersedes: none
evidence: measured
