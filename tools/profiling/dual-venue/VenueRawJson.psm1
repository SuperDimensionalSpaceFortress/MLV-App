# VenueRawJson.psm1 -- BACHELOR-THERMAL-GATE-WAIT-1 r13/r14. One JSON reader for the lease-batch receipts and agent results whose IDENTITY fields (leaseId, token) are compared: the
# identity is the RAW string the writer put in the file.
#
# WHY. `ConvertFrom-Json` turns a timestamp-shaped string ("2026-10-30T10:00:00Z") into a DateTime on some PowerShell versions (and leaves it a string on others); the [string] cast
# then prints it in the host's culture. Two DISTINCT ids that read as one instant ("...:00Z", "...:00.0Z", "...+00:00") became one identity, and the raw lexical id was lost. The
# -DateKind String switch exists only from PowerShell 7.5, so this reader does not depend on it: it parses the same text with System.Text.Json and writes the raw string of every
# `leaseId` / `token` property back over the converted one, at any depth (admission.leaseBatch.leaseId, a batch receipt's legs, ...). Every other property is ConvertFrom-Json's, unchanged.
#
# FAIL CLOSED (r14, adversary r13 A2/A3/A4, Dot I1). The raw id is recovered EXACTLY or not at all. Callers read `.leaseId` / `.token`, which PowerShell matches without regard to case, so
# the property name is matched the same way here (a `LeaseId` is the lease id); two identity properties that differ only by case, a one-element root array (which ConvertFrom-Json
# unwraps to its element, so no restoration shape applies), text System.Text.Json rejects but ConvertFrom-Json accepted (single quotes, ...) and any shape disagreement between the two
# parses are NOT exact. A reading that is not exact has NO identity anywhere in it (every leaseId / token is $null, never a converted or guessed one), and says so: callers that
# group receipts (Read-VenueLegHeatHistory) treat it as a barrier that still needs its on-lease proof, and every ordinal comparison of an absent id simply fails.

function Test-VenueIdentityName {
    param([string]$Name)
    [string]::Equals($Name, 'leaseId', [StringComparison]::OrdinalIgnoreCase) -or [string]::Equals($Name, 'token', [StringComparison]::OrdinalIgnoreCase)
}

function Restore-VenueRawIdentity {
    # Writes the raw text of each identity property over ConvertFrom-Json's value. Returns $true only when both parses have the same shape all the way down and no object holds
    # two identity properties of one name.
    param($Obj, [System.Text.Json.JsonElement]$Element)
    $kind = $Element.ValueKind
    if ($kind -eq [System.Text.Json.JsonValueKind]::Object) {
        if ($Obj -isnot [System.Management.Automation.PSCustomObject]) { return $false }
        $ok = $true
        $seen = @{}
        foreach ($p in $Element.EnumerateObject()) {
            $prop = $Obj.PSObject.Properties[$p.Name]
            if ($null -eq $prop) { $ok = $false; continue }
            if (Test-VenueIdentityName $p.Name) {
                if ($seen.ContainsKey($p.Name)) { $ok = $false; continue }
                $seen[$p.Name] = $true
                $vk = $p.Value.ValueKind
                $prop.Value = $(if ($vk -eq [System.Text.Json.JsonValueKind]::String) { $p.Value.GetString() } elseif ($vk -eq [System.Text.Json.JsonValueKind]::Null) { $null } else { $p.Value.GetRawText() })
            } elseif (-not (Restore-VenueRawIdentity -Obj $prop.Value -Element $p.Value)) { $ok = $false }
        }
        return $ok
    }
    if ($kind -eq [System.Text.Json.JsonValueKind]::Array) {
        if ($Obj -isnot [array]) { return $false }
        if ($Obj.Count -ne $Element.GetArrayLength()) { return $false }
        $ok = $true
        $i = 0
        foreach ($e in $Element.EnumerateArray()) { if (-not (Restore-VenueRawIdentity -Obj $Obj[$i] -Element $e)) { $ok = $false }; $i++ }
        return $ok
    }
    $true
}

function Clear-VenueIdentity {
    # Every identity property, at any depth, becomes $null: a reading that is not exact carries no identity at all.
    param($Obj)
    if ($Obj -is [System.Management.Automation.PSCustomObject]) {
        foreach ($prop in @($Obj.PSObject.Properties)) {
            if (Test-VenueIdentityName $prop.Name) { $prop.Value = $null } else { Clear-VenueIdentity -Obj $prop.Value }
        }
    } elseif ($Obj -is [array]) { foreach ($e in $Obj) { Clear-VenueIdentity -Obj $e } }
}

function Read-VenueJsonRawIdentity {
    <#
    .SYNOPSIS
    ConvertFrom-Json with every leaseId / token property left as the raw string in the text (never a DateTime), and whether that reading is exact. Throws, like ConvertFrom-Json, on text that is not JSON.
    .OUTPUTS
    [pscustomobject]@{ value; exact } -- when exact is $false every leaseId / token in value is $null (see the header).
    #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    $obj = $Text | ConvertFrom-Json
    if ($null -eq $obj -or ($obj -isnot [System.Management.Automation.PSCustomObject] -and $obj -isnot [array])) { return [pscustomobject]@{ value = $obj; exact = $true } }
    $exact = $false
    $doc = $null
    try {
        $opt = [System.Text.Json.JsonDocumentOptions]::new()
        $opt.MaxDepth = 1024; $opt.AllowTrailingCommas = $true; $opt.CommentHandling = [System.Text.Json.JsonCommentHandling]::Skip
        $doc = [System.Text.Json.JsonDocument]::Parse($Text, $opt)
        $root = $doc.RootElement
        # a one-element root array reaches here as its element: the shapes differ, so nothing was restored and the reading is not exact
        if (-not ($root.ValueKind -eq [System.Text.Json.JsonValueKind]::Array -and $obj -isnot [array])) { $exact = [bool](Restore-VenueRawIdentity -Obj $obj -Element $root) }
    } catch { $exact = $false }
    finally { if ($null -ne $doc) { $doc.Dispose() } }
    if (-not $exact) { Clear-VenueIdentity -Obj $obj }
    [pscustomobject]@{ value = $obj; exact = $exact }
}

function ConvertFrom-VenueJsonRawIdentity {
    <#
    .SYNOPSIS
    Read-VenueJsonRawIdentity's value alone: the parsed JSON with raw identity strings, or with no identity at all when the reading is not exact.
    #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    (Read-VenueJsonRawIdentity -Text $Text).value
}

Export-ModuleMember -Function Read-VenueJsonRawIdentity, ConvertFrom-VenueJsonRawIdentity
