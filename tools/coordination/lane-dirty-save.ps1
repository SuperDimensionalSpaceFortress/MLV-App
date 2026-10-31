# lane-dirty-save.ps1 - LANE-DIRTY-SAVE-CORE-1 (core split out of parked PR #698).
# A dead lane's uncommitted work is the one thing a refusal or a clean-up destroys. When the lane process is dead, this writes into -RunDir:
#   dirty-save-<head8>.patch   git diff HEAD --binary, byte for byte; .staged.patch = git diff --cached --binary (staged work the tree no longer has)
#   dirty-save-<head8>.untracked.txt = git ls-files -z --others --exclude-standard: untracked file NAMES only, NUL-separated and never C-quoted (git quotes " \ TAB LF even with core.quotepath=false)
#   .receipt.json = one JSON line naming the files of THIS save, also printed; it says untrackedFormat=nul-separated and untrackedContents=not-saved
# Untracked file CONTENTS are NOT preserved by this saver (names only); copying them is LANE-DIRTY-SAVE-INVENTORY-ROWS-1 / LANE-DIRTY-SAVE-WORKSTREAM-WIRE-1 scope.
# The lane is named by -LanePid or by -ChildJson ({pid, createdUtc}; a process that started after createdUtc is a reused pid, so the lane is dead), never both.
# Exit: 0 saved (or already saved with these exact bytes), 10 nothing to save, 20 lane alive (refused, nothing written), 30 bad arguments, 40 git failed.
# Never stashes, resets, checks out, cleans or deletes. Each file is created CREATE_NEW; a name taken by different bytes is skipped (.2, .3 ... suffix), by the same bytes reused.
# Every git call gets GIT_TRACE*=0 in its OWN environment (never the caller's): a trace variable or Trace2 config naming a tracked file would make git
# append to it (sol r20 B1 on #698), and removing a caller's disable is what exposed it. GIT_OPTIONAL_LOCKS=0 keeps git from refreshing the index.
param(
    [Parameter(Mandatory)][string]$WorkDir,
    [Parameter(Mandatory)][string]$RunDir,
    [int]$LanePid = 0,
    [string]$ChildJson = ''
)
$ErrorActionPreference = 'Stop'
function Invoke-GitBytes([string[]]$GitArgs) {
    try {
        $psi = [System.Diagnostics.ProcessStartInfo]::new('git')
        foreach ($a in (@('-C', $WorkDir) + $GitArgs)) { $psi.ArgumentList.Add($a) }
        $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.UseShellExecute = $false
        foreach ($k in 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE') { [void]$psi.Environment.Remove($k) }
        foreach ($k in (@('GIT_TRACE', 'GIT_TRACE2', 'GIT_TRACE2_EVENT', 'GIT_TRACE2_PERF') + @($psi.Environment.Keys | Where-Object { $_ -like 'GIT_TRACE*' }))) { $psi.Environment[$k] = '0' }
        $psi.Environment['GIT_OPTIONAL_LOCKS'] = '0'
        $p = [System.Diagnostics.Process]::Start($psi)
        $err = $p.StandardError.ReadToEndAsync()
        $ms = [System.IO.MemoryStream]::new()
        $p.StandardOutput.BaseStream.CopyTo($ms)
        $p.WaitForExit()
        return @{ Code = $p.ExitCode; Bytes = $ms.ToArray(); Err = $err.Result }
    } catch { return @{ Code = -1; Bytes = [byte[]]@(); Err = $_.Exception.Message } }
}
function Test-LaneAlive {
    if ($LanePid -and $ChildJson) { throw 'mixed-selectors' }  # one pid with another lane's createdUtc would judge a live lane reused (dot B1 on #722)
    $procId = $LanePid; $born = $null
    if ($ChildJson) {
        $j = Get-Content -LiteralPath $ChildJson -Raw | ConvertFrom-Json
        $procId = [int]$j.pid
        if ($j.createdUtc) { $born = ([datetime]$j.createdUtc).ToUniversalTime() }
    }
    if ($procId -le 0) { throw 'no-lane-pid' }
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    return -not ($born -and $proc.StartTime.ToUniversalTime() -gt $born.AddSeconds(1))
}
function Save-Bytes([string]$Ext, [byte[]]$Bytes) {
    for ($n = 1; ; $n++) {
        $name = if ($n -eq 1) { "dirty-save-$head8.$Ext" } else { "dirty-save-$head8.$n.$Ext" }
        $f = Join-Path $RunDir $name
        try { $fs = [System.IO.File]::Open($f, 'CreateNew', 'Write', 'None') }
        catch [System.IO.IOException] {
            if (-not (Test-Path -LiteralPath $f)) { throw }
            $same = try { [System.Linq.Enumerable]::SequenceEqual([byte[]][System.IO.File]::ReadAllBytes($f), $Bytes) } catch { $false }
            if ($same) { return $name }
            continue
        }
        try { $fs.Write($Bytes, 0, $Bytes.Length) } finally { $fs.Dispose() }
        return $name
    }
}
try {
    $WorkDir = (Resolve-Path -LiteralPath $WorkDir).ProviderPath; $RunDir = (Resolve-Path -LiteralPath $RunDir).ProviderPath
    if (-not (Test-Path -LiteralPath $WorkDir -PathType Container) -or -not (Test-Path -LiteralPath $RunDir -PathType Container)) { throw 'no-such-directory' }
    if (Test-LaneAlive) { Write-Output 'DIRTY_SAVE_REFUSED reason=lane-alive'; exit 20 }
} catch { Write-Output "DIRTY_SAVE_BAD_ARGS reason=$($_.Exception.Message)"; exit 30 }
$flags = @('--binary', '--no-color', '--no-ext-diff', '--no-textconv', '--unified=3', '--src-prefix=a/', '--dst-prefix=b/')
$git = [ordered]@{ head = Invoke-GitBytes @('rev-parse', 'HEAD'); patch = Invoke-GitBytes (@('diff', 'HEAD') + $flags); staged = Invoke-GitBytes (@('diff', '--cached') + $flags)
    untracked = Invoke-GitBytes @('ls-files', '-z', '--others', '--exclude-standard') }
$bad = $git.Values | Where-Object { $_.Code -ne 0 } | Select-Object -First 1
if ($bad) { Write-Output "DIRTY_SAVE_GIT_FAILED reason=$($bad.Err.Trim())"; exit 40 }
$todo = 'patch', 'staged', 'untracked' | Where-Object { $git[$_].Bytes.Length -gt 0 }
if (-not $todo) { Write-Output 'DIRTY_SAVE_NOTHING reason=clean-tree'; exit 10 }
$headFull = [System.Text.Encoding]::ASCII.GetString($git.head.Bytes).Trim(); $head8 = $headFull.Substring(0, 8)
$ext = @{ patch = 'patch'; staged = 'staged.patch'; untracked = 'untracked.txt' }
$r = [ordered]@{ schema = 'lane-dirty-save/v1'; head = $headFull; untrackedFormat = 'nul-separated'; untrackedContents = 'not-saved' }
foreach ($k in $todo) { $r[$k] = Save-Bytes $ext[$k] $git[$k].Bytes }
if ($r.Contains('patch')) { $r.patchSha256 = [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::HashData($git.patch.Bytes)).Replace('-', '').ToLowerInvariant() }
$receipt = $r | ConvertTo-Json -Compress
[void](Save-Bytes 'receipt.json' ([System.Text.UTF8Encoding]::new($false).GetBytes($receipt + "`n")))
Write-Output $receipt
exit 0