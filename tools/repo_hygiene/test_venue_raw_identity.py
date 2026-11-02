"""Tests for tools/profiling/dual-venue/VenueRawJson.psm1 (BACHELOR-THERMAL-GATE-WAIT-1 r13/r14) and its producer tools/profiling/um-run.ps1.

A lease id or token is compared as the RAW string the writer put in the file. The reader recovers it exactly or reports the reading as not exact and carries no identity
at all (adversary r13 A1-A4, Dot I1). The consumers (the history reader, the batch runner) are covered in test_venue_thermal_gate.py and test_venue_lease_batch.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DV = ROOT / "tools" / "profiling" / "dual-venue"
MODULE = DV / "VenueRawJson.psm1"
UM_RUN = ROOT / "tools" / "profiling" / "um-run.ps1"
PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")

LEASE = "2026-10-30T10:00:00Z"
OPEN_RESULT = {"exitCode": 0, "admission": {"label": "clear", "slotContinuity": "kept", "leaseBatch": {"token": "0123456789ab", "leaseId": LEASE}, "leaseRenew": {"okMaxGapSec": 60}}}


def read(text: str) -> dict:
    """Read-VenueJsonRawIdentity on `text`: {exact, leaseId (the value's top-level or admission.leaseBatch one, with its .NET type), threw}."""
    script = (f"$ErrorActionPreference = 'Stop'; Import-Module '{MODULE}' -Force -DisableNameChecking\n"
              "$text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:RAW_TEXT))\n"
              "try { $r = Read-VenueJsonRawIdentity -Text $text } catch { [pscustomobject]@{ threw = $true } | ConvertTo-Json -Compress; return }\n"
              "$v = $r.value; $o = $(if ($v -is [array]) { $v[0] } else { $v })\n"
              "$lb = $(if ($null -ne $o -and $o.PSObject.Properties['admission']) { $o.admission.leaseBatch } else { $o })\n"
              "[pscustomobject]@{ threw = $false; exact = $r.exact; leaseId = $lb.leaseId; leaseType = $(if ($null -ne $lb.leaseId) { $lb.leaseId.GetType().Name } else { 'null' });\n"
              "                   token = $lb.token; tokenType = $(if ($null -ne $lb.token) { $lb.token.GetType().Name } else { 'null' }) } | ConvertTo-Json -Compress")
    import base64
    env = dict(os.environ, RAW_TEXT=base64.b64encode(text.encode("utf-8")).decode("ascii"))
    r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120, env=env, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise AssertionError(r.stdout + r.stderr)
    return json.loads(r.stdout)


@requires_windows_pwsh
class RawIdentityReaderTests(unittest.TestCase):
    def test_a_timestamp_shaped_lease_id_and_token_are_the_raw_strings(self) -> None:
        got = read(json.dumps({"leaseId": LEASE, "token": "2026-10-30T10:00:00.0Z"}))
        self.assertEqual((got["exact"], got["leaseId"], got["leaseType"], got["token"], got["tokenType"]), (True, LEASE, "String", "2026-10-30T10:00:00.0Z", "String"), got)

    def test_a_nested_lease_id_is_the_raw_string(self) -> None:
        got = read(json.dumps(OPEN_RESULT))
        self.assertEqual((got["exact"], got["leaseId"], got["leaseType"]), (True, LEASE, "String"), got)

    def test_r13_adversary_a2_a_lease_id_property_spelled_with_other_case_is_the_lease_id(self) -> None:
        # callers read `.leaseId`, which PowerShell matches without regard to case, so the restoration matches the same way
        for spelling in ("LeaseId", "LEASEID", "leaseid"):
            got = read(json.dumps({spelling: LEASE, "Token": "t"}))
            self.assertEqual((got["exact"], got["leaseId"], got["leaseType"], got["token"]), (True, LEASE, "String", "t"), (spelling, got))

    def test_r13_adversary_a2_two_identity_properties_that_differ_only_by_case_are_not_a_reading(self) -> None:
        # ConvertFrom-Json refuses the text; the history reader turns that into a barrier row (test_venue_thermal_gate.py)
        self.assertTrue(read('{"leaseId": "L1", "LeaseId": "L2"}')["threw"])

    def test_r14_two_identity_properties_of_one_exact_name_are_not_exact_and_carry_no_identity(self) -> None:
        got = read('{"leaseId": "2026-10-30T10:00:00Z", "leaseId": "2026-10-30T10:00:00.0Z", "token": "t"}')
        self.assertEqual((got["exact"], got["leaseId"], got["token"]), (False, None, None), got)

    def test_r13_adversary_a3_a_one_element_root_array_is_not_exact_and_carries_no_identity(self) -> None:
        # ConvertFrom-Json unwraps it to its element, so no restoration shape applies and the id would stay a converted DateTime
        got = read(json.dumps([{"leaseId": LEASE, "token": "t"}]))
        self.assertEqual((got["exact"], got["leaseId"], got["leaseType"], got["token"]), (False, None, "null", None), got)

    def test_r13_adversary_a4_text_the_strict_parser_rejects_is_not_exact_and_carries_no_identity(self) -> None:
        # ConvertFrom-Json accepts single quotes, System.Text.Json does not: the raw ids cannot be recovered, and a converted DateTime must not survive as one
        got = read("{'leaseId': '2026-10-30T10:00:00Z', 'token': 't'}")
        self.assertEqual((got["exact"], got["leaseId"], got["leaseType"], got["token"]), (False, None, "null", None), got)

    def test_a_reading_without_identity_fields_is_exact(self) -> None:
        got = read('{"exitCode": 0, "stdout": "x"}')
        self.assertEqual((got["exact"], got["leaseId"]), (True, None), got)


@requires_windows_pwsh
class UmRunResultReaderTests(unittest.TestCase):
    """BACHELOR-THERMAL-GATE-WAIT-1 r14 (adversary r13 A1): the real um-run.ps1 returns the agent's result with the lease id as the raw string."""

    def test_r13_adversary_a1_um_run_returns_the_open_results_lease_id_as_the_raw_string(self) -> None:
        with tempfile.TemporaryDirectory(prefix="umrun-raw-") as tmp:
            t = Path(os.path.realpath(tmp))
            share = t / "agent"
            (share / "inbox").mkdir(parents=True)
            (share / "outbox").mkdir()
            (share / "heartbeat.txt").write_text("alive", encoding="utf-8")
            job = t / "demo.job.ps1"
            job.write_text("Write-Output 'hi'\n", encoding="utf-8")
            staged = t / "staged.json"
            staged.write_text(json.dumps(OPEN_RESULT), encoding="utf-8")
            script = (f"$ErrorActionPreference = 'Stop'\n"
                      f"$hook = {{ Copy-Item -LiteralPath '{staged}' -Destination '{share / 'outbox' / 'raw1.result.json'}' }}\n"
                      f"$r = & '{UM_RUN}' -ScriptPath '{job}' -AgentShare '{share}' -PollSeconds 1 -JobId raw1 -TestHookAtLoopTop $hook 6>$null\n"
                      "$id = $r.admission.leaseBatch.leaseId\n"
                      "[pscustomobject]@{ type = $id.GetType().Name; id = [string]$id; token = [string]$r.admission.leaseBatch.token } | ConvertTo-Json -Compress")
            r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]), {"type": "String", "id": LEASE, "token": "0123456789ab"})


if __name__ == "__main__":
    unittest.main()
