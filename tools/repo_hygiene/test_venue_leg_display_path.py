"""VENUE-LEG-DISPLAY-PATH-RECEIPT-NARROW-2: a leg receipt carries ONE display-path block (`venue.displayPath`), written by ONE writer
(New-DvDisplayPathBlock) and read by ONE System.Text.Json reader (Read-DvReceiptDisplayPath).

WHY. VENUE-LEG-DISPLAY-PATH-RECEIPT-1 (PR #904, closed) carried the block through 15 rounds; its pass-2 majors came from its wider collector and
timing work, and one (A3) from reading the block with two grammars: ConvertFrom-Json keeps the last of two same-name keys, accepts `//` comments and
single quotes, reads a lone-surrogate escape and turns `2026-11-24T00:00:00Z` into a [datetime], while System.Text.Json does none of that. This card
keeps only the block: three states and one reader.

WHAT IS PINNED
  (a) A3 grammar parity: a block that System.Text.Json and ConvertFrom-Json read differently is `malformed` (or, for a time-looking text, the text
      exactly as written), never the ConvertFrom-Json reading;
  (b) any parse failure of the block types it `malformed`: no recovery, no fallback, no second reader;
  (c) a legacy receipt with NO block reads `NO_SIGNAL`, and Test-DvReceiptValid reads it exactly as fork/master does;
  (d) a valid block round-trips: writer -> Write-DvReceipt -> file text -> reader gives the same typed block, and Invoke-VenueLeg writes it.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_dual_venue_evidence import (
    DV, HEALTHY_PROBE, ROOT, RunnerHarness, requires_windows_pwsh, run_pwsh,
)

MODULE = DV / "DualVenueRunner.psm1"
SCHEMA = "mlv-app/venue-display-path/v2"
DOC = ROOT / "docs" / "dual-venue-evidence.md"

GOOD_BLOCK_TEXT = ('{"schema": "mlv-app/venue-display-path/v2", "source": "health-probe", '
                   '"adapters": ["Intel(R) Iris(R) Xe Graphics", "NVIDIA GeForce RTX 3060 Laptop GPU"], '
                   '"firstAdapterDriverVersion": "31.0.101.5186", "primaryDisplayDevice": "\\\\\\\\.\\\\DISPLAY1"}')
GOOD_BLOCK = {"schema": SCHEMA, "source": "health-probe",
              "adapters": ["Intel(R) Iris(R) Xe Graphics", "NVIDIA GeForce RTX 3060 Laptop GPU"],
              "firstAdapterDriverVersion": "31.0.101.5186", "primaryDisplayDevice": "\\\\.\\DISPLAY1"}


def legacy_receipt() -> dict:
    """A receipt in fork/master's shape (New-DvReceipt's skeleton fields that matter here): no `venue.displayPath`."""
    return {
        "schema": "mlv-app/dual-venue-receipt/v1", "receiptId": "00000000-0000-0000-0000-000000000001", "card": "DUAL-VENUE-EVIDENCE-1", "legId": "unit-leg",
        "subject": {"digest": None, "buildManifestSha256": None, "legSpecSha256": None, "clipId": None, "clipContentSha256": None, "backend": "cuda", "lookFlavor": None},
        "venue": {"name": "bachelor", "role": "supplementary", "declared": "bachelor", "detected": "bachelor", "hostName": "BACHELOR",
                  "gpuNames": ["Intel(R) Iris(R) Xe Graphics"], "driverVersion": "31.0.101.5186", "displayDevice": "\\\\.\\DISPLAY1",
                  "instrumentDigests": {"presentmon": None, "scorer": None}},
        "actor": "unit-test", "startedUtc": "2026-11-26T00:00:00.0000000Z", "finishedUtc": "2026-11-26T00:01:00.0000000Z",
        "health": {"outcome": "HEALTHY"}, "outcome": "UNRESOLVED", "outcomeDetail": "HEALTH_ONLY_NO_LEG_RUN", "model_verdicts": [],
    }


LEGACY_TEXT = json.dumps(legacy_receipt(), indent=2)


def with_block(block_text: str, venue_extra: str = "", root_extra: str = "") -> str:
    """The legacy receipt's TEXT with `"displayPath": <block_text>` spliced verbatim into `venue` (so a duplicate key or a comment survives)."""
    r = legacy_receipt()
    r["venue"]["displayPath"] = "__BLOCK__"
    text = json.dumps(r)
    text = text.replace('"displayPath": "__BLOCK__"', '"displayPath": ' + block_text + venue_extra)
    if root_extra:
        text = text[:-1] + root_extra + "}"
    return text


def read_texts(texts: list[str], reader: str = "Read-DvReceiptDisplayPath") -> list[dict]:
    """Run THE reader over each text in one pwsh; returns per text {state, reason, block, adapterTypes, threw}. The texts travel in a UTF-8 file read
    with System.Text.Json (never ConvertFrom-Json, so the harness itself cannot reinterpret a text)."""
    with tempfile.TemporaryDirectory() as d:
        cases = Path(d) / "cases.json"
        cases.write_text(json.dumps(texts, ensure_ascii=True), encoding="utf-8")
        script = (
            f"Import-Module '{MODULE}' -Force\n"
            f"$doc = [System.Text.Json.JsonDocument]::Parse([IO.File]::ReadAllText('{cases}'))\n"
            "$out = @(foreach ($e in $doc.RootElement.EnumerateArray()) {\n"
            "  $t = $e.GetString()\n"
            f"  try {{ $r = {reader} -ReceiptText $t\n"
            "        [ordered]@{ threw = $null; state = [string]$r.state; reason = $r.reason; block = $r.block;\n"
            "                    adapterTypes = @(if ($null -ne $r.block) { @($r.block.adapters) | ForEach-Object { $_.GetType().FullName } }) } }\n"
            "  catch { [ordered]@{ threw = [string]$_.Exception.Message; state = $null; reason = $null; block = $null; adapterTypes = @() } }\n"
            "})\n"
            "Write-Output ('CASES=' + (ConvertTo-Json -InputObject $out -Depth 8 -Compress))\n"
        )
        proc = run_pwsh(["-Command", script])
    for line in proc.stdout.splitlines():
        if line.startswith("CASES="):
            return json.loads(line[len("CASES="):])
    raise AssertionError(f"the reader harness printed no CASES line: rc={proc.returncode} stdout={proc.stdout[-2000:]} stderr={proc.stderr[-2000:]}")


def cfj_reads(texts: list[str]) -> list[dict]:
    """How ConvertFrom-Json (the grammar the reader must NOT use) reads venue.displayPath of each text: the premise of the A3 rows."""
    with tempfile.TemporaryDirectory() as d:
        cases = Path(d) / "cases.json"
        cases.write_text(json.dumps(texts, ensure_ascii=True), encoding="utf-8")
        script = (
            f"$doc = [System.Text.Json.JsonDocument]::Parse([IO.File]::ReadAllText('{cases}'))\n"
            "$out = @(foreach ($e in $doc.RootElement.EnumerateArray()) {\n"
            "  try { $o = $e.GetString() | ConvertFrom-Json; $b = $o.venue.displayPath\n"
            "        [ordered]@{ ok = $true; device = [string]$b.primaryDisplayDevice; adapter0Type = $(if (@($b.adapters).Count -gt 0) { @($b.adapters)[0].GetType().FullName } else { $null }) } }\n"
            "  catch { [ordered]@{ ok = $false; device = $null; adapter0Type = $null } }\n"
            "})\n"
            "Write-Output ('CASES=' + (ConvertTo-Json -InputObject $out -Depth 5 -Compress))\n"
        )
        proc = run_pwsh(["-Command", script])
    for line in proc.stdout.splitlines():
        if line.startswith("CASES="):
            return json.loads(line[len("CASES="):])
    raise AssertionError(f"the ConvertFrom-Json harness printed no CASES line: {proc.stdout[-2000:]} {proc.stderr[-2000:]}")


# (a) texts that System.Text.Json and ConvertFrom-Json read differently. Each is (label, text, expected state, cfj premise).
DUP_DEVICE = GOOD_BLOCK_TEXT[:-1] + ', "primaryDisplayDevice": "\\\\\\\\.\\\\DISPLAY2"}'
A3_ROWS = [
    ("duplicate field inside the block (ConvertFrom-Json keeps the last)", with_block(DUP_DEVICE), "malformed"),
    ("two displayPath members in venue", with_block(GOOD_BLOCK_TEXT, venue_extra=', "displayPath": ' + GOOD_BLOCK_TEXT.replace("DISPLAY1", "DISPLAY3")), "malformed"),
    ("two venue members at the root, the last one carries the block", with_block(GOOD_BLOCK_TEXT, root_extra=', "venue": {"name": "bachelor", "displayPath": ' + GOOD_BLOCK_TEXT + '}'), "malformed"),
    ("a // comment inside the block (ConvertFrom-Json accepts it)", with_block(GOOD_BLOCK_TEXT.replace('"source"', '// note\n"source"')), "malformed"),
    ("a single-quoted string inside the block (ConvertFrom-Json accepts it)", with_block(GOOD_BLOCK_TEXT.replace('"health-probe"', "'health-probe'")), "malformed"),
    ("a trailing comma inside the block (ConvertFrom-Json accepts it)", with_block(GOOD_BLOCK_TEXT[:-1] + ",}"), "malformed"),
    ("an escaped lone surrogate in an adapter name (ConvertFrom-Json reads it)", with_block(GOOD_BLOCK_TEXT.replace("Intel(R) Iris(R) Xe Graphics", "Intel\\ud800")), "malformed"),
    ("a field name differing only in case beside the real one", with_block(GOOD_BLOCK_TEXT[:-1] + ', "PrimaryDisplayDevice": null}'), "malformed"),
]
TIME_TEXT = "2026-11-24T00:00:00Z"
TIME_ROW = with_block(GOOD_BLOCK_TEXT.replace("NVIDIA GeForce RTX 3060 Laptop GPU", TIME_TEXT))


@requires_windows_pwsh
class GrammarParityA3Tests(unittest.TestCase):
    """(a) pass-2 A3: the block is read by System.Text.Json alone; a text the two grammars read differently never takes ConvertFrom-Json's reading."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.got = read_texts([t for _, t, _ in A3_ROWS] + [TIME_ROW])
        cls.cfj = cfj_reads([A3_ROWS[0][1], TIME_ROW])

    def test_the_premise_convertfrom_json_reads_these_texts_differently(self) -> None:
        self.assertTrue(self.cfj[0]["ok"])
        self.assertEqual(self.cfj[0]["device"], "\\\\.\\DISPLAY2", "premise: ConvertFrom-Json keeps the LAST of two same-name keys")
        self.assertEqual(self.cfj[1]["adapter0Type"], "System.String")  # adapters[0] is the Intel name; the time text is adapters[1]

    def test_each_grammar_difference_reads_malformed(self) -> None:
        for (label, _, want), got in zip(A3_ROWS, self.got):
            with self.subTest(label):
                self.assertIsNone(got["threw"], f"the reader threw instead of typing the block: {got['threw']}")
                self.assertEqual(got["state"], want, f"{label}: read {got['state']} ({got['reason']})")
                self.assertIsNone(got["block"], "a malformed block hands back no block")
                self.assertTrue(got["reason"], "a malformed block names its reason")

    def test_a_time_looking_adapter_name_stays_the_text_as_written(self) -> None:
        got = self.got[-1]
        self.assertIsNone(got["threw"])
        self.assertEqual(got["state"], "present", got["reason"])
        self.assertEqual(got["block"]["adapters"][1], TIME_TEXT)
        self.assertEqual(got["adapterTypes"], ["System.String", "System.String"], "every adapter name is a [string], never ConvertFrom-Json's [datetime]")


MALFORMED_ROWS = [
    ("the block is JSON null", with_block("null")),
    ("the block is an array", with_block("[" + GOOD_BLOCK_TEXT + "]")),
    ("the block is a string", with_block('"' + SCHEMA + '"')),
    ("the schema is the closed #904 v1", with_block(GOOD_BLOCK_TEXT.replace("/v2", "/v1"))),
    ("the schema differs in case", with_block(GOOD_BLOCK_TEXT.replace("mlv-app/venue", "MLV-app/venue"))),
    ("the source is not health-probe", with_block(GOOD_BLOCK_TEXT.replace('"health-probe"', '"collector"'))),
    ("a field is missing", with_block(GOOD_BLOCK_TEXT.replace(', "firstAdapterDriverVersion": "31.0.101.5186"', ""))),
    ("an extra field", with_block(GOOD_BLOCK_TEXT[:-1] + ', "refreshHz": 59}')),
    ("a field name in the wrong case only", with_block(GOOD_BLOCK_TEXT.replace('"primaryDisplayDevice"', '"primarydisplaydevice"'))),
    ("adapters is a string", with_block(GOOD_BLOCK_TEXT.replace('["Intel(R) Iris(R) Xe Graphics", "NVIDIA GeForce RTX 3060 Laptop GPU"]', '"Intel"'))),
    ("an adapter is a number", with_block(GOOD_BLOCK_TEXT.replace('"NVIDIA GeForce RTX 3060 Laptop GPU"', "3060"))),
    ("an adapter is null", with_block(GOOD_BLOCK_TEXT.replace('"NVIDIA GeForce RTX 3060 Laptop GPU"', "null"))),
    ("an adapter is an object", with_block(GOOD_BLOCK_TEXT.replace('"NVIDIA GeForce RTX 3060 Laptop GPU"', '{"name": "x"}'))),
    ("65 adapters", with_block(GOOD_BLOCK_TEXT.replace('["Intel(R) Iris(R) Xe Graphics", "NVIDIA GeForce RTX 3060 Laptop GPU"]', json.dumps(["a"] * 65)))),
    ("the driver version is a number", with_block(GOOD_BLOCK_TEXT.replace('"31.0.101.5186"', "31.0"))),
    ("the display device is a bool", with_block(GOOD_BLOCK_TEXT.replace('"\\\\\\\\.\\\\DISPLAY1"', "true"))),
    ("the displayPath member name is in the wrong case", with_block(GOOD_BLOCK_TEXT).replace('"displayPath"', '"displaypath"')),
    ("venue is not an object", json.dumps(dict(legacy_receipt(), venue=[1, 2]))),
    ("venue is null", json.dumps(dict(legacy_receipt(), venue=None))),
    ("the root is an array", "[" + with_block(GOOD_BLOCK_TEXT) + "]"),
    ("the text is cut short", with_block(GOOD_BLOCK_TEXT)[:-40]),
    ("the text is empty", ""),
    ("the text is not JSON", "DVE receipt"),
    ("the text nests deeper than 64", with_block(GOOD_BLOCK_TEXT, venue_extra=', "deep": ' + "[" * 70 + "]" * 70)),
]


@requires_windows_pwsh
class MalformedBlockTests(unittest.TestCase):
    """(b) any parse failure of the block types it malformed; no recovery rewrite, no fallback, never an exception."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.got = read_texts([t for _, t in MALFORMED_ROWS])

    def test_every_bad_block_is_malformed_never_a_throw(self) -> None:
        for (label, _), got in zip(MALFORMED_ROWS, self.got):
            with self.subTest(label):
                self.assertIsNone(got["threw"], f"{label}: the reader threw: {got['threw']}")
                self.assertEqual(got["state"], "malformed", f"{label}: read {got['state']} ({got['reason']})")
                self.assertIsNone(got["block"])
                self.assertTrue(got["reason"])

    def test_the_reader_has_no_second_grammar(self) -> None:
        """The reader's source: System.Text.Json, strict default options; no ConvertFrom-Json, no recovery rewrite, no comment or trailing-comma option."""
        text = MODULE.read_text(encoding="utf-8")
        m = re.search(r"^function Read-DvReceiptDisplayPath \{.*?^\}", text, re.S | re.M)
        self.assertIsNotNone(m, "Read-DvReceiptDisplayPath is defined in DualVenueRunner.psm1")
        body = m.group(0)
        self.assertIn("System.Text.Json.JsonDocument]::Parse", body)
        for forbidden in ("ConvertFrom-Json", "CommentHandling", "AllowTrailingCommas", "Get-DvJsonRootObjectWithout", "Invoke-Expression"):
            self.assertNotIn(forbidden, body, f"the reader must not use {forbidden}")


@requires_windows_pwsh
class LegacyNoBlockTests(unittest.TestCase):
    """(c) a receipt with no block reads NO_SIGNAL, and the validator reads it exactly as fork/master does."""

    def test_no_block_reads_no_signal(self) -> None:
        no_venue = legacy_receipt()
        del no_venue["venue"]
        rows = [("fork/master shape", LEGACY_TEXT), ("compressed", json.dumps(legacy_receipt())), ("no venue member", json.dumps(no_venue))]
        for (label, _), got in zip(rows, read_texts([t for _, t in rows])):
            with self.subTest(label):
                self.assertIsNone(got["threw"])
                self.assertEqual(got["state"], "NO_SIGNAL", f"{label}: {got['reason']}")
                self.assertIsNone(got["block"])
                self.assertIsNone(got["reason"])

    def test_a_new_skeleton_receipt_reads_no_signal_and_the_validator_is_unchanged(self) -> None:
        """New-DvReceipt still carries no displayPath member (the block is added only by the writer), and Test-DvReceiptValid's status of a legacy
        non-PASS/FAIL receipt is NO_SIGNAL, as on fork/master."""
        script = (
            f"Import-Module '{MODULE}' -Force\n"
            "$r = New-DvReceipt -Card 'DUAL-VENUE-EVIDENCE-1' -LegId 'unit-leg' -DeclaredVenue 'bachelor'\n"
            "$r.outcome = 'UNRESOLVED'\n"
            "$t = $r | ConvertTo-Json -Depth 12\n"
            "$read = Read-DvReceiptDisplayPath -ReceiptText $t\n"
            f"$legacy = [IO.File]::ReadAllText('{{0}}')\n"
            "$v = Test-DvReceiptValid -Receipt ($legacy | ConvertFrom-Json)\n"
            "Write-Output ('OUT=' + (ConvertTo-Json -Compress -InputObject ([ordered]@{ hasMember = $r.venue.Contains('displayPath'); state = [string]$read.state; status = [string]$v.status; valid = [bool]$v.valid })))\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "legacy.json"
            p.write_text(LEGACY_TEXT, encoding="utf-8")
            proc = run_pwsh(["-Command", script.replace("{0}", str(p))])
        line = next((l for l in proc.stdout.splitlines() if l.startswith("OUT=")), None)
        self.assertIsNotNone(line, proc.stdout + proc.stderr)
        out = json.loads(line[4:])
        self.assertFalse(out["hasMember"], "the skeleton carries no displayPath member")
        self.assertEqual(out["state"], "NO_SIGNAL")
        self.assertEqual(out["status"], "NO_SIGNAL")
        self.assertTrue(out["valid"])


def writer_round_trip(probes: list[dict]) -> list[dict]:
    """For each probe dict: ConvertTo-Json -> DVE_PROBE line -> ConvertFrom-DvProbeStdout (the runner's own probe parse) -> New-DvDisplayPathBlock ->
    a receipt written by Write-DvReceipt -> its file text -> Read-DvReceiptDisplayPath."""
    with tempfile.TemporaryDirectory() as d:
        cases = Path(d) / "probes.json"
        cases.write_text(json.dumps(probes), encoding="utf-8")
        root = Path(d) / "receipts"
        script = (
            f"Import-Module '{MODULE}' -Force\n"
            f"$doc = [System.Text.Json.JsonDocument]::Parse([IO.File]::ReadAllText('{cases}'))\n"
            "$out = @(foreach ($e in $doc.RootElement.EnumerateArray()) {\n"
            "  $probe = ConvertFrom-DvProbeStdout ('DVE_PROBE=' + $e.GetRawText())\n"
            "  $threw = $null; $block = $null\n"
            "  try { $block = New-DvDisplayPathBlock -Probe $probe } catch { $threw = [string]$_.Exception.Message }\n"
            "  $r = New-DvReceipt -Card 'DUAL-VENUE-EVIDENCE-1' -LegId 'unit-leg' -DeclaredVenue 'bachelor'\n"
            "  $r.outcome = 'UNRESOLVED'\n"
            "  if ($null -ne $block) { $r.venue['displayPath'] = $block }\n"
            f"  $path = Write-DvReceipt -Receipt $r -ReceiptRoot '{root}'\n"
            "  $read = Read-DvReceiptDisplayPath -ReceiptText ([IO.File]::ReadAllText($path))\n"
            "  [ordered]@{ threw = $threw; wrote = ($null -ne $block); state = [string]$read.state; reason = $read.reason; block = $read.block }\n"
            "})\n"
            "Write-Output ('CASES=' + (ConvertTo-Json -InputObject $out -Depth 8 -Compress))\n"
        )
        proc = run_pwsh(["-Command", script])
    for line in proc.stdout.splitlines():
        if line.startswith("CASES="):
            return json.loads(line[len("CASES="):])
    raise AssertionError(f"the writer harness printed no CASES line: rc={proc.returncode} {proc.stdout[-2000:]} {proc.stderr[-2000:]}")


@requires_windows_pwsh
class RoundTripTests(unittest.TestCase):
    """(d) one writer, one reader: what the writer writes the reader gives back, field for field."""

    def test_a_valid_block_round_trips_through_the_receipt_file(self) -> None:
        bachelor = dict(HEALTHY_PROBE, hostName="BACHELOR", gpuNames=["Intel(R) Iris(R) Xe Graphics", "NVIDIA GeForce RTX 3060 Laptop GPU"],
                        driverVersion="31.0.101.5186", displayDevice="\\\\.\\DISPLAY1")
        nulls = dict(HEALTHY_PROBE, gpuNames=[], driverVersion=None, displayDevice=None)
        one = dict(HEALTHY_PROBE)
        got = writer_round_trip([bachelor, nulls, one])
        want = [
            GOOD_BLOCK,
            {"schema": SCHEMA, "source": "health-probe", "adapters": [], "firstAdapterDriverVersion": None, "primaryDisplayDevice": None},
            {"schema": SCHEMA, "source": "health-probe", "adapters": ["NVIDIA GeForce RTX 4090"], "firstAdapterDriverVersion": "32.0.1", "primaryDisplayDevice": "\\\\.\\DISPLAY2"},
        ]
        for i, (g, w) in enumerate(zip(got, want)):
            with self.subTest(i=i):
                self.assertIsNone(g["threw"])
                self.assertTrue(g["wrote"], "the writer wrote a block")
                self.assertEqual(g["state"], "present", g["reason"])
                self.assertEqual(g["block"], w)
                self.assertEqual(list(g["block"].keys()), list(w.keys()), "the block's field order is the contract's")

    def test_a_probe_the_writer_cannot_copy_writes_no_block_and_reads_no_signal(self) -> None:
        """gpuNames missing / not a list / holding a non-string, or a non-string driver or device (a time-looking text ConvertFrom-Json made a [datetime]):
        the writer writes NO block (never a throw, never a malformed block) and the receipt reads NO_SIGNAL."""
        no_names = dict(HEALTHY_PROBE)
        del no_names["gpuNames"]
        rows = [no_names, dict(HEALTHY_PROBE, gpuNames="NVIDIA"), dict(HEALTHY_PROBE, gpuNames=["NVIDIA", 7]),
                dict(HEALTHY_PROBE, driverVersion=32), dict(HEALTHY_PROBE, displayDevice="2026-11-24T00:00:00Z")]
        for i, g in enumerate(writer_round_trip(rows)):
            with self.subTest(i=i):
                self.assertIsNone(g["threw"])
                self.assertFalse(g["wrote"])
                self.assertEqual(g["state"], "NO_SIGNAL")


@requires_windows_pwsh
class VenueLegWritesTheBlockTests(RunnerHarness, unittest.TestCase):
    """(d) Invoke-VenueLeg writes the block from the health probe it already ran; a leg refused before the probe carries none."""

    def setUp(self) -> None:
        self.make_harness()

    def test_a_health_only_leg_receipt_carries_the_block_from_the_probe(self) -> None:
        probe = dict(HEALTHY_PROBE, gpuNames=["NVIDIA GeForce RTX 4090", "Microsoft Basic Display Adapter"])
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=probe, extra=["-HealthOnly"])
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        self.assertEqual(receipt["venue"]["displayPath"],
                         {"schema": SCHEMA, "source": "health-probe", "adapters": ["NVIDIA GeForce RTX 4090", "Microsoft Basic Display Adapter"],
                          "firstAdapterDriverVersion": "32.0.1", "primaryDisplayDevice": "\\\\.\\DISPLAY2"})
        files = sorted(self.receipts.rglob("*.json"), key=lambda f: f.stat().st_mtime_ns)
        got = read_texts([files[-1].read_text(encoding="utf-8")])[0]
        self.assertEqual(got["state"], "present", got["reason"])

    def test_an_unhealthy_leg_still_carries_the_block(self) -> None:
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=dict(HEALTHY_PROBE, pwshColdStartMs=9000))
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "VENUE_UNHEALTHY")
        self.assertEqual(receipt["venue"]["displayPath"]["adapters"], ["NVIDIA GeForce RTX 4090"])


class DocsTests(unittest.TestCase):
    def test_the_doc_states_the_contract(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        for needle in ("VENUE-LEG-DISPLAY-PATH-RECEIPT-NARROW-2", "Read-DvReceiptDisplayPath", "New-DvDisplayPathBlock", SCHEMA, "NO_SIGNAL", "malformed"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
