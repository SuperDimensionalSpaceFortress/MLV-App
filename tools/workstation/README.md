# MLV workstation bootstrap and hub cut-over

`Setup-MlvWorkstation.ps1` builds a dedicated MLV-App box that mirrors the VIRTUAL-TEN fleet hub,
moves the machine-level Claude setup across without secrets, and moves the hub with a
**one-host-active lease** so the board is never driven from two hosts at once.

Every phase is idempotent and logs to `C:\ProgramData\MlvWorkstation\logs\`. Add `-WhatIf` to any
run for a dry run. The new-machine phases refuse to run on VIRTUAL-TEN unless you pass `-Force`.

| Phase | Where | What |
|---|---|---|
| `Host` | new | long paths, power (never sleep on AC), w32time, Defender exclusions, optional `-PagefileMaxGB` |
| `Layout` | new | `C:\!Layi Wkspc`, `C:\mlvtmp` (optional `-MlvTmpTarget D:\mlvtmp` junction), `C:\ProgramData\MlvWorkstation` |
| `Packages` | new | winget: pwsh 7, Git, gh, Python install manager + 3.14, Node LTS, VS Code, Claude Desktop, ChatGPT, Codex desktop, Tailscale, ripgrep, 7-Zip; npm: Claude Code CLI, Codex CLI, codex lane pin 0.160.1 |
| `Ssh` | new | sshd (key-only, pwsh default shell, port 22 from LAN + Tailscale), keys for GitHub and BACHELOR |
| `Clone` | new | MLV-App (`fork` = SuperDimensionalSpaceFortress, `origin` = ilia3101, master tracks fork/master), doctrine bus. **Push over SSH (SETUP-MLVWORKSTATION-SSH-PUSHURL-1):** when `ssh -T git@github.com` authenticates (BatchMode, bounded, never interactive; the probe runs with its own generated `-F` config, so your `~/.ssh/config` is not parsed and no `Match exec` runs, and `StrictHostKeyChecking=yes` + `CheckHostIP=no` + `UpdateHostKeys=no`, so `~/.ssh` is not written; the probe offers only the `id_ed25519_github` key the Ssh phase creates (`-i`, `IdentitiesOnly yes`), when it exists, so a key your own config assigns to github.com is not what is probed), `remote.fork.pushurl` is set to `git@github.com:<owner>/<repo>.git` and the https fetch url stays; prints `SSH_PUSH_READY remote=fork pushurl=<url>`. If ssh does not authenticate: one owner-only MANUAL line `SSH_KEY_UNREGISTERED` and nothing changes (the script never creates, copies, prints or registers a key). A pushurl that is neither the https url nor the SSH form is left alone (`PUSHURL_FOREIGN`); a `url.<base>.insteadOf` rule over either url, a url that is also a remote name, or a fork remote that names another repository is `PUSHURL_REFUSED`. The setting lives in the repo's own config file, so a Codex lane with another HOME pushes the same way (a push url that only a global, system or `include.path` file supplies is `PUSHURL_FOREIGN`, never ready; the probe config is never written under a `.ssh` folder, nor through a junction or symlink into one); `credential.helper` is not changed by this. Only `fork` is handled (`origin` is the upstream, never pushed to) |
| `Qt` | new | Qt 6.10.2 win64_mingw + MinGW 13.1 via the repo's hash-locked aqtinstall |
| `Import` | new | unpacks the transfer bundle into `~\.claude`, `~\bin`, `~\.codex`, FleetDefender, `.claude-state`, `.codex-state\doctrine` (never overwrites a newer local file); **never copies a credential:** a bundle file that carries a credential or is over 25 MiB, and a bundle file that would replace a host file carrying a credential (any name) or a credential-role file (`settings.json`, `config.toml`, ...) naming a credential key, is skipped and logged `IMPORT_SKIPPED_CREDENTIAL_FILE`; a host file it cannot lock exclusively (including one another process holds with a share mode that denies readers, `FileShare.None` or a writer that denies Read: that file is never classed as carrying a credential), or that gained a credential between inspection and replace, is kept and reported with a typed `IMPORT_DEST_CHANGED_SKIPPED <path>` line, and the step then fails (exit 1); the `settings.json` profile rewrite is skipped when that file was skipped; moves hub claims whose run dir is absent here and that are under 24 h old to `claims\migrated-foreign-<stamp>` |
| `Tasks` | new | registers 8 scheduled tasks, **disabled**, behind the gate |
| `Verify` | either | report, plus checks that change the exit code. **Fork push url:** when ssh authenticates and `remote.fork.pushurl` is not the SSH form, `SSH_PUSHURL_MISSING remote=fork` (an ssh fetch url with no explicit `remote.fork.pushurl` counts: exactly one pushurl in the repository's own config is required, and Clone writes it; also when the clone has no `fork` remote at all; or `PUSHURL_FOREIGN remote=fork`, or `PUSHURL_REFUSED remote=fork` when a rewrite rule or another repository stops the check) fails Verify; a probe that does not authenticate is a MANUAL `SSH_KEY_UNREGISTERED` line, not a failure. **Share preflight, fail closed:** both `\\Ultra-Magnus\g\Temp\mlv-gpu-profile\agent\heartbeat.txt` and `\\bachelor\mlv-agent\heartbeat.txt` must be readable, else `SHARE_PREFLIGHT_FAILED <path> (<host> share; ...step 6b)` and exit 1 (README step 6b). **Meter installs:** runs `-Install` of `MLV-DiskAutoReclaim` and `MLV-PwshAutoRepin` (not on the source host without `-Force`, not under `-WhatIf`). A meter task that was Disabled or absent before its `-Install` is put back Disabled afterwards (`METER_TASK_RESTORED_DISABLED`; `METER_TASK_LEFT_ENABLED` and exit 1 if that fails), one the owner had enabled is left alone; a host without the SBP launcher reports `METER_INSTALL_UNSUPPORTED_HOST` and goes on |
| `Checklist` | either | owner-only checklist (printed, never scripted) |
| `Export` | VIRTUAL-TEN | sanitized bundle under `C:\mlvtmp\migrate-out` (read-only towards its tasks and hooks) |
| `LeaseShow/Acquire/Release` | either | the hub lease |

## Steps on the new box

1. **Get the script there and install pwsh.** Copy `Setup-MlvWorkstation.ps1` to `C:\MlvSetup\` (USB,
   OneDrive, or the raw GitHub URL once it is merged). In an **elevated Windows PowerShell**:

   ```powershell
   winget install --id Microsoft.PowerShell -e --accept-package-agreements --accept-source-agreements
   ```

   Sign in to Windows as **`obabalola`** if you can. The tracked `.claude\settings.json` hard-codes
   `C:/Users/obabalola/AppData/Local/Python/bin/python.exe`, so project hooks only work under that name.

2. **Run the bootstrap** in an **elevated pwsh 7**:

   ```powershell
   pwsh -NoProfile -ExecutionPolicy Bypass -File "C:\MlvSetup\Setup-MlvWorkstation.ps1" -PagefileMaxGB 32
   ```

   Re-run it as often as needed. A new shell after the first run picks up PATH changes.

3. **Let VIRTUAL-TEN in over SSH** (key-only). On VIRTUAL-TEN, in normal pwsh:

   ```powershell
   ssh-keygen -t ed25519 -f "C:\Users\obabalola\.ssh\id_ed25519_mlvbox" -C "obabalola@VIRTUAL-TEN-mlvbox"
   ```

   ```powershell
   Get-Content "C:\Users\obabalola\.ssh\id_ed25519_mlvbox.pub" | Set-Clipboard
   ```

   On the new box (elevated), paste it in:

   ```powershell
   pwsh -NoProfile -File "C:\MlvSetup\Setup-MlvWorkstation.ps1" -Phase Ssh -AuthorizedKey "<paste the ssh-ed25519 line>"
   ```

   Add a `Host mlvbox` block (HostName = the new box's Tailscale or LAN name, `User obabalola`,
   `IdentityFile ~/.ssh/id_ed25519_mlvbox`, `IdentitiesOnly yes`) to `C:\Users\obabalola\.ssh\config`
   on VIRTUAL-TEN, then test it with `ssh mlvbox hostname`.

4. **Move the machine setup.** On VIRTUAL-TEN (not elevated):

   ```powershell
   pwsh -NoProfile -File "C:\!Layi Wkspc\MLV-App\tools\workstation\Setup-MlvWorkstation.ps1" -Phase Export
   ```

   It prints the bundle path. Read `SECRET-SCAN.txt` and `reference\unpushed-worktree-branches.tsv`
   inside the staging folder, then copy the zip over (or use USB):

   ```powershell
   scp "C:\mlvtmp\migrate-out\mlv-migrate-bundle-<stamp>.zip" mlvbox:C:/mlvtmp/migrate-in/mlv-migrate-bundle.zip
   ```

   On the new box (elevated):

   ```powershell
   pwsh -NoProfile -File "C:\MlvSetup\Setup-MlvWorkstation.ps1" -Phase "Import,Tasks,Verify,Checklist"
   ```

   **What the bundle leaves out:** `.credentials.json`, `.claude.json`, `auth.json`, every ssh key,
   `*token*` / `*secret*` / `*credential*` files, session transcripts, `history.jsonl`, sqlite stores,
   logs, hook state dot-files, `identity\`, run directories and worktrees. Any staged file whose text
   matches a token pattern (Anthropic, OpenAI, GitHub, Slack, AWS, private key, `access_token`) or
   holds a secret-named key with a quoted value (TOML `"""` and `'''` strings included) is deleted
   from the bundle, **and Export then ends with exit 1**: it prints one
   `EXPORT_CREDENTIAL_HIT <path in the bundle>:<line>` line per file (never the matched value) and writes
   the same lines to `SECRET-SCAN.txt`. A file too large or unreadable to scan is
   `EXPORT_SCAN_DROPPED <path> (<why>)`. The zip is still written, **without those files**: a file the
   bundle lacks is a file the new host keeps stale, so read the list. A false positive (a script line
   `$Token = '<literal>'`, a ledger line quoting a synthetic password) is not fixed by loosening the
   detector: carry that file by hand, or reword the line, then re-export. Import never replaces a host file
   that carries a credential: it opens each host file it would replace exclusively, judges those bytes and
   writes through the same handle. A host file another process holds open, or one that gained a credential
   since the inspection, stays as it is (`IMPORT_DEST_CHANGED_SKIPPED <path>`, exit 1): close what holds it
   and re-run `-Phase Import` (it is idempotent). That holds for every share mode (r17): a holder that
   denies readers (`FileShare.None`, or a writer that denies Read) gives the same typed line and exit 1,
   never a quiet `IMPORT_SKIPPED_CREDENTIAL_FILE` warning with exit 0. A held host file that the copy
   rules would keep anyway (it is newer, `/XO`) is not opened and is not a failure. A host file Import
   cannot read for any other reason (access denied, over 25 MiB) still counts as carrying a credential.

   **No backup, no replace (IMPORT-BACKUP-UNREADABLE-MUST-NOT-REPLACE-1).** Before the copy, Import backs
   up the host `~/.claude/CLAUDE.md` and `settings.json` under `<StateRoot>\backups\home-claude-<stamp>`. A
   host file whose backup cannot be made (a write failure, a folder at the backup name, a held or unreadable
   file, a backups folder that cannot be created) is left byte for byte as it is: the bundle copy is not
   applied and the run records `IMPORT_BACKUP_UNAVAILABLE_NOT_REPLACED <path>` (exit 1); the log line
   `IMPORT_BACKUP_FAILED <path>` names the cause. The rule is tied to the backup that was written, not to a
   check made at one moment: a host file is replaced only while it is locked and still holds exactly the
   bytes that were backed up, so a file that was absent while the backup ran and came back, or one saved
   after the backup (even with an older timestamp than the bundle copy), is refused the same way. A file
   the copy rules would keep anyway (`/XO`, the bundle copy is older) is still kept with no failure, held
   or not. Fix what blocks the backup and re-run `-Phase Import`. A bundle
   zip that `tar` cannot expand completely (a truncated copy) is `IMPORT_BUNDLE_EXPAND_FAILED <path>` (exit 1):
   nothing is snapshotted or copied from it, so copy the zip again and re-run.

   **Remote URLs and git tracing (r17).** Every line the script prints or logs redacts a URL's user part
   up to the last `@` before the host, so an unencoded `@` in a password leaks nothing. It also drops
   everything after a `?` or `#` and prints `?***` (a query-string token, a fragment). Every native git
   call (and every child that may run git) starts with each `GIT_TRACE*` variable removed from its
   environment. A trace destination that names a tracked file is never appended to; the shell's own
   values are put back afterwards.

   **The credential rule is deny by default on the key NAME (r15).** A file is a hit when a secret-named
   key (password / passwd, secret, credential, api / private / access / auth key, token; a part `auth`,
   `pwd` or `authorization`; any separator or camelCase; braced or subscripted script keys such as
   `${env:DB_PASSWORD}` and `$config['password']` too) appears with ANY value. The value is the raw text
   to the end of its record, nothing stripped first. Only these values are exempt: empty, `""`, `''`,
   `null`, `~`, `[]`, `{}`, `***`, `${NAME}`, `<words>`, and an XML element with no content. Everything
   else is a hit, including `true` / `false`, a number under `max_tokens`, a mapping under `auth`, and
   `$Token = Get-Token`. Expect more files in `SECRET-SCAN.txt` than before: that is the accepted cost.
   A netrc-shaped file (`machine` / `default` / `login` / `macdef`) where `password` is followed by
   anything is a hit. The rule does not catch a key name built at run time or a value computed at run time.

   **Typed failures added in r15:** `EXPORT_CREDENTIAL_ZIP_NOT_DELETED <path>` means an archive that
   failed the post-archive scan could not be deleted, because something holds it open. Delete it by hand
   and never transfer it. `IMPORT_ROLLBACK_DEST_CHANGED <path>` means a save landed while Import was
   putting back an interleaved save. The newest save stays at the path. `IMPORT_ROLLBACK_BACKUP_NOT_DELETED`,
   `IMPORT_TEMP_NOT_DELETED`, `IMPORT_PARTIAL_NOT_DELETED` and `IMPORT_BACKUP_NOT_DELETED` each name a
   file Import could not remove. `CLONE_REMOTE_MISMATCH` and `CLONE_DOCTRINE_REMOTE_MISMATCH` mean an
   existing checkout's remote names another repository. Nothing is fetched; fix the remote by hand.
   (r17: a push url that names another repository is the same mismatch.) `LEASE_OBJECT_FAILED` means
   git could not build the lease commit (an empty `-GitName`, say), and `LEASE_PUSH_URL_DIFFERS` means the
   lease remote pushes somewhere other than where it reads. In both cases nothing is pushed.
   (r22: remote urls are read raw from `git config -z`, and the lease and Clone fetch and push the url
   strings they checked, never the remote name. `REMOTE_URL_UNSAFE` means a url holds a control
   character, starts with `-`, or is rewritten by a `url.<base>.insteadOf` rule; it is refused, never
   compared. `GATE_LOCK_IO_FAILED` (exit 5) means the gate lock file could not be opened for a reason
   other than another holder; the gate does not run its target and a release changes nothing.)
   (r23: a url that is also the name of a configured remote is `REMOTE_URL_UNSAFE` too, because git
   would resolve it as that remote. A url Clone adds, and the url of a first clone, are checked the
   same way before use.)
   (r26: `LEASE_LEGACY_REMOTE_ALIAS_PRESENT <path>` means an entry exists under `remotes\` or
   `branches\` of the repository's common or per-worktree git directory. Git can resolve a url through
   such a legacy alias, so the lease and Clone refuse and never parse it; move the entry aside.
   `LEASE_GIT_DIR_UNRESOLVED` means `git rev-parse --git-dir --git-common-dir` gave no usable answer, and
   the same refusal follows. A `://` url whose authority is not exactly `[userinfo@]host[:port]` (it
   holds `?`, `#`, `\` or a second `@`, as in `https://evil.example#@github.com/o/r`) is compared only as
   the identical string, and logs print it as `https://***`, so a log never names a host the url
   does not contact.)

5. **Do the owner-only checklist** (`-Phase Checklist` prints it). Then go through the cut-over phases.

## Owner-only checklist (never scripted)

1. `gh auth login` (GitHub.com, HTTPS, browser), then re-run `-Phase Clone` if the doctrine bus clone failed.
2. `gh ssh-key add "C:\Users\obabalola\.ssh\id_ed25519_github.pub" --title "mlvbox"`.
3. `claude auth login --claudeai` on the **same org** as Claude Desktop. Check with `claude auth status --json`:
   you need `loggedIn`, `firstParty`, `max`, and the Desktop `orgId`.
4. `codex login`, sign in to Codex desktop and ChatGPT, then run the lane pin probe:
   `pwsh -NoProfile -File "C:\!Layi Wkspc\MLV-App\tools\coordination\Promote-CodexPin.ps1"`.
5. Tailscale sign-in, then `tailscale ping bachelor.tail47abfa.ts.net`.
6. BACHELOR: append `C:\Users\obabalola\.ssh\id_ed25519_bachelor.pub` to user `layib`'s authorized keys
   there, then `ssh bachelor hostname`. If `\\bachelor\mlv-agent` asks for credentials, enter them
   yourself. Never list `processed\` or `outbox\` unbounded.
6b. SMB share credentials for BOTH venues (unconditional; added 2026-11-15 after PC-MINI3 ran a day with
   neither). Every venue job, every staged build and the Bachelor heartbeat read go through these shares;
   moving any build to the hub host does not remove the need. As the user the scheduled tasks run as,
   each command prompts for the password (type it there, never in chat):
   `cmdkey /add:Ultra-Magnus /user:MicrosoftAccount\<your Microsoft account> /pass`
   `cmdkey /add:bachelor /user:MicrosoftAccount\<your Microsoft account> /pass`
   (use `<machine>\<local user>` instead for a machine that signs in with a local account). Prove both,
   each must print True:
   `Test-Path "\\Ultra-Magnus\g\Temp\mlv-gpu-profile\agent\heartbeat.txt"; Test-Path "\\bachelor\mlv-agent\heartbeat.txt"`
   A missing credential shows as `System error 5` on `net view`, and as a misleading "No agent heartbeat"
   from the stage chain.
7. Paste the VIRTUAL-TEN key (step 3 above).
8. Windows: stay signed in (the tasks use Interactive logon), set Windows Update active hours, save
   the BitLocker recovery key, and reboot once after `-PagefileMaxGB`.

## One-host-active rule (the hub lease)

The lease is the branch `hub-lease/mlv-app` on the **fork**. It holds one file, `HUB-LEASE.json`,
with `holder` set to a computer name or empty. Writes use `git push --force-with-lease`, so two
hosts cannot both win. No workflow runs on a push to that branch (CI runs only on `master` pushes
and pull requests).

- **Lease-gated tasks** run only when `holder` equals this computer: `MLV-HubAutopilot`,
  `MLV-FleetHeartbeatPublish`, `MLV-QualityLift`, `Fleet-HealthDaily`. If the lease cannot be
  read, they skip the run, so a failure never leads to two hubs.
- **Host-local tasks** run on any host: `MLV-BoardStateHeartbeat`, `MLV-DiskAutoReclaim`,
  `MLV-ArchiveRetention`, `Claude-UsageProbe`.
- The lease only moves when someone moves it on purpose. A stale lease is never taken over
  automatically, because VIRTUAL-TEN does not renew it. Use `-Force -LeaseReason "<why>"` only when
  the holder is gone.
- Kill switch for every gated task on a host: create `C:\ProgramData\MlvWorkstation\PAUSED`.
- **Known gap:** VIRTUAL-TEN's own tasks were not changed by this work, so they do not check the
  lease. On that side the rule holds only by procedure: **disable before you release.** A follow-up
  could re-register them through `-Phase Gate`.

## Phased cut-over

**A - Build-out (no automation).** All tasks stay disabled. Prove the box: build in a scratch
worktree under `C:\mlvtmp` through `tools\coordination\build-guard.ps1`, run the fast tests, and run
one bounded `Invoke-Lane.ps1` lane on a docs-only card. Check that `-Phase Verify` reports the commit
ceiling you expect.

**B - Shared, VIRTUAL-TEN stays hub.** On VIRTUAL-TEN, run `-Phase LeaseAcquire` to record what is
already true. On the new box, enable only the host-local tasks:

```powershell
"MLV-BoardStateHeartbeat","MLV-DiskAutoReclaim","MLV-ArchiveRetention","Claude-UsageProbe" | ForEach-Object { Enable-ScheduledTask -TaskName $_ }
```

Move your interactive sessions and builds to the new box. That frees VIRTUAL-TEN's memory for the
lanes it runs. **Card-claim rule:** a card belongs to whichever host first pushed `product/<CARD>`
to the fork. Check `git ls-remote fork "refs/heads/product/<CARD>*"` before you start, and push the
branch straight away to claim it. The new box never runs `dispatch-lane.ps1` while it does not hold
the lease.

**C - Hub cut-over** (one quiet window):

1. On VIRTUAL-TEN, disable the board-driving tasks: `"MLV-HubAutopilot","MLV-FleetHeartbeatPublish","MLV-QualityLift","Fleet-HealthDaily" | ForEach-Object { Disable-ScheduledTask -TaskName $_ }`
2. Let in-flight lanes finish, or record them. Push every branch listed in `unpushed-worktree-branches.tsv`.
3. Run `-Phase Export` again (the hub state is now quiet), transfer it, then run `-Phase Import` on the new box.
4. On VIRTUAL-TEN, run `-Phase LeaseRelease`. On the new box, run `-Phase LeaseAcquire`.
5. On the new box, run `Enable-ScheduledTask` for the four lease-gated tasks. In a Claude session
   there, re-arm the 10-minute hub tick and the hourly archive-sweep crons
   (`.claude-state\project-memory\loop-resume-mlv-hub-20261011.md`).
6. Check: `gate.log` shows `running` for the autopilot, `heartbeat\inflight.md` keeps updating,
   and `-Phase LeaseShow` names the new box.

**D - After.** VIRTUAL-TEN keeps its other projects (Adobe, agent-bridge, TopBuilder), and its MLV
board tasks stay disabled. **Rollback** is the same steps in reverse: disable on the new box,
release, acquire on VIRTUAL-TEN, enable there.

## Multi-project and shared-machine readiness

- **Per-user:** `~\.claude` (instructions, hooks, skills, memory), `~\.codex`, gh/Claude/Codex
  sign-ins, npm globals, the scheduled tasks (Interactive logon).
  **Machine-wide:** sshd and the firewall, Defender, long paths, power, pagefile, `C:\Qt`, the Git
  system config, `C:\mlvtmp`, `C:\ProgramData\*`.
- **One OS user for MLV** (`obabalola`). The user hooks, the tracked project hook paths, the
  account-drift tooling and the Interactive tasks all assume a single user. Add a separate OS user,
  with its own Claude account, only for a different project family that needs its credentials kept
  apart. Do not split MLV lanes across users.
- **Disk:** VIRTUAL-TEN carries about 265 worktrees under `C:\mlvtmp`, so plan 300 GB or more free
  there. With a second NVMe, use `-MlvTmpTarget D:\mlvtmp`; a Windows 11 Dev Drive is a good target
  for it. Keep the repo on `C:`.
- **Memory gates are derived, not copied.** `host-commit-gate.ps1` uses ceiling = (RAM + pagefile
  MAX) - 12 GiB, needs at least 8 GiB of free physical memory, and trips when nonpaged pool exceeds
  8 GiB. VIRTUAL-TEN works out to 64 + 32 - 12 = 84 GiB. `-Phase Verify` prints the new box's figure,
  so set the pagefile on purpose. `build-guard.ps1` floors stay at 6 GiB physical and 8 GiB commit.
  The producer cap is in `.claude-state\hub-autopilot\producer-cap.json` (1 to 4, and the hub clamps
  it to 2 under pressure). Raise it only after measuring the new box.

## Easy to forget

- **Unpushed work:** about 860 local unmerged branches. Export lists the worktree branches that are
  missing from the fork or differ from it. Only pushed work moves.
- **Tasks not migrated** (XML copies are in `reference\task-xml`): `MLV-PwshAutoRepin` (SBP-specific);
  `CodexMemoryPressureGuard`, `CodexProcessHygiene`, `Codex-WeeklyHygiene` and
  `CodexHygieneAlertNotifier` (these need `C:\!Layi Wkspc\OrphanReaper`); `fleet-doctrine-sweep`; the
  Adobe, TopBuilder and DiskGuard tasks; and the retired `MLV-WorkstreamLoop` and
  `MLV-LaneIgnitionWatchdog`.
- **SBP launcher:** VIRTUAL-TEN runs its tasks through a hash-pinned `sbp-launcher.exe`. The new box
  runs them through `conhost --headless pwsh` instead.
- **Claude Desktop routines** (`~\.claude\scheduled-tasks`) and session transcripts do not move. The
  MLV project memory does move.
- **Hook dependency:** the user `SessionStart` hook calls
  `C:\ProgramData\FleetDefender\Show-DefenderRootsAlert.ps1`, which Import provides.
- **Qt:** only 6.10.2 is installed. VIRTUAL-TEN also has 5.15.2 and 6.8.3, both unused by the
  current build. OpenSSL (Chocolatey) is needed only for release packaging.
- **GPU:** VIRTUAL-TEN has no CUDA, and the CUDA legs run on BACHELOR. The script sets up no CUDA
  toolkit.
- **Tailscale ACLs** must let the new box reach BACHELOR.
