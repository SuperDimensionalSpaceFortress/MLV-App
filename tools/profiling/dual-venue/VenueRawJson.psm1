# VenueRawJson.psm1 -- BACHELOR-THERMAL-GATE-WAIT-1 r13 (adversary r12 A1 / Dot "version-qualified risk"). One JSON reader for the lease-batch receipts and agent results whose
# IDENTITY fields (leaseId, token) are compared: the identity is the RAW string the writer put in the file.
#
# WHY. `ConvertFrom-Json` turns a timestamp-shaped string ("2026-10-30T10:00:00Z") into a DateTime on some PowerShell versions (and leaves it a string on others); the [string] cast
# then prints it in the host's culture. Two DISTINCT ids that read as one instant ("...:00Z", "...:00.0Z", "...+00:00") became one identity, and the raw lexical id was lost. The
# -DateKind String switch exists only from PowerShell 7.5, so this reader does not depend on it: it parses the same text with System.Text.Json and writes the raw string of every
# `leaseId` / `token` property back over the converted one, at any depth (admission.leaseBatch.leaseId, a batch receipt's legs, ...). Every other property is ConvertFrom-Json's, unchanged.

function Restore-VenueRawIdentity {
    param($Obj, [System.Text.Json.JsonElement]$Element)
    $kind = $Element.ValueKind
    if ($kind -eq [System.Text.Json.JsonValueKind]::Object -and $Obj -is [System.Management.Automation.PSCustomObject]) {
        foreach ($p in $Element.EnumerateObject()) {
            $prop = $Obj.PSObject.Properties[$p.Name]
            if ($null -eq $prop) { continue }
            if ($p.Name -ceq 'leaseId' -or $p.Name -ceq 'token') {
                $vk = $p.Value.ValueKind
                $prop.Value = $(if ($vk -eq [System.Text.Json.JsonValueKind]::String) { $p.Value.GetString() } elseif ($vk -eq [System.Text.Json.JsonValueKind]::Null) { $null } else { $p.Value.GetRawText() })
            } else { Restore-VenueRawIdentity -Obj $prop.Value -Element $p.Value }
        }
    } elseif ($kind -eq [System.Text.Json.JsonValueKind]::Array -and $Obj -is [array]) {
        $i = 0
        foreach ($e in $Element.EnumerateArray()) { if ($i -lt $Obj.Count) { Restore-VenueRawIdentity -Obj $Obj[$i] -Element $e }; $i++ }
    }
}

function Clear-VenueConvertedIdentity {
    # System.Text.Json refused a text ConvertFrom-Json accepted (a lenient extension): the raw ids cannot be recovered, so an id that was converted to a date is dropped (no identity,
    # never a wrong one); everything else stays as read.
    param($Obj)
    if ($Obj -is [System.Management.Automation.PSCustomObject]) {
        foreach ($prop in @($Obj.PSObject.Properties)) {
            if (($prop.Name -ceq 'leaseId' -or $prop.Name -ceq 'token') -and ($prop.Value -is [datetime] -or $prop.Value -is [datetimeoffset])) { $prop.Value = $null }
            else { Clear-VenueConvertedIdentity -Obj $prop.Value }
        }
    } elseif ($Obj -is [array]) { foreach ($e in $Obj) { Clear-VenueConvertedIdentity -Obj $e } }
}

function ConvertFrom-VenueJsonRawIdentity {
    <#
    .SYNOPSIS
    ConvertFrom-Json with every leaseId / token property left as the raw string in the text (never a DateTime). Throws, like ConvertFrom-Json, on text that is not JSON.
    #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    $obj = $Text | ConvertFrom-Json
    if ($null -eq $obj -or ($obj -isnot [System.Management.Automation.PSCustomObject] -and $obj -isnot [array])) { return $obj }
    $doc = $null
    try {
        $opt = [System.Text.Json.JsonDocumentOptions]::new()
        $opt.MaxDepth = 1024; $opt.AllowTrailingCommas = $true; $opt.CommentHandling = [System.Text.Json.JsonCommentHandling]::Skip
        $doc = [System.Text.Json.JsonDocument]::Parse($Text, $opt)
        Restore-VenueRawIdentity -Obj $obj -Element $doc.RootElement
    } catch { Clear-VenueConvertedIdentity -Obj $obj }
    finally { if ($null -ne $doc) { $doc.Dispose() } }
    $obj
}

Export-ModuleMember -Function ConvertFrom-VenueJsonRawIdentity
