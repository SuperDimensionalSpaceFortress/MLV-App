<#
.SYNOPSIS
    The ONE definition of the GitHub repository slug the tools/coordination callers talk to.
    DOT-SOURCED (never run) by Invoke-Workstream.ps1, compose-lane-prompt-core.ps1,
    landing-probe.ps1 and Remove-LaneBulkScratch.ps1.

.DESCRIPTION
    The owner transferred the repository on 2026-10-04 (the old owner's MLV-App ->
    SuperDimensionalSpaceFortress/MLV-App). The old name only keeps working while GitHub's
    redirect does, and the redirect stops the day any account creates a repository at the old name,
    so no live caller may carry the old slug (test_repo_slug_hygiene.py enforces it).

    Two callers keep their own LITERAL on purpose, and a test pins each literal to this value:
      - set-required-checks.ps1: its owner approval is bound to the SHA-256 of that one file, so
        the target of its branch-protection PATCH must live inside the hashed bytes, not in a
        sibling the approval does not cover.
      - Export-PrReviewEvidence.ps1: the repository is a hardcoded pin, not a parameter, and its
        test asserts the literal; it also pins the immutable repository id.

    A function, not a variable: a dot-sourced $script: variable lands in whichever scope did the
    dot-sourcing, a function resolves the same way from every caller. NO Set-StrictMode here
    (see landing-probe.ps1: a dot-sourced helper must not change its caller's semantics).
    ASCII-only by project convention.
#>

function Get-MlvRepoSlug {
    return 'SuperDimensionalSpaceFortress/MLV-App'
}
