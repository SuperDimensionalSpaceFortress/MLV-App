# PLAYBACK-ATTR-3-CUDA owner-input legs: pre-launch trace, one read, derived timeouts

Card BACHELOR-OWNER-CLIP-STAGE-STALL-1. Runbook for the three things an owner-input leg now does
that it did not before. The route itself is in [playback-attr-3-cuda.md](playback-attr-3-cuda.md).

## What was wrong

On Bachelor an owner input is read at ~2 MB/s cold (real-time scanner on; the first part alone is
2.2 GB). The leg job read it three to four times before playback (two hashes, then the smoke
runner, then the app), printed nothing, and the agent's cap killed it with an empty stdout.
Measured on the host: 4 KiB reads (what `Get-FileHash` issues) run at 0.55 MB/s even warm; 4 MiB
reads with an incremental SHA-256 ran 2.1-2.4 MB/s cold in steady state and 13 MB/s warm.

## 1. Trace: a killed job still tells you where it was

Every pre-launch step appends one timestamped, path-free line to
`<AgentRoot>\logs\<JobId>.trace.txt` as it starts and as it ends (flushed per line). Fetch it
through the queue:

```
pwsh -NoProfile -File tools\profiling\bachelor\attr3-trace-fetch-job.ps1 -OutDir <dir> [-TraceJobId <id>] [-Tail 200]
pwsh -NoProfile -File tools\profiling\um-run.ps1 -AgentShare '\\bachelor\mlv-agent' -ScriptPath <dir>\attr3-trace-fetch-<n>.job.ps1
```

With no `-TraceJobId` it lists the ten newest traces and prints the tail of the newest.

## 2. One full read of the input per job

The owner arm screens each part cheaply first (`Test-AttrCudaFootagePart -LengthOnly`: exists,
readable, length; no content read), links and holds the part, then reads it in full exactly once
through the held link: 4 MiB blocks, incremental SHA-256, traced with size and rate
(`Get-AttrCudaFileSha256Blocks`). The identity check is never skipped, and the same pass warms the
OS file cache for the smoke runner and the app. Each cached build artifact is hashed once too and
its digest reused for the deployed copies.

## 3. Timeouts from a measurement

`Get-AttrCudaLegTimeBudget` (AttrCudaArtifacts.psm1) derives, from the input's bytes and the
measured cold rate `$script:AttrCudaMeasuredColdReadMBps`:
`identityReadSec = ceil(MB / rate * 1.5)`, the smoke runner's `-ProcessTimeoutMs`
(identity read + launch 60 + play 40 + settle 3 + slack 30 s; the runner's own default of ~75 s has
no allowance for loading the input), and the `um-run -TimeoutSec` to submit with
(`recommendedJobTimeoutSec` in the generator's output). Re-measure with
`attr3-footage-read-rate-job.ps1` (bounded 128 MiB regions, never whole-file) and pass
`-ColdReadMBps` to the generator to override.

## Known limits

- The quiescence gate (mean CPU <= 20 %) is unchanged; a host with pegged cores exits 12 after the
  identity read. Check per-core load first (`hfr-settings-record` prints PERCORE).
- Get-CimInstance / Get-Counter can take minutes on a loaded Bachelor; the pre-launch steps avoid
  CIM, the quiescence gate still samples it.
- A Defender exclusion is a security setting and is not attempted; the cost is measured, not removed.
