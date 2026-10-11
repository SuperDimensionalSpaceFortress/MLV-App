# lane-local-playback-lint.ps1 -- HUB-VM-NO-LOCAL-PLAYBACK-1 (owner objection 2026-10-11). Dot-source it; it defines three functions and runs nothing.
#
#   Get-HubHostConfig                       the tracked host list (tools/profiling/dual-venue/hub-hosts.json)
#   Test-HubHost [-ComputerName <name>]     $true when this host is a hub host and the CI-fixture override is NOT set
#   Get-LocalPlaybackPromptFindings -Text   one string per sentence that tells a lane to run MLVApp playback locally (wrap the call in @())
#
# Why: the 2026-10-11 incident. A producer prompt said to run the exactness arms "where a GUI desktop session and the clip exist"; the lane found
# a copy of the clip on the hub VM and launched MLVApp.exe gui-smoke-playback there. The hub VM is a guest on Ultra-Magnus, so the window appeared
# on the owner's machine and the run contaminated the Ultra-Magnus venue legs. The app refuses that entry on a hub host (platform/qt/HubHostPlaybackGuard.h);
# this lint stops the INSTRUCTION earlier: Invoke-Lane.ps1 refuses a lane on a hub host whose prompt asks for local playback, and
# .claude-state\hub-autopilot\dispatch-perf-lane.ps1 refuses such a producer prompt before any lane is created.
#
# The binding "NO LOCAL PLAYBACK" section the dispatch scripts append is excluded from the scan (it names the forbidden flags on purpose), and a
# sentence that is itself a prohibition (never / do not / must not / forbidden / refuse ...) is not an instruction. A sentence that routes the work
# through a venue (Bachelor, Ultra-Magnus, the venue chain) is not a local launch.
#
# ASCII only (PowerShell 5.1 reads a BOM-less file as the ANSI code page). No Set-StrictMode here: a dot-sourced file would leak it into the caller.

function Get-HubHostConfig {
    [CmdletBinding()]
    param([string]$Path = (Join-Path $PSScriptRoot '..\profiling\dual-venue\hub-hosts.json'))
    if (-not (Test-Path -LiteralPath $Path)) { throw "hub-hosts.json not found: $Path" }
    $cfg = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if ($cfg.schema -ne 'mlv-app/hub-hosts/v1') { throw "hub-hosts.json: unexpected schema '$($cfg.schema)'" }
    if (@($cfg.hubHosts).Count -lt 1) { throw 'hub-hosts.json: hubHosts is empty' }
    return $cfg
}

function Test-HubHost {
    [CmdletBinding()]
    param([string]$ComputerName = $env:COMPUTERNAME, [string]$HostsFile)
    $cfg = if ($HostsFile) { Get-HubHostConfig -Path $HostsFile } else { Get-HubHostConfig }
    $overrideNow = [Environment]::GetEnvironmentVariable([string]$cfg.ciFixtureOverrideEnv)
    if ($overrideNow -and $overrideNow.Trim() -ceq [string]$cfg.ciFixtureOverrideValue) { return $false }
    $name = ([string]$ComputerName).Trim()
    if (-not $name) { return $false }
    foreach ($h in @($cfg.hubHosts)) { if ($name -ieq [string]$h) { return $true } }
    return $false
}

# Drop every "## NO LOCAL PLAYBACK ..." section (heading to the next heading of the same or a higher level, or the end of the text).
function Remove-NoLocalPlaybackSection {
    param([string]$Text)
    $out = New-Object System.Collections.Generic.List[string]
    $skipLevel = 0
    foreach ($line in ($Text -split "\r?\n")) {
        if ($line -match '^(#{1,6})\s+(.*)$') {
            $level = $Matches[1].Length
            if ($skipLevel -gt 0 -and $level -le $skipLevel) { $skipLevel = 0 }
            if ($skipLevel -eq 0 -and $Matches[2] -match '^NO LOCAL PLAYBACK\b') { $skipLevel = $level; continue }
        }
        if ($skipLevel -eq 0) { $out.Add($line) }
    }
    return ($out -join "`n")
}

function Get-LocalPlaybackPromptFindings {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)

    $scan = Remove-NoLocalPlaybackSection -Text $Text
    # A verb or product word is bounded by non-word, non-hyphen characters, so "run-release-gui-smoke" is not the verb "run" and
    # "MLVAPP_C1_VECTORS_DIR" is not the app.
    $b = '(?<![\w\-])'; $e = '(?![\w\-])'
    $strongNeg = "(?i)${b}(never|do not|don't|must not|mustn't|may not|no local|not locally|forbidden|forbids|refuse[sd]?|refusal|prohibit\w*|instead of running|rather than running|without running)${e}"
    $weakNeg = "(?i)${b}(not|no|without)${e}"
    $venueQualified = "(?i)${b}(venue|venues|bachelor|ultra-magnus|um-run|Invoke-Venue(Chain|Leg)|chain runner)${e}"
    $launchVerb = "(?i)${b}(run|runs|running|launch|launches|launching|launched|start|starts|starting|execute|executes|executing|invoke|invokes|invoking|drive|drives|driving|kick off)${e}"
    $playbackWord = "(?i)(${b}playback${e}|gui-smoke|profile-playback|${b}MLVApp(Release)?(\.exe)?${e}|smoke run|${b}exactness${e}|${b}arms?${e}|${b}GUI${e})"
    $scripts = '(run-release-gui-smoke|start-release-cuda-playback|run-release-playback-profile|validate-visible-playback|capture-reference-frame)\.ps1'
    $findings = New-Object System.Collections.Generic.List[string]
    # A "sentence" is a line, cut again at '. ' so one bullet carrying several instructions is judged piece by piece.
    foreach ($line in ($scan -split "\r?\n")) {
        foreach ($sentence in ($line -split '(?<=[.;!?])\s+')) {
            $s = $sentence.Trim()
            if ($s.Length -lt 8) { continue }
            if ($s -match $strongNeg) { continue }
            $hit = $null
            # (1) the incident phrase: "where a GUI desktop session and the clip exist" and its relatives
            # A phrase that opens with a quote mark is a QUOTATION of the old wording (an incident report), not an instruction.
            if ($s -match ('(?i)(?<![\w\-"''`])where\s+an?\s+(gui|desktop|display)(\s+desktop)?\s+session' + $e + '[^.\n]{0,100}' + $b + 'exists?' + $e)) { $hit = 'where-a-gui-session-exists' }
            # (2) "locally" attached to a playback / smoke / MLVApp / arm instruction
            elseif ($s -match "(?i)${b}locally${e}" -and $s -match $playbackWord -and $s -match $launchVerb) { $hit = 'run-playback-locally' }
            elseif ($s -match $weakNeg -or $s -match $venueQualified) { continue }
            # (3) "on this host / the hub (VM)" attached to a playback launch
            elseif ($s -match "(?i)${b}on\s+(this|the)\s+(hub|host|vm|machine|box)${e}|${b}on\s+virtual-ten${e}" -and $s -match $playbackWord -and $s -match $launchVerb) { $hit = 'run-playback-on-this-host' }
            # (4) a direct command line: the app with a playback mode, or a tracked launcher called with a parameter
            elseif ($s -match "(?i)${b}MLVApp(Release)?\.exe\s+(?:(?!/\s)\S+\s+){0,12}--(gui-smoke-playback|profile-playback)" -or
                    $s -match "(?i)(pwsh|powershell)(\.exe)?${e}[^\n]{0,80}-File\s+\S*$scripts" -or
                    $s -match "(?i)(^|[\s``&(])(&\s*)?\.{0,2}[\\/]?\S*$scripts\s+-\w+") { $hit = 'direct-playback-launch' }
            if ($hit) {
                $shown = if ($s.Length -gt 220) { $s.Substring(0, 220) + '...' } else { $s }
                $findings.Add("${hit}: $shown")
            }
        }
    }
    return $findings.ToArray()
}
