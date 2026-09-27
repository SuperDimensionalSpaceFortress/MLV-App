# playback-attr-3-cuda-dll-job.ps1 -- GENERATOR (runs locally / in a lane; nothing here runs
# on Ultra-Magnus). Emits a self-contained <jobId>.job.ps1 plus a <jobId>-source.zip for the
# Ultra-Magnus file-drop agent (tools/profiling/ultra-magnus-agent.ps1 protocol: both files land
# in <agent root>\inbox; the agent runs the job; the job publishes into
# <agent root>\outbox\<jobId>.artifacts). The hub submits -- with tools/profiling/um-run.ps1 for
# the job script, which renames it to its own job id, which is why this job never reads its own
# file name: its jobId and therefore its source-zip name are BAKED IN.
#
# WHY THIS EXISTS (swarm ruling 2026-09-16,
# .claude-state/fleet-runs/swarm-attr3-buildhost-20260916T2150Z/SYNTHESIS.md). Bachelor has
# Visual Studio without the VC tools component (no cl.exe) and no CUDA toolkit at all; the
# compile-on-Bachelor job exited 5 at msvcDiscovery, as did the Aug 9 precedent. Ultra-Magnus
# has VC tools, CUDA 12.6 nvcc and an RTX 4090. So the CUDA DLL PAIR is built here and the
# Qt/MinGW exe is built on the board host
# (tools/profiling/bachelor/playback-attr-3-cuda-assemble.ps1), which consumes this job's
# published artifacts directory as its -DllPairDir.
#
# TARGET sm_86, NOT sm_89. The existing Ultra-Magnus compile jobs target sm_89 (Ada, the 4090's
# own architecture). The measurement host is Bachelor, whose GPU cannot run sm_89 cubins at all.
# -CudaArchitectures must therefore contain sm_86; the job re-proves it on the host with
# cuobjdump --list-elf against the built bytes, because a requested flag is not an emitted cubin.
#
# BOTH DLLs. The playback path under measurement loads igpu_recon_cuda.dll AND
# igpu_amaze_debayer_cuda.dll (MLVAPP_EXPERIMENTAL_GPU_AMAZE_DEBAYER=1 in the attribution job);
# shipping only the recon DLL produced a package that silently fell back.
#
# The tracked backend scripts are reused verbatim -- tools/gpu/backend/build-backend-dll.ps1 and
# tools/gpu/backend/amaze-debayer-dll.ps1, from the EXTRACTED source archive, so the build
# recipe is the one committed at -SourceCommit and not a copy that can drift.
#
# SOURCE ARCHIVE BINDING (sol, PR #133 r2, BLOCKER). Everything above is a claim about the
# EXTRACTED source, and the archive travels to Ultra-Magnus as an ordinary inbox side-file. Its
# lowercase sha256 is therefore computed here and BAKED IN, and the job verifies the arriving
# bytes against it -- and the commit id git stamps into the zip comment against -SourceCommit --
# BEFORE Expand-Archive runs, failing closed at exit 8. Without that, altered or stale source
# would produce DLLs whose manifest still claims sourceCommit. The verified sha is recorded as
# dll-pair-manifest.json's sourceArchiveSha256 so the claim is auditable downstream.
# The verification functions are embedded VERBATIM from
# tools/profiling/bachelor/AttrCudaArtifacts.psm1 (a job on a host with no checkout cannot
# Import-Module), which is how the test suite exercises the same code the job runs.
# `<jobId>.job.ps1 -VerifyOnly` runs exactly that prefix and exits, writing nothing.
#
# NO FOOTAGE. This job compiles source and inspects binaries. It never opens, names, globs or
# resolves a media file, and nothing it emits does either.
#
# Usage:
#   pwsh -NoProfile -File tools\profiling\ultramagnus\playback-attr-3-cuda-dll-job.ps1 `
#       -SourceCommit <40-hex> -OutDir <dir> [-CudaArchitectures sm_86]

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$SourceCommit,

    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    # Passed straight through to build-backend-dll.ps1 -CudaArchitectures, and joined with
    # commas for amaze-debayer-dll.ps1 -Arch (that script has no -CudaArchitectures parameter;
    # its -Arch accepts the same comma-separated token list).
    [string[]]$CudaArchitectures = @('sm_86'),

    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path,

    # Host-side path of the Ultra-Magnus agent root. tools/profiling/um-run.ps1 submits over the
    # share \\Ultra-Magnus\g\Temp\mlv-gpu-profile\agent, which is this directory on the host.
    # `~` is admitted because Windows temp roots carry 8.3 short names (RUNNER~1, OBABAL~1) and
    # the behavioural tests point -AgentRoot at one; it is inert everywhere this value is used
    # (single-quoted PowerShell literals and Join-Path), unlike the quote/`$ characters the
    # allowlist exists to exclude.
    [ValidatePattern('^[A-Za-z]:\\[A-Za-z0-9 _.~\\-]+$')]
    [string]$AgentRoot = 'G:\Temp\mlv-gpu-profile\agent',

    # Optional oracle vector directory for build-backend-dll.ps1's LoadLibrary parity harness
    # (dll_test.exe). Left empty the job does not pass -Vectors and that script keeps its own
    # default. See -RequireParityHarness for what a failing harness means.
    [string]$ParityVectors = '',

    # The recon backend script ends by RUNNING dll_test.exe against oracle vectors and exits
    # with the harness's code, so a host without those vectors fails the script even when the
    # DLL built cleanly. The product of this job is the DLL pair, which is gated independently
    # and far more tightly (artifact present, cuobjdump shows the required architecture, exports
    # present, sha256 bound in the manifest). So by default a nonzero harness code is RECORDED
    # in the manifest (reconBackendScript.exitCode / parityHarnessPassed) and not fatal; pass
    # -RequireParityHarness to make it fatal (exit 13).
    [switch]$RequireParityHarness
)

$ErrorActionPreference = 'Stop'
# The shared module lives beside the Bachelor scripts; this generator reaches across for the ONE
# definition of the verification functions rather than restating them.
Import-Module (Join-Path $PSScriptRoot '..\bachelor\AttrCudaArtifacts.psm1') -Force

$RequiredArchitecture = 'sm_86'

# --- validate the request locally, BEFORE anything is emitted ---------------------
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
& git -C $RepoRoot cat-file -e "$SourceCommit^{commit}" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "SourceCommit is not a commit known to the local repo at $RepoRoot : $SourceCommit"
}

$architectures = @($CudaArchitectures | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
if ($architectures.Count -eq 0) { throw 'No CUDA architectures requested.' }
foreach ($architecture in $architectures) {
    if ($architecture -notmatch '^(sm|compute)_[0-9]{2,3}$') {
        throw "Unsupported CUDA architecture token '$architecture'. Use sm_86 / compute_86 form."
    }
}
if ($architectures -notcontains $RequiredArchitecture) {
    throw "-CudaArchitectures must contain ${RequiredArchitecture}: the measurement host's GPU cannot run cubins built only for a newer architecture. Got: $($architectures -join ',')"
}

$shortSha = $SourceCommit.Substring(0, 12)
$JobId = "playback-attr-3-cuda-dllpair-$shortSha"

if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
$archivePath = Join-Path $OutDir "$JobId-source.zip"
$jobPath = Join-Path $OutDir "$JobId.job.ps1"

if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath -Force }
& git -C $RepoRoot archive --format=zip -o $archivePath $SourceCommit
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $archivePath)) {
    throw "git archive failed for $SourceCommit"
}
# Bind the emitted job to THESE bytes. Verified here as well, with the same function the job
# runs, so a git that did not stamp the commit into the zip comment is caught on this machine
# instead of costing an agent round trip.
$sourceArchiveSha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
[void](Assert-AttrCudaSourceArchive -ArchivePath $archivePath -ExpectedSha256 $sourceArchiveSha256 -ExpectedCommit $SourceCommit)

$embeddedFunctions = Get-AttrCudaEmbeddedFunctionSource -Name @(
    'Get-AttrCudaZipArchiveComment',
    'Assert-AttrCudaSourceArchive',
    'Assert-AttrCudaWritableFileSlot',
    'Publish-AttrCudaText',
    'Publish-AttrCudaFileCopy',
    'Publish-AttrCudaFileMove',
    'New-AttrCudaDirectory',
    'Remove-AttrCudaPartialFile',
    'Assert-AttrCudaNoLinkBelowRoot',
    'Remove-AttrCudaTree'
)

# --- job body template (placeholders are substituted below; the body itself never touches
#     this generator's variables directly, so there is no accidental capture of this machine's
#     environment into the emitted script) ----------------------------------------------------
$template = @'
# -VerifyOnly runs the input-verification prefix (agent-root containment, source-archive
# presence, sha256 and commit binding) and exits WITHOUT creating, expanding or publishing
# anything. The agent never passes it; the behavioural tests do.
param([switch]$VerifyOnly)

$ErrorActionPreference = 'Stop'
$SourceCommit = '__SOURCE_COMMIT__'
$SourceArchiveSha256 = '__SOURCE_ARCHIVE_SHA256__'
$CudaArchitectures = @(__ARCH_LIST__)
$RequiredArchitecture = '__REQUIRED_ARCH__'
$ParityVectors = '__PARITY_VECTORS__'
$RequireParityHarness = [bool]__REQUIRE_PARITY__
$AgentRoot = '__AGENT_ROOT__'
$JobId = '__JOB_ID__'
$ManifestName = 'dll-pair-manifest.json'
$CudartName = 'cudart64_12.dll'
$Archive = Join-Path $AgentRoot "inbox\$JobId-source.zip"
$Work = Join-Path $AgentRoot "work\$JobId"
$Pub = Join-Path $AgentRoot "outbox\$JobId.artifacts"
$ReconDllName = 'igpu_recon_cuda.dll'
$AmazeDllName = 'igpu_amaze_debayer_cuda.dll'

function Say([string]$Message) { Write-Output "[$JobId] $Message" }
function Get-ShaLower([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

# --- verifiers, embedded VERBATIM from tools/profiling/bachelor/AttrCudaArtifacts.psm1 --------
# This host has no checkout, so Import-Module is not available; the generator splices the module
# text in. tools/repo_hygiene/test_playback_attr_3_cuda_split_route.py executes the module copy
# directly, so what is tested and what runs here are the same characters.
__EMBEDDED_FUNCTIONS__
# --- end embedded verifiers -------------------------------------------------------------------

$StepLog = [System.Collections.Specialized.OrderedDictionary]::new()
$Evidence = [System.Collections.Specialized.OrderedDictionary]::new()
# Set only once this run owns a freshly created outbox. Until then a failure must not write into
# a previous run's artifacts directory -- and -VerifyOnly must not write at all.
$PubReady = $false

# Machine-safety boundary: every path this job owns lives under the agent root, so a
# substituted value that escaped it would fail here rather than write somewhere unexpected.
function Assert-UnderAgentRoot([string]$Path, [string]$Label) {
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetFullPath($AgentRoot)
    if ($full -ne $root -and -not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "job-owned path '$Label' resolves outside the agent root: $full"
    }
}
foreach ($check in @(
    @{ path = $Archive; label = 'Archive' },
    @{ path = $Work; label = 'Work' },
    @{ path = $Pub; label = 'Pub' }
)) { Assert-UnderAgentRoot $check.path $check.label }

function Complete-Failed([int]$Code, [string]$Step, [string]$Message) {
    $StepLog[$Step] = $Code
    Say "FAIL step=$Step exit=$Code $Message"
    if ($PubReady -and (Test-Path -LiteralPath $Pub)) {
        $partial = [ordered]@{
            schema = 'mlvapp.playback-attr-3-cuda-dll-pair.v1'
            sourceCommit = $SourceCommit
            jobId = $JobId
            cudaArch = @($CudaArchitectures)
            exitCode = $Code
            failedStep = $Step
            message = $Message
            builtOnHost = $env:COMPUTERNAME
            evidence = $Evidence
            steps = $StepLog
        }
        # Still a publish write: slot-checked, and a refusal never masks the original exit code.
        try {
            [void](Publish-AttrCudaText -Path (Join-Path $Pub 'result.json') -Value ($partial | ConvertTo-Json -Depth 12))
        } catch {
            Say "RESULT_JSON_NOT_WRITTEN $($_.Exception.Message)"
        }
    }
    Write-Output "RESULT=DLL_PAIR_FAILED STEP=$Step EXIT=$Code"
    exit $Code
}

Say "START source=$SourceCommit arch=$($CudaArchitectures -join ',')"
if (-not (Test-Path -LiteralPath $Archive)) { Complete-Failed 3 'sourceArchive' "missing: $Archive" }
$StepLog['sourceArchive'] = 0

# EVERYTHING below this point is a claim about the extracted source, so the archive is bound to
# the generator's bytes AND to $SourceCommit before a single file is written. Exit 8 is reserved
# for this and nothing else.
try {
    $verifiedArchiveSha = Assert-AttrCudaSourceArchive -ArchivePath $Archive -ExpectedSha256 $SourceArchiveSha256 -ExpectedCommit $SourceCommit
} catch {
    Complete-Failed 8 'sourceArchiveBinding' $_.Exception.Message
}
$Evidence['sourceArchiveSha256'] = $verifiedArchiveSha
$StepLog['sourceArchiveBinding'] = 0
Say "SOURCE ARCHIVE bound sha256=$verifiedArchiveSha commit=$SourceCommit"

if ($VerifyOnly) {
    Write-Output "RESULT=VERIFY_ONLY_OK SOURCE=$SourceCommit ARCHIVE_SHA256=$verifiedArchiveSha"
    exit 0
}

Remove-AttrCudaTree -TrustedRoot $AgentRoot -Path $Work
Remove-AttrCudaTree -TrustedRoot $AgentRoot -Path $Pub
New-Item -ItemType Directory -Path $Work -Force | Out-Null
# The outbox is created explicitly (never as a side effect of -Force on a deeper path), so its
# parent is checked for links like every other directory this job creates.
[void](New-AttrCudaDirectory -Path (Join-Path $AgentRoot 'outbox'))
[void](New-AttrCudaDirectory -Path $Pub)
$PubReady = $true
Expand-Archive -LiteralPath $Archive -DestinationPath $Work -Force
$StepLog['sourceExpand'] = 0

# Job-owned TEMP: a scratch dir under $Work becomes TEMP/TMP for the rest of this process, so
# nvcc, cl, the backend scripts' own .cmd files and every other child inherit it instead of the
# ambient machine TEMP. Set before any child process runs.
$Scratch = Join-Path $Work '.job-tmp'
New-Item -ItemType Directory -Path $Scratch -Force | Out-Null
$env:TEMP = $Scratch
$env:TMP = $Scratch

$Backend = Join-Path $Work 'tools\gpu\backend'
$ReconScript = Join-Path $Backend 'build-backend-dll.ps1'
$AmazeScript = Join-Path $Backend 'amaze-debayer-dll.ps1'
$required = @(
    $ReconScript,
    $AmazeScript,
    (Join-Path $Backend 'igpu_recon_cuda.cu'),
    (Join-Path $Backend 'igpu_recon_cuda.def'),
    (Join-Path $Backend 'igpu_amaze_debayer_cuda.cu'),
    (Join-Path $Backend 'igpu_amaze_debayer_cuda.def')
)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path)) { Complete-Failed 4 'backendSourcesPresent' "extracted backend input missing: $path" }
}
$StepLog['backendSourcesPresent'] = 0

$pf86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
$vsLocator = Join-Path $pf86 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $vsLocator)) { Complete-Failed 5 'msvcDiscovery' "VS locator not found: $vsLocator" }
$vsRoot = & $vsLocator -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null | Select-Object -First 1
if (-not $vsRoot) { Complete-Failed 5 'msvcDiscovery' 'MSVC (VC tools) not found on this host' }
$StepLog['msvcDiscovery'] = 0

$cudaBase = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA'
$cudaRoot = Get-ChildItem -LiteralPath $cudaBase -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 1
if (-not $cudaRoot) { Complete-Failed 6 'cudaDiscovery' "CUDA toolkit root not found under $cudaBase" }
$cudaBin = Join-Path $cudaRoot.FullName 'bin'
$nvcc = Join-Path $cudaBin 'nvcc.exe'
$cuobjdump = Join-Path $cudaBin 'cuobjdump.exe'
foreach ($tool in @($nvcc, $cuobjdump)) {
    if (-not (Test-Path -LiteralPath $tool)) { Complete-Failed 6 'cudaDiscovery' "required CUDA tool not found: $tool" }
}
$StepLog['cudaDiscovery'] = 0

# The CUDA runtime shipped with the package must come from the SAME toolkit that compiled the
# DLLs -- an older cudart beside a newer cubin is a load-time failure on the measurement host.
$cudartSource = Join-Path $cudaBin $CudartName
if (-not (Test-Path -LiteralPath $cudartSource)) {
    Complete-Failed 7 'cudaRuntimePresent' "$CudartName not found in $cudaBin (the package requires the CUDA runtime from the compiling toolkit)"
}
$StepLog['cudaRuntimePresent'] = 0

# --- build both DLLs with the TRACKED backend scripts, from the extracted source -------------
# Each backend script ends in `exit <code>`, so it runs in a CHILD shell: invoking it in-process
# with & would terminate this job at that exit. -Command (not -File) because -File passes every
# argument as one literal string, which cannot express the [string[]] -CudaArchitectures list.
$archLabel = ($CudaArchitectures -join ',')
$reconLog = Join-Path $Pub 'recon-backend-build.log'
$amazeLog = Join-Path $Pub 'amaze-backend-build.log'
$psExe = (Get-Process -Id $PID).Path
if ([string]::IsNullOrWhiteSpace($psExe)) { $psExe = 'powershell.exe' }
function ConvertTo-PsSingleQuoted([string]$Value) { "'" + $Value.Replace("'", "''") + "'" }
$archArrayLiteral = '@(' + ((@($CudaArchitectures) | ForEach-Object { ConvertTo-PsSingleQuoted ([string]$_) }) -join ',') + ')'

$reconCommand = "& $(ConvertTo-PsSingleQuoted $ReconScript) -Dir $(ConvertTo-PsSingleQuoted $Backend) -CudaArchitectures $archArrayLiteral"
if (-not [string]::IsNullOrWhiteSpace($ParityVectors)) {
    $reconCommand += " -Vectors $(ConvertTo-PsSingleQuoted $ParityVectors)"
}
Say "BUILD recon backend arch=$archLabel"
$reconOutput = @(& $psExe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $reconCommand 2>&1 | ForEach-Object { [string]$_ })
$reconRc = $LASTEXITCODE
[void](Publish-AttrCudaText -Path $reconLog -Value $reconOutput)
$reconDll = Join-Path $Backend $ReconDllName
if (-not (Test-Path -LiteralPath $reconDll)) { Complete-Failed 11 'reconBackendBuild' "$ReconDllName not produced (script exit=$reconRc)" }
# The script's tail runs the LoadLibrary parity harness and exits with ITS code; see the
# -RequireParityHarness note in the generator header.
$parityHarnessPassed = ($reconRc -eq 0)
if ($RequireParityHarness -and -not $parityHarnessPassed) {
    Complete-Failed 13 'reconParityHarness' "build-backend-dll.ps1 exit=$reconRc with -RequireParityHarness set"
}
$StepLog['reconBackendBuild'] = 0

Say "BUILD amaze backend arch=$archLabel"
# amaze-debayer-dll.ps1 has no -CudaArchitectures parameter; its -Arch takes the same tokens as
# a comma-separated list, which is how the requested set reaches it unchanged.
$amazeCommand = "& $(ConvertTo-PsSingleQuoted $AmazeScript) -Dir $(ConvertTo-PsSingleQuoted $Backend) -Arch $(ConvertTo-PsSingleQuoted $archLabel)"
$amazeOutput = @(& $psExe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $amazeCommand 2>&1 | ForEach-Object { [string]$_ })
$amazeRc = $LASTEXITCODE
[void](Publish-AttrCudaText -Path $amazeLog -Value $amazeOutput)
$amazeDll = Join-Path $Backend $AmazeDllName
# amaze-debayer-dll.ps1 exits 0 only after its own export verification passes, so a nonzero
# code here is a real failure, never a harness artefact.
if ($amazeRc -ne 0 -or -not (Test-Path -LiteralPath $amazeDll)) {
    Complete-Failed 12 'amazeBackendBuild' "amaze-debayer-dll.ps1 exit=$amazeRc"
}
$StepLog['amazeBackendBuild'] = 0

# --- prove the emitted cubins, not the requested flags ---------------------------------------
$listElf = [System.Collections.Specialized.OrderedDictionary]::new()
foreach ($target in @(
    @{ name = $ReconDllName; path = $reconDll },
    @{ name = $AmazeDllName; path = $amazeDll }
)) {
    $output = @(& $cuobjdump --list-elf ([string]$target.path) 2>&1 | ForEach-Object { [string]$_ })
    $rc = $LASTEXITCODE
    $tokens = @($output | ForEach-Object { [regex]::Matches($_, '(?:sm|compute)_[0-9]{2,3}') } |
        ForEach-Object { $_.Value } | Sort-Object -Unique)
    $listElf[[string]$target.name] = [ordered]@{ exitCode = $rc; tokens = @($tokens); lines = @($output) }
    if ($rc -ne 0) {
        $Evidence['listElf'] = $listElf
        Complete-Failed 14 'architectureProof' "cuobjdump --list-elf exit=$rc for $($target.name)"
    }
    if ($tokens -notcontains $RequiredArchitecture) {
        $Evidence['listElf'] = $listElf
        Complete-Failed 14 'architectureProof' "$($target.name) cubins do not include $RequiredArchitecture (found: $($tokens -join ','))"
    }
}
$Evidence['listElf'] = $listElf
$StepLog['architectureProof'] = 0

# --- export inspection, and the pendingSymbolPresence the measurement host cannot derive ------
# This is the symbol test that used to live (and could only ever throw) in the attribution job
# on Bachelor: tools/profiling/bachelor/playback-attr-3-cuda-job.ps1 now READS the boolean this
# block writes into the manifest.
$exportTool = Get-ChildItem -LiteralPath (Join-Path $vsRoot 'VC\Tools\MSVC') -Recurse -Filter 'dumpbin.exe' -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\x64\\' } | Select-Object -First 1
if (-not $exportTool) { Complete-Failed 15 'exportInspection' "dumpbin.exe not found under $vsRoot (cannot prove pendingSymbolPresence)" }
$exports = [System.Collections.Specialized.OrderedDictionary]::new()
foreach ($target in @(
    @{ name = $ReconDllName; path = $reconDll },
    @{ name = $AmazeDllName; path = $amazeDll }
)) {
    $output = @(& $exportTool.FullName /EXPORTS ([string]$target.path) 2>&1 | ForEach-Object { [string]$_ })
    $rc = $LASTEXITCODE
    if ($rc -ne 0) { Complete-Failed 15 'exportInspection' "dumpbin /EXPORTS exit=$rc for $($target.name)" }
    $exports[[string]$target.name] = @($output)
}
$Evidence['exports'] = $exports
# A real boolean, always: gpu_job_result_provenance.py rejects null/omitted, and the
# attribution job refuses a manifest whose field is not a boolean.
$pendingSymbolPresence = [bool](@($exports[$ReconDllName] | Select-String 'igpu_recon_').Count -gt 0)
if (-not $pendingSymbolPresence) {
    Complete-Failed 16 'reconExportsPresent' "$ReconDllName exports no igpu_recon_ symbol; the package would load nothing"
}
$StepLog['exportInspection'] = 0

# --- transactional publish: .partial, verify, rename, manifest LAST --------------------------
# The board-host assembler reads this directory. Publishing the manifest last means its mere
# presence proves every file beside it is complete and hash-verified.
$publishSet = @(
    [ordered]@{ name = $ReconDllName; source = $reconDll },
    [ordered]@{ name = $AmazeDllName; source = $amazeDll },
    [ordered]@{ name = $CudartName; source = $cudartSource }
)
$archSidecar = Join-Path $Backend 'igpu_recon_cuda.arch.json'
if (Test-Path -LiteralPath $archSidecar) {
    $publishSet += [ordered]@{ name = 'igpu_recon_cuda.arch.json'; source = $archSidecar }
}

$partials = @()
function Remove-JobPartials {
    # Files only: never -Recurse, never through a link (sol PR #133 r3; see Remove-AttrCudaPartialFile).
    foreach ($p in $script:partials) { [void](Remove-AttrCudaPartialFile -TrustedRoot $AgentRoot -Path $p) }
    [void](Remove-AttrCudaPartialFile -TrustedRoot $AgentRoot -Path (Join-Path $Pub "$ManifestName.partial"))
}

$files = [System.Collections.Specialized.OrderedDictionary]::new()
try {
    foreach ($item in $publishSet) {
        $name = [string]$item.name
        $sourcePath = [string]$item.source
        $partial = Join-Path $Pub "$name.partial"
        $script:partials += $partial
        [void](Publish-AttrCudaFileCopy -Source $sourcePath -Destination $partial)
        $expected = Get-ShaLower $sourcePath
        if ((Get-ShaLower $partial) -ne $expected) { throw "sha256 did not round-trip for $name" }
        $files[$name] = $expected
    }
} catch {
    Remove-JobPartials
    Complete-Failed 20 'publishPartials' $_.Exception.Message
}
$StepLog['publishPartials'] = 0

try {
    foreach ($item in $publishSet) {
        $name = [string]$item.name
        [void](Publish-AttrCudaFileMove -Source (Join-Path $Pub "$name.partial") -Destination (Join-Path $Pub $name))
    }
} catch {
    Remove-JobPartials
    Complete-Failed 21 'publishRename' $_.Exception.Message
}
$StepLog['publishRename'] = 0

$nvccVersion = ((& $nvcc --version 2>&1) -join "`n").Trim()
$manifest = [ordered]@{
    schema = 'mlvapp.playback-attr-3-cuda-dll-pair-manifest.v1'
    sourceCommit = $SourceCommit
    # The archive these DLLs were compiled from, verified on this host before it was expanded.
    # sourceCommit above is a CLAIM; this is what makes it checkable after the fact.
    sourceArchiveSha256 = $verifiedArchiveSha
    cudaArch = @($CudaArchitectures)
    nvccVersion = $nvccVersion
    files = $files
    pendingSymbolPresence = $pendingSymbolPresence
    builtOnHost = $env:COMPUTERNAME
    requiredArchitecture = $RequiredArchitecture
    cuobjdumpListElf = $listElf
    reconBackendScript = [ordered]@{ exitCode = $reconRc; parityHarnessPassed = $parityHarnessPassed; parityHarnessRequired = $RequireParityHarness }
    amazeBackendScript = [ordered]@{ exitCode = $amazeRc }
    cudaToolkitRoot = $cudaRoot.FullName
    jobId = $JobId
    builtAtUtc = (Get-Date).ToUniversalTime().ToString('o')
}
try {
    [void](Assert-AttrCudaWritableFileSlot -Path (Join-Path $Pub $ManifestName))
    [void](Publish-AttrCudaText -Path (Join-Path $Pub "$ManifestName.partial") -Value ($manifest | ConvertTo-Json -Depth 12))
    [void](Publish-AttrCudaFileMove -Source (Join-Path $Pub "$ManifestName.partial") -Destination (Join-Path $Pub $ManifestName))
} catch {
    Remove-JobPartials
    Complete-Failed 22 'publishManifest' $_.Exception.Message
}
$StepLog['publishManifest'] = 0

$result = [ordered]@{
    schema = 'mlvapp.playback-attr-3-cuda-dll-pair.v1'
    sourceCommit = $SourceCommit
    sourceArchiveSha256 = $verifiedArchiveSha
    jobId = $JobId
    cudaArch = @($CudaArchitectures)
    exitCode = 0
    files = $files
    pendingSymbolPresence = $pendingSymbolPresence
    builtOnHost = $env:COMPUTERNAME
    manifest = [ordered]@{ name = $ManifestName; sha256 = (Get-ShaLower (Join-Path $Pub $ManifestName)) }
    nvccVersion = $nvccVersion
    evidence = $Evidence
    steps = $StepLog
}
try { [void](Publish-AttrCudaText -Path (Join-Path $Pub 'result.json') -Value ($result | ConvertTo-Json -Depth 12)) } catch { Complete-Failed 23 'publishResult' $_.Exception.Message }
Write-Output "RESULT=DLL_PAIR_OK SOURCE=$SourceCommit ARCH=$archLabel RECON=$($files[$ReconDllName]) AMAZE=$($files[$AmazeDllName]) ARTIFACTS=$Pub"
exit 0
'@

$archLiteral = ($architectures | ForEach-Object { "'" + $_.Replace("'", "''") + "'" }) -join ','
$text = $template.
    Replace('__SOURCE_COMMIT__', $SourceCommit).
    Replace('__SOURCE_ARCHIVE_SHA256__', $sourceArchiveSha256).
    Replace('__ARCH_LIST__', $archLiteral).
    Replace('__REQUIRED_ARCH__', $RequiredArchitecture).
    Replace('__PARITY_VECTORS__', $ParityVectors.Replace("'", "''")).
    Replace('__REQUIRE_PARITY__', $(if ($RequireParityHarness) { '$true' } else { '$false' })).
    Replace('__AGENT_ROOT__', $AgentRoot.Replace("'", "''")).
    Replace('__JOB_ID__', $JobId)
# LAST, and deliberately so: the module text is spliced in after every other substitution, so no
# placeholder rule can rewrite a character inside the verbatim verifier source.
$text = $text.Replace('__EMBEDDED_FUNCTIONS__', $embeddedFunctions)

[IO.File]::WriteAllText($jobPath, $text, [Text.UTF8Encoding]::new($false))

[pscustomobject]@{
    jobFile = $jobPath
    sourceArchive = $archivePath
    # The hub drops the archive into the inbox under EXACTLY this name; the job refuses any
    # other bytes at exit 8.
    sourceArchiveSha256 = $sourceArchiveSha256
    jobId = $JobId
    sourceCommit = $SourceCommit
    cudaArchitectures = @($architectures)
    agentRoot = $AgentRoot
    # String join, not Join-Path: this describes a path on the REMOTE host, whose drive need
    # not exist on the machine running the generator (Join-Path resolves the drive qualifier).
    artifactsDir = ($AgentRoot.TrimEnd('\') + "\outbox\$JobId.artifacts")
    manifestName = 'dll-pair-manifest.json'
}
