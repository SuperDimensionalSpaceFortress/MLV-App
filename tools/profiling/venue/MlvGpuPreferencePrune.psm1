# MlvGpuPreferencePrune.psm1 -- the PURE selection logic behind Prune-MlvGpuPreferences.ps1 (GPU-PREF-REGISTRY-STOP-AND-PRUNE-1).
#
# Nothing in this module touches the registry or the file system by itself: the file-system questions are
# injected as script blocks, so tools/repo_hygiene/test_prune_mlv_gpu_preferences.py can drive it offline
# against a fake value list. ASCII only, on purpose (Windows PowerShell 5.1 reads a BOM-less file as ANSI).
#
# A value NAME in the DirectX per-app GPU-preference key is an exe path. A value is PRUNABLE only when ALL hold:
#   1. the name is an absolute drive path (X:\...), with no '..' segment (a traversal is never judged);
#   2. it is STRICTLY under one of the MLV roots (a venue agentRoot / scratchRoot from venues.json, or one of the
#      always-present build roots C:\mlvtmp, C:\dell-bench, C:\temp);
#   3. that root is itself present right now (an unmounted volume proves nothing about its files);
#   4. the file the name points at no longer exists.
# With -IncludeExisting (Select-MlvStaleGpuPreferenceValue), rule 4 is relaxed for ONE name shape only: a value
# whose file still exists is also prunable when its file name is MLVApp-*.exe (a staged build). Rules 1-3 still
# apply, so an exe outside the roots is never pruned, with or without the switch.
# Everything else is KEPT, with the reason, so the dry run shows why each value stayed.

Set-StrictMode -Version 2.0

function ConvertTo-MlvPrefPath {
    # Slashes to backslashes, trim, drop trailing backslashes (but keep 'X:\'); no .NET path APIs, so the same
    # answer comes out on any host the test suite runs on.
    param([AllowNull()][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return '' }
    $p = $Path.Trim().Replace('/', '\')
    while ($p.Length -gt 3 -and $p.EndsWith('\')) { $p = $p.Substring(0, $p.Length - 1) }
    $p
}

function Get-MlvGpuPreferenceRoots {
    # The MLV agent/scratch roots named by venues.json plus the build roots the stale values point at
    # (C:\mlvtmp, C:\dell-bench, C:\temp -- ALWAYS included, whatever -ExtraRoots says). Distinct, case-insensitive.
    param(
        [Parameter(Mandatory)][string]$VenuesJsonPath,
        [string[]]$ExtraRoots = @()
    )
    $json = Get-Content -LiteralPath $VenuesJsonPath -Raw | ConvertFrom-Json
    $roots = New-Object System.Collections.Generic.List[string]
    foreach ($venue in $json.venues.PSObject.Properties) {
        foreach ($field in @('agentRoot', 'scratchRoot')) {
            $value = $venue.Value.PSObject.Properties[$field]
            if ($value -and $value.Value) { $roots.Add((ConvertTo-MlvPrefPath $value.Value)) }
        }
    }
    foreach ($required in @('C:\mlvtmp', 'C:\dell-bench', 'C:\temp')) { $roots.Add((ConvertTo-MlvPrefPath $required)) }
    foreach ($extra in $ExtraRoots) {
        if ($extra) { $roots.Add((ConvertTo-MlvPrefPath $extra)) }
    }
    $seen = @{}
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($root in $roots) {
        if ($root -match '^[A-Za-z]:\\.' -and -not $seen.ContainsKey($root.ToLowerInvariant())) {
            $seen[$root.ToLowerInvariant()] = $true
            $out.Add($root)
        }
    }
    $out.ToArray()
}

function Select-MlvStaleGpuPreferenceValue {
    # Classify every value name. Returns one object per name: Name, Verdict, Root, Reason.
    #   Verdict 'prune'                    -- under an MLV root, root present, file gone; or, with -IncludeExisting,
    #                                         the file is still there but is an MLVApp-*.exe build (ExistingBuild=$true)
    #   Verdict 'keep-file-exists'         -- under an MLV root and the file is still there (and not pruned by the above)
    #   Verdict 'keep-root-unavailable'    -- under an MLV root that is not present right now
    #   Verdict 'keep-outside-roots'       -- not under any MLV root (the owner's own entries, Windows' entries)
    #   Verdict 'keep-unjudged-path'       -- not an absolute drive path, or carries a '..' segment
    param(
        [AllowEmptyCollection()][AllowNull()][string[]]$Names = @(),
        [Parameter(Mandatory)][string[]]$Roots,
        [Parameter(Mandatory)][scriptblock]$FileExists,
        [Parameter(Mandatory)][scriptblock]$RootExists,
        [switch]$IncludeExisting
    )
    $normalRoots = @($Roots | ForEach-Object { ConvertTo-MlvPrefPath $_ } | Where-Object { $_ -match '^[A-Za-z]:\\.' })
    foreach ($name in @($Names)) {
        $norm = ConvertTo-MlvPrefPath $name
        if ($norm -notmatch '^[A-Za-z]:\\.' -or ($norm -split '\\') -contains '..' -or ($norm -split '\\') -contains '.') {
            [pscustomobject]@{ Name = $name; Verdict = 'keep-unjudged-path'; Root = $null; Reason = 'not an absolute drive path, or carries a . / .. segment' }
            continue
        }
        $hit = $null
        foreach ($root in $normalRoots) {
            if ($norm.Length -gt $root.Length + 1 -and $norm.StartsWith($root + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
                if ($null -eq $hit -or $root.Length -gt $hit.Length) { $hit = $root }
            }
        }
        if ($null -eq $hit) {
            [pscustomobject]@{ Name = $name; Verdict = 'keep-outside-roots'; Root = $null; Reason = 'not under an MLV agent/scratch root' }
            continue
        }
        if (-not [bool](& $RootExists $hit)) {
            [pscustomobject]@{ Name = $name; Verdict = 'keep-root-unavailable'; Root = $hit; Reason = 'the root is not present right now' }
            continue
        }
        if ([bool](& $FileExists $norm)) {
            $leaf = $norm.Substring($norm.LastIndexOf('\') + 1)
            if ($IncludeExisting -and $leaf -match '^MLVApp-.*\.exe$') {
                [pscustomobject]@{ Name = $name; Verdict = 'prune'; Root = $hit; Reason = 'under an MLV root; existing MLVApp-*.exe build (-IncludeExisting)'; ExistingBuild = $true }
            }
            else {
                [pscustomobject]@{ Name = $name; Verdict = 'keep-file-exists'; Root = $hit; Reason = 'the file still exists'; ExistingBuild = $false }
            }
            continue
        }
        [pscustomobject]@{ Name = $name; Verdict = 'prune'; Root = $hit; Reason = 'under an MLV root and the file no longer exists'; ExistingBuild = $false }
    }
}

Export-ModuleMember -Function ConvertTo-MlvPrefPath, Get-MlvGpuPreferenceRoots, Select-MlvStaleGpuPreferenceValue
