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
# r1 adversary pass-1 A7: every harness temp dir carries an apostrophe, so a path is always quoted for a PowerShell single-quoted literal (psq)
TMP_PREFIX = "dvp-o'neil-"


PS_SINGLE_QUOTES = ("'", chr(0x2018), chr(0x2019), chr(0x201A), chr(0x201B))   # r1 pass-3 A7: PowerShell ends a '...' literal on each of these


def psq(path) -> str:
    """A path as the body of a PowerShell single-quoted literal (every quote character PowerShell accepts as a single quote doubles)."""
    text = str(path)
    for q in PS_SINGLE_QUOTES:
        text = text.replace(q, q + q)
    return text


def cases_line(proc, n: int, what: str) -> list[dict]:
    """The one CASES= line of a harness run, holding exactly one result per input (r1 pass-3 A8: an empty table must never pass a zip loop)."""
    for line in proc.stdout.splitlines():
        if line.startswith("CASES="):
            got = json.loads(line[len("CASES="):])
            got = got if isinstance(got, list) else [got]
            assert len(got) == n, f"the {what} harness returned {len(got)} results for {n} inputs"
            return got
    raise AssertionError(f"the {what} harness printed no CASES line: rc={proc.returncode} stdout={proc.stdout[-2000:]} stderr={proc.stderr[-2000:]}")

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


def read_texts(texts: list[str], reader: str = "Read-DvReceiptDisplayPath", prefix: str | None = None) -> list[dict]:
    """Run THE reader over each text in one pwsh; returns per text {state, reason, block, adapterTypes, threw}. The texts travel in a UTF-8 file read
    with System.Text.Json (never ConvertFrom-Json, so the harness itself cannot reinterpret a text)."""
    with tempfile.TemporaryDirectory(prefix=TMP_PREFIX if prefix is None else prefix) as d:
        cases = Path(d) / "cases.json"
        cases.write_text(json.dumps(texts, ensure_ascii=True), encoding="utf-8")
        script = (
            f"Import-Module '{psq(MODULE)}' -Force\n"
            f"$doc = [System.Text.Json.JsonDocument]::Parse([IO.File]::ReadAllText('{psq(cases)}'))\n"
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
    return cases_line(proc, len(texts), "reader")


def cfj_reads(texts: list[str]) -> list[dict]:
    """How ConvertFrom-Json (the grammar the reader must NOT use) reads venue.displayPath of each text: the premise of the A3 rows."""
    with tempfile.TemporaryDirectory(prefix=TMP_PREFIX) as d:
        cases = Path(d) / "cases.json"
        cases.write_text(json.dumps(texts, ensure_ascii=True), encoding="utf-8")
        script = (
            f"$doc = [System.Text.Json.JsonDocument]::Parse([IO.File]::ReadAllText('{psq(cases)}'))\n"
            "$out = @(foreach ($e in $doc.RootElement.EnumerateArray()) {\n"
            "  try { $o = $e.GetString() | ConvertFrom-Json; $b = $o.venue.displayPath\n"
            "        [ordered]@{ ok = $true; device = [string]$b.primaryDisplayDevice; adapterTypes = @(@($b.adapters) | ForEach-Object { $_.GetType().FullName }) } }\n"
            "  catch { [ordered]@{ ok = $false; device = $null; adapterTypes = @() } }\n"
            "})\n"
            "Write-Output ('CASES=' + (ConvertTo-Json -InputObject $out -Depth 5 -Compress))\n"
        )
        proc = run_pwsh(["-Command", script])
    return cases_line(proc, len(texts), "ConvertFrom-Json")


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
        # r1 adversary pass-1 A5: the premise reads the TIME text (adapters[1]): ConvertFrom-Json makes it a [datetime], the reader keeps the text
        self.assertEqual(self.cfj[1]["adapterTypes"], ["System.String", "System.DateTime"],
                         "premise: ConvertFrom-Json reads a time-looking adapter name as a [datetime]")

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

    def test_the_reader_s_helpers_have_no_second_grammar_either(self) -> None:
        """sol r2 DV-DISPLAY-PATH-ONE-GRAMMAR-GUARD-HELPERS-1: the helpers the reader calls (and the validator's marker lookup) are held to the same
        static rule as the reader body, with comments removed (a comment may NAME the cmdlet it avoids)."""
        text = MODULE.read_text(encoding="utf-8")
        for fn in ("Get-DvJsonMembersNamed", "Test-DvJsonTextReadable", "Test-DvMemberNameIs", "Get-DvObjectMembersNamed", "Test-DvReceiptCarriesDisplayPath"):
            m = re.search(rf"^function {fn} \{{.*?^\}}", text, re.S | re.M)
            with self.subTest(fn):
                self.assertIsNotNone(m, f"{fn} is defined in DualVenueRunner.psm1")
                code = re.sub(r"(?s)<#.*?#>", "", m.group(0))
                code = re.sub(r"(?m)#.*$", "", code)
                for forbidden in ("ConvertFrom-Json", "CommentHandling", "AllowTrailingCommas", "Invoke-Expression"):
                    self.assertNotIn(forbidden, code, f"{fn} must not use {forbidden}")


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
            f"Import-Module '{psq(MODULE)}' -Force\n"
            "$r = New-DvReceipt -Card 'DUAL-VENUE-EVIDENCE-1' -LegId 'unit-leg' -DeclaredVenue 'bachelor'\n"
            "$r.outcome = 'UNRESOLVED'\n"
            "$t = $r | ConvertTo-Json -Depth 12\n"
            "$read = Read-DvReceiptDisplayPath -ReceiptText $t\n"
            f"$legacy = [IO.File]::ReadAllText('{{0}}')\n"
            "$v = Test-DvReceiptValid -Receipt ($legacy | ConvertFrom-Json)\n"
            "Write-Output ('OUT=' + (ConvertTo-Json -Compress -InputObject ([ordered]@{ hasMember = $r.venue.Contains('displayPath'); state = [string]$read.state; status = [string]$v.status; valid = [bool]$v.valid })))\n"
        )
        with tempfile.TemporaryDirectory(prefix=TMP_PREFIX) as d:
            p = Path(d) / "legacy.json"
            p.write_text(LEGACY_TEXT, encoding="utf-8")
            proc = run_pwsh(["-Command", script.replace("{0}", psq(p))])
        line = next((l for l in proc.stdout.splitlines() if l.startswith("OUT=")), None)
        self.assertIsNotNone(line, proc.stdout + proc.stderr)
        out = json.loads(line[4:])
        self.assertFalse(out["hasMember"], "the skeleton carries no displayPath member")
        self.assertEqual(out["state"], "NO_SIGNAL")
        self.assertEqual(out["status"], "NO_SIGNAL")
        self.assertTrue(out["valid"])


def probe_stdout(probe) -> str:
    """The health probe job's stdout for a probe dict (one `DVE_PROBE=<json>` line, as the job prints it), or a hand-made stdout text as is."""
    return probe if isinstance(probe, str) else "pwsh 7\nDVE_PROBE=" + json.dumps(probe) + "\n"


def writer_round_trip(probes: list) -> list[dict]:
    """For each probe (a dict, or a raw stdout text): the probe job's stdout -> New-DvDisplayPathBlock -ProbeStdout (r1 pass-3 A2: the writer reads the
    probe LINE with System.Text.Json) -> a receipt written by Write-DvReceipt -> its file text -> Read-DvReceiptDisplayPath."""
    with tempfile.TemporaryDirectory(prefix=TMP_PREFIX) as d:
        cases = Path(d) / "probes.json"
        cases.write_text(json.dumps([probe_stdout(p) for p in probes], ensure_ascii=True), encoding="utf-8")
        root = Path(d) / "receipts"
        script = (
            f"Import-Module '{psq(MODULE)}' -Force\n"
            f"$doc = [System.Text.Json.JsonDocument]::Parse([IO.File]::ReadAllText('{psq(cases)}'))\n"
            "$out = @(foreach ($e in $doc.RootElement.EnumerateArray()) {\n"
            "  $stdout = $e.GetString()\n"
            "  $threw = $null; $block = $null\n"
            "  try { $block = New-DvDisplayPathBlock -ProbeStdout $stdout } catch { $threw = [string]$_.Exception.Message }\n"
            "  $r = New-DvReceipt -Card 'DUAL-VENUE-EVIDENCE-1' -LegId 'unit-leg' -DeclaredVenue 'bachelor'\n"
            "  $r.outcome = 'UNRESOLVED'\n"
            "  if ($null -ne $block) { $r.venue['displayPath'] = $block }\n"
            f"  $path = Write-DvReceipt -Receipt $r -ReceiptRoot '{psq(root)}'\n"
            "  $read = Read-DvReceiptDisplayPath -ReceiptText ([IO.File]::ReadAllText($path))\n"
            "  [ordered]@{ threw = $threw; wrote = ($null -ne $block); state = [string]$read.state; reason = $read.reason; block = $read.block }\n"
            "})\n"
            "Write-Output ('CASES=' + (ConvertTo-Json -InputObject $out -Depth 8 -Compress))\n"
        )
        proc = run_pwsh(["-Command", script])
    return cases_line(proc, len(probes), "writer")


@requires_windows_pwsh
class RoundTripTests(unittest.TestCase):
    """(d) one writer, one reader: what the writer writes the reader gives back, field for field."""

    def test_a_valid_block_round_trips_through_the_receipt_file(self) -> None:
        bachelor = dict(HEALTHY_PROBE, hostName="BACHELOR", gpuNames=["Intel(R) Iris(R) Xe Graphics", "NVIDIA GeForce RTX 3060 Laptop GPU"],
                        driverVersion="31.0.101.5186", displayDevice="\\\\.\\DISPLAY1")
        nulls = dict(HEALTHY_PROBE, gpuNames=["Microsoft Basic Display Adapter"], driverVersion=None, displayDevice=None)
        one = dict(HEALTHY_PROBE)
        # the writer reads the probe line with System.Text.Json, so a time-looking device name is copied as the text the probe printed
        timeish = dict(HEALTHY_PROBE, displayDevice="2026-11-24T00:00:00Z")
        got = writer_round_trip([bachelor, nulls, one, timeish])
        want = [
            GOOD_BLOCK,
            {"schema": SCHEMA, "source": "health-probe", "adapters": ["Microsoft Basic Display Adapter"], "firstAdapterDriverVersion": None, "primaryDisplayDevice": None},
            {"schema": SCHEMA, "source": "health-probe", "adapters": ["NVIDIA GeForce RTX 4090"], "firstAdapterDriverVersion": "32.0.1", "primaryDisplayDevice": "\\\\.\\DISPLAY2"},
            {"schema": SCHEMA, "source": "health-probe", "adapters": ["NVIDIA GeForce RTX 4090"], "firstAdapterDriverVersion": "32.0.1", "primaryDisplayDevice": "2026-11-24T00:00:00Z"},
        ]
        self.assertEqual(len(got), len(want))
        for i, (g, w) in enumerate(zip(got, want)):
            with self.subTest(i=i):
                self.assertIsNone(g["threw"])
                self.assertTrue(g["wrote"], "the writer wrote a block")
                self.assertEqual(g["state"], "present", g["reason"])
                self.assertEqual(g["block"], w)
                self.assertEqual(list(g["block"].keys()), list(w.keys()), "the block's field order is the contract's")

    def test_a_probe_the_writer_cannot_copy_writes_no_block_and_reads_no_signal(self) -> None:
        """gpuNames missing / not a list / holding a non-string / empty, or a non-string driver or device: the writer writes NO block (never a throw, never a
        malformed block) and the receipt reads NO_SIGNAL."""
        no_names = dict(HEALTHY_PROBE)
        del no_names["gpuNames"]
        rows = [no_names, dict(HEALTHY_PROBE, gpuNames="NVIDIA"), dict(HEALTHY_PROBE, gpuNames=["NVIDIA", 7]),
                dict(HEALTHY_PROBE, driverVersion=32), dict(HEALTHY_PROBE, displayDevice=True), dict(HEALTHY_PROBE, gpuNames=[])]
        got = writer_round_trip(rows)
        self.assertEqual(len(got), len(rows))
        for i, g in enumerate(got):
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

    def test_a_host_mismatched_leg_still_carries_the_block(self) -> None:
        """sol r2 VENUE-LEG-DISPLAY-PATH-HOST-MISMATCH-ROW-1: the block is attached BEFORE the host-mismatch refusal, so a refused leg records the
        display route the venue reported (a bachelor host answering for a declared ultra-magnus)."""
        wrong = dict(HEALTHY_PROBE, hostName="BACHELOR", gpuNames=["Intel(R) UHD Graphics 770"])
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=wrong)
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "VENUE_HOST_MISMATCH")
        self.assertEqual(receipt["venue"]["displayPath"]["adapters"], ["Intel(R) UHD Graphics 770"])
        files = sorted(self.receipts.rglob("*.json"), key=lambda f: f.stat().st_mtime_ns)
        got = read_texts([files[-1].read_text(encoding="utf-8")])[0]
        self.assertEqual(got["state"], "present", got["reason"])

    def test_a2_a_probe_powershell_cannot_parse_still_leaves_its_readable_sample_on_the_unhealthy_receipt(self) -> None:
        """adversary pass A2: an empty member name is legal JSON the writer reads and ConvertFrom-Json refuses, so ConvertFrom-DvProbeStdout answers $null and
        the leg refuses VENUE_UNHEALTHY; the sample the writer could read before that refusal stays on the receipt."""
        # The stub venue re-serializes the probe through ConvertTo-Json, so the odd line cannot travel through run_leg: the premise is checked on the two
        # readers, and the ORDER on the leg's source (the attachment sits before the refusal on a probe that did not parse).
        script = (
            f"Import-Module '{psq(MODULE)}' -Force\n"
            "$line = 'DVE_PROBE={\"hostName\":\"ULTRA-MAGNUS\",\"gpuNames\":[\"Intel\"],\"driverVersion\":null,\"displayDevice\":null,\"\":0}'\n"
            "$parsed = & (Get-Module DualVenueRunner) { param($s) ConvertFrom-DvProbeStdout $s } $line\n"
            "$block = New-DvDisplayPathBlock -ProbeStdout $line\n"
            "Write-Output ('CASES=' + (ConvertTo-Json -InputObject @(, [ordered]@{ powershellParses = ($null -ne $parsed); writerCopies = ($null -ne $block) }) -Compress))\n"
        )
        got = cases_line(run_pwsh(["-Command", script]), 1, "empty-member-name")[0]
        self.assertEqual(got, {"powershellParses": False, "writerCopies": True})
        text = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        attach = text.index("New-DvDisplayPathBlock -ProbeStdout")
        refusal = text.index("if ($null -eq $probe) {")
        self.assertLess(attach, refusal, "the display-path block is attached before the leg refuses a probe PowerShell could not parse")
        self.assertEqual(text.count("New-DvDisplayPathBlock -ProbeStdout"), 1)

    def test_an_unhealthy_leg_still_carries_the_block(self) -> None:
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=dict(HEALTHY_PROBE, pwshColdStartMs=9000))
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "VENUE_UNHEALTHY")
        self.assertEqual(receipt["venue"]["displayPath"]["adapters"], ["NVIDIA GeForce RTX 4090"])


def extra_field(name_json: str, value_json: str) -> str:
    return with_block(GOOD_BLOCK_TEXT[:-1] + ", " + name_json + ": " + value_json + "}")


# r1 adversary pass-1 (5609f280). A1/A2: PowerShell's -ceq / -cne / -cnotcontains compare by CULTURE, which ignores a NUL, a soft hyphen (U+00AD) or a
# combining grapheme joiner (U+034F), so an altered schema, source or field name compared equal. A3: ConvertFrom-Json turns a probe's escaped lone
# surrogate into U+FFFD, so the writer copied a text that is not the one reported.
A1_ROWS = [
    ("a NUL inside the schema", with_block(GOOD_BLOCK_TEXT.replace("mlv-app/venue", "mlv\\u0000-app/venue"))),
    ("a soft hyphen inside the schema", with_block(GOOD_BLOCK_TEXT.replace("mlv-app/venue", "mlv\\u00ad-app/venue"))),
    ("a soft hyphen inside the source", with_block(GOOD_BLOCK_TEXT.replace('"health-probe"', '"health\\u00ad-probe"'))),
    ("a NUL inside the source", with_block(GOOD_BLOCK_TEXT.replace('"health-probe"', '"health\\u0000-probe"'))),
]
A2_ROWS = [
    ("an extra sche<NUL>ma holding a lone surrogate", extra_field('"sche\\u0000ma"', '{"anything": "\\ud800"}')),
    ("an extra sou<SHY>rce", extra_field('"sou\\u00adrce"', '"x"')),
    ("an extra ada<NUL>pters", extra_field('"ada\\u0000pters"', "[]")),
    ("an extra firstAdapterDriver<SHY>Version", extra_field('"firstAdapterDriver\\u00adVersion"', "null")),
    ("an extra primaryDisplay<CGJ>Device", extra_field('"primaryDisplay\\u034fDevice"', "null")),
]
A3_READER_ROWS = [
    ("an escaped U+FFFD adapter name", with_block(GOOD_BLOCK_TEXT.replace("Intel(R) Iris(R) Xe Graphics", "Intel\\ufffd"))),
    ("a raw U+FFFD driver version", with_block(GOOD_BLOCK_TEXT.replace("31.0.101.5186", "31.0" + chr(0xFFFD)))),
    ("an escaped U+FFFD display device", with_block(GOOD_BLOCK_TEXT.replace("DISPLAY1", "DISPLAY\\ufffd"))),
]


@requires_windows_pwsh
class AdversaryPass1Tests(unittest.TestCase):
    """r1 adversary pass-1 on 5609f280: A1, A2, A3 (introduced majors) and the in-scope advisories A4, A7 (A5 is the premise row above)."""

    def test_a1_a2_a3_altered_text_reads_malformed(self) -> None:
        rows = A1_ROWS + A2_ROWS + A3_READER_ROWS
        for (label, _), got in zip(rows, read_texts([t for _, t in rows])):
            with self.subTest(label):
                self.assertIsNone(got["threw"], got["threw"])
                self.assertEqual(got["state"], "malformed", f"{label}: read {got['state']} ({got['reason']})")

    def test_a3_a_lone_surrogate_in_the_probe_writes_no_block(self) -> None:
        rows = [dict(HEALTHY_PROBE, gpuNames=["\ud800"]), dict(HEALTHY_PROBE, driverVersion="\ud800"), dict(HEALTHY_PROBE, displayDevice="\ud800"),
                dict(HEALTHY_PROBE, gpuNames=["NVIDIA " + chr(0xFFFD)])]
        for i, g in enumerate(writer_round_trip(rows)):
            with self.subTest(i=i):
                self.assertIsNone(g["threw"])
                self.assertFalse(g["wrote"], "a probe text ConvertFrom-Json could not give back as reported is never present evidence")
                self.assertEqual(g["state"], "NO_SIGNAL")

    def test_a4_the_writer_takes_no_powershell_object(self) -> None:
        """A rank-two array (pass-1 A4) can no longer reach the writer: since pass-3 A2 it takes only the probe's stdout TEXT (a JSON list is rank one)."""
        script = (
            f"Import-Module '{psq(MODULE)}' -Force\n"
            "$names = @((Get-Command New-DvDisplayPathBlock).Parameters.Keys | Where-Object { $_ -notin [System.Management.Automation.PSCmdlet]::CommonParameters })\n"
            "$t = (Get-Command New-DvDisplayPathBlock).Parameters['ProbeStdout'].ParameterType.FullName\n"
            "Write-Output ('OUT=' + ($names -join ',') + '|' + $t)\n"
        )
        proc = run_pwsh(["-Command", script])
        self.assertIn("OUT=ProbeStdout|System.String", proc.stdout, proc.stdout + proc.stderr)

    def test_a7_a_temp_path_with_an_apostrophe_still_reaches_the_reader(self) -> None:
        got = read_texts([with_block(GOOD_BLOCK_TEXT)], prefix=TMP_PREFIX)[0]
        self.assertEqual(got["state"], "present", got["reason"])


# r1 adversary pass-3 (f7a24744).
LOSSY = "Carte vid?o de base Microsoft"   # A1: a venue agent writes its result as ASCII, so a non-ASCII name arrives with a '?'
PROBE_JSON = json.dumps(HEALTHY_PROBE)
A2_PROBE_LINES = [
    ("gpuNames twice, the first null", "DVE_PROBE=" + PROBE_JSON[:-1] + ', "gpuNames": null}'),
    ("gpuNames twice, the first a list", "DVE_PROBE=" + PROBE_JSON[:-1] + ', "gpuNames": ["Other GPU"]}'),
    ("driverVersion twice", "DVE_PROBE=" + PROBE_JSON[:-1] + ', "driverVersion": "1.0"}'),
    ("displayDevice twice", "DVE_PROBE=" + PROBE_JSON[:-1] + ', "displayDevice": null}'),
    ("a case variant GpuNames beside gpuNames", "DVE_PROBE=" + PROBE_JSON[:-1] + ', "GpuNames": ["Other GPU"]}'),
    ("a singleton array root", "DVE_PROBE=[" + PROBE_JSON + "]"),
    ("a // comment in the probe line", "DVE_PROBE=" + PROBE_JSON[:-1] + " // x\n}"),
    ("no DVE_PROBE line", "pwsh 7\n"),
    ("a probe line that is not JSON", "DVE_PROBE={gpuNames}"),
]
A4_READER_ROWS = [
    ("an unreadable string outside the block, beside a good block",
     with_block(GOOD_BLOCK_TEXT).replace('"gpuNames": ["Intel(R) Iris(R) Xe Graphics"]', '"gpuNames": ["\\ud800"]')),
    ("an unreadable string in a receipt with no block", json.dumps(legacy_receipt()).replace('"actor": "unit-test"', '"actor": "\\ud800"')),
    ("an unreadable member name in a receipt with no block", json.dumps(legacy_receipt()).replace('"actor": "unit-test"', '"\\udc00": "unit-test"')),
]


@requires_windows_pwsh
class AdversaryPass3Tests(RunnerHarness, unittest.TestCase):
    """r1 adversary pass-3 on f7a24744: A1-A5 (introduced majors) and the in-scope advisories A7, A8."""

    def setUp(self) -> None:
        self.make_harness()

    def test_a1_a3_a4_reader_rows_read_malformed(self) -> None:
        rows = [
            ("a1 an adapter name that lost a character to ASCII", with_block(GOOD_BLOCK_TEXT.replace("Intel(R) Iris(R) Xe Graphics", LOSSY))),
            ("a1 a driver version with a ?", with_block(GOOD_BLOCK_TEXT.replace("31.0.101.5186", "31.0.?"))),
            ("a1 a display device with a ?", with_block(GOOD_BLOCK_TEXT.replace("DISPLAY1", "DISPLAY?"))),
            ("a3 an empty adapters list", with_block(GOOD_BLOCK_TEXT.replace('["Intel(R) Iris(R) Xe Graphics", "NVIDIA GeForce RTX 3060 Laptop GPU"]', "[]"))),
        ] + A4_READER_ROWS
        got = read_texts([t for _, t in rows])
        self.assertEqual(len(got), len(rows))
        for (label, _), g in zip(rows, got):
            with self.subTest(label):
                self.assertIsNone(g["threw"], g["threw"])
                self.assertEqual(g["state"], "malformed", f"{label}: read {g['state']} ({g['reason']})")

    def test_a1_a2_a3_a_probe_the_writer_cannot_copy_as_reported_writes_no_block(self) -> None:
        rows = [("a1 an ASCII-lossy adapter name", dict(HEALTHY_PROBE, gpuNames=[LOSSY])),
                ("a1 an ASCII-lossy driver", dict(HEALTHY_PROBE, driverVersion="32.0.?")),
                ("a3 no adapter at all (a failed controller read)", dict(HEALTHY_PROBE, gpuNames=[], driverVersion=None))] + A2_PROBE_LINES
        got = writer_round_trip([p for _, p in rows])
        self.assertEqual(len(got), len(rows))
        for (label, _), g in zip(rows, got):
            with self.subTest(label):
                self.assertIsNone(g["threw"], g["threw"])
                self.assertFalse(g["wrote"], f"{label}: the writer wrote {g['block']}")
                self.assertEqual(g["state"], "NO_SIGNAL")

    def test_a5_a_pass_receipt_lists_the_block_as_unbound(self) -> None:
        self.write_artifacts()
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec())
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertIn("displayPath", receipt["venue"])
        unbound = receipt["verification"]["unbound"]
        self.assertTrue(any(u.startswith("DISPLAY_PATH_UNBOUND") for u in unbound), unbound)

    def test_a5_the_sheet_pair_record_names_the_block_unbound(self) -> None:
        text = (DV / "New-VenueSheetPair.ps1").read_text(encoding="utf-8")
        self.assertRegex(text, r"unbound = @\([^)]*'venue\.displayPath'")

    def test_a7_a_smart_apostrophe_in_the_temp_path_still_reaches_the_reader(self) -> None:
        got = read_texts([with_block(GOOD_BLOCK_TEXT)], prefix="dvp-o" + chr(0x2019) + "neil-")[0]
        self.assertEqual(got["state"], "present", got["reason"])

    def test_a8_an_empty_result_table_fails_the_harness(self) -> None:
        class P:
            returncode = 0
            stdout = "CASES=[]\n"
            stderr = ""
        with self.assertRaises(AssertionError):
            cases_line(P, 3, "reader")


KEYS_SHADOW_SCRIPT = r"""
Import-Module '__MODULE__' -Force
$m = Get-Module DualVenueRunner
$blockJson = '{"schema":"mlv-app/venue-display-path/v2","source":"health-probe","adapters":["Intel"],"firstAdapterDriverVersion":"1.0","primaryDisplayDevice":"D1"}'
$block = [ordered]@{ schema = 'mlv-app/venue-display-path/v2'; source = 'health-probe'; adapters = @('Intel'); firstAdapterDriverVersion = '1.0'; primaryDisplayDevice = 'D1' }
function Rec($venue) { [ordered]@{ outcome = 'PASS'; card = 'C'; legId = 'L'; subject = [ordered]@{ clipId = 'x' }; venue = $venue } }
function Marker($venue) { try { @((Test-DvReceiptValid -Receipt (Rec $venue) -AllowOfflineTestMode).unbound | Where-Object { $_ -like 'DISPLAY_PATH_UNBOUND*' }).Count } catch { "threw: $($_.Exception.Message)" } }
$out = [ordered]@{}
# sol r1 B1: a venue entry named Keys must never stand in for the dictionary's key collection
$out['hashtable-block-present-keys-entry'] = Marker @{ name = 'bachelor'; displayPath = $block; Keys = @('name') }
$out['hashtable-block-absent-keys-entry'] = Marker @{ name = 'bachelor'; Keys = @('displayPath') }
$out['ordered-block-present-keys-entry'] = Marker ([ordered]@{ name = 'bachelor'; displayPath = $block; Keys = @('name') })
$out['ordered-block-absent-keys-entry'] = Marker ([ordered]@{ name = 'bachelor'; Keys = @('displayPath') })
$out['asHashtable-block-present-keys-entry'] = Marker (ConvertFrom-Json -AsHashtable ('{"name":"bachelor","displayPath":' + $blockJson + ',"Keys":["name"]}'))
$out['asHashtable-block-absent-keys-entry'] = Marker (ConvertFrom-Json -AsHashtable '{"name":"bachelor","Keys":["displayPath"]}')
$out['hashtable-case-variant-block'] = Marker @{ name = 'bachelor'; DisplayPath = $block; Keys = 'x' }
$out['hashtable-plain-present'] = Marker @{ name = 'bachelor'; displayPath = $block }
$out['hashtable-plain-absent'] = Marker @{ name = 'bachelor' }
# SIBLING_SWEEP: every other key reader over receipt / stored-playback data
$out['canonical-json'] = try { ConvertTo-DvCanonicalJson @{ Keys = 'a'; b = 1 } } catch { "threw: $($_.Exception.Message)" }
$out['get-dvkeys'] = try { [string[]]$k = @(& $m { Get-DvKeys @{ Keys = @('x'); a = 1 } }); [Array]::Sort($k, [StringComparer]::Ordinal); $k -join ',' } catch { "threw: $($_.Exception.Message)" }
$out['json-equivalent-key-sets-differ'] = try { [bool](Test-DvJsonEquivalent @{ Keys = 'a'; z = 1 } @{ Keys = 'a'; y = 1 }) } catch { "threw: $($_.Exception.Message)" }
$out['stored-playback-case-variant'] = try { @(Get-DvStoredPlaybackFieldProblems -Stored @{ Keys = 'x'; ConformRepeats = 1 } -Derived @{ conformRepeats = 1 } | Where-Object { $_ -like 'PLAYBACK_FIELD_CASE_VARIANT*' }).Count } catch { "threw: $($_.Exception.Message)" }
Write-Output ('CASES=' + (ConvertTo-Json -InputObject @(, $out) -Depth 5 -Compress))
"""


@requires_windows_pwsh
class SolR1KeysShadowTests(unittest.TestCase):
    """r2, sol r1 B1 on 9eb3de01 (DEFECT_CLASS: a dictionary's `.Keys` property read where the data may carry an entry named Keys). PowerShell resolves
    `$hashtable.Keys` to the ENTRY named Keys, so a venue (or any receipt dictionary) with such an entry made the validator omit DISPLAY_PATH_UNBOUND for a
    present block, or add it for an absent one. Every key read over receipt data goes through Get-DvDictionaryKeys (the IDictionary interface's own Keys)."""

    @classmethod
    def setUpClass(cls) -> None:
        proc = run_pwsh(["-Command", KEYS_SHADOW_SCRIPT.replace("__MODULE__", psq(MODULE))])
        cls.got = cases_line(proc, 1, "keys-shadow")[0]

    def test_b1_a_venue_entry_named_keys_never_changes_the_unbound_marker(self) -> None:
        for name, want in (("hashtable-block-present-keys-entry", 1), ("hashtable-block-absent-keys-entry", 0),
                           ("ordered-block-present-keys-entry", 1), ("ordered-block-absent-keys-entry", 0),
                           ("asHashtable-block-present-keys-entry", 1), ("asHashtable-block-absent-keys-entry", 0),
                           ("hashtable-case-variant-block", 1), ("hashtable-plain-present", 1), ("hashtable-plain-absent", 0)):
            with self.subTest(name):
                self.assertEqual(self.got[name], want, f"{name}: DISPLAY_PATH_UNBOUND count {self.got[name]!r}, expected {want}")

    def test_b1_siblings_canonical_json_reads_the_dictionary_keys_not_a_keys_entry(self) -> None:
        self.assertEqual(self.got["canonical-json"], '{"b":1,"Keys":"a"}')   # the module's invariant-culture key order: b before Keys

    def test_b1_siblings_get_dvkeys_returns_every_key(self) -> None:
        self.assertEqual(self.got["get-dvkeys"], "Keys,a")   # ordinal order

    def test_b1_siblings_json_equivalence_compares_real_key_sets(self) -> None:
        self.assertIs(self.got["json-equivalent-key-sets-differ"], False)

    def test_b1_siblings_stored_playback_case_variant_is_found_beside_a_keys_entry(self) -> None:
        self.assertEqual(self.got["stored-playback-case-variant"], 1)

    def test_h1_the_writer_validates_only_the_three_copied_fields_and_the_member_names(self) -> None:
        """sol r1 H1: docs/dual-venue-evidence.md says which probe strings the writer validates; this pins what the doc says."""
        rows = [("an unreadable hostName (not a copied field)", dict(HEALTHY_PROBE, hostName="\ud800"), True),
                ("an unreadable gpuNames element (a copied field)", dict(HEALTHY_PROBE, gpuNames=["Intel\ud800"]), False),
                ("an unreadable driverVersion (a copied field)", dict(HEALTHY_PROBE, driverVersion="31.\ud800"), False),
                ("an unreadable displayDevice (a copied field)", dict(HEALTHY_PROBE, displayDevice="\\\\.\\DISPLAY\ud800"), False),
                ("an unreadable member name at the root", dict(HEALTHY_PROBE, **{"\udc00": "x"}), False)]
        got = writer_round_trip([p for _, p, _ in rows])
        self.assertEqual(len(got), len(rows))
        for (label, _, wrote), g in zip(rows, got):
            with self.subTest(label):
                self.assertIsNone(g["threw"], g["threw"])
                self.assertEqual(g["wrote"], wrote, f"{label}: wrote={g['wrote']} block={g['block']}")
        text = " ".join(DOC.read_text(encoding="utf-8").split())   # the doc wraps lines
        self.assertIn("and no other probe string", text)
        self.assertIn("member names at the root of the line", text)
        self.assertIn("so that receipt does not read `NO_SIGNAL`", text)   # r2 adversary pass A1: the NO_SIGNAL sentence must not read as the hostName case's outcome

    def test_b1_no_dictionary_keys_property_read_is_left_in_the_receipt_key_readers(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        for fn in ("ConvertTo-DvCanonicalJson", "Get-DvStoredPlaybackFieldProblems", "Get-DvKeys", "Test-DvReceiptCarriesDisplayPath"):
            start = text.index(f"function {fn}")
            body = text[start:text.index("\nfunction ", start + 10)]
            with self.subTest(fn):
                self.assertNotRegex(body, r"\$\w+\.Keys\b", f"{fn} still reads a dictionary's .Keys property")


VENUE_LOOKUP_SCRIPT = r"""
Import-Module '__MODULE__' -Force
$blockJson = '{"schema":"mlv-app/venue-display-path/v2","source":"health-probe","adapters":["Intel"],"firstAdapterDriverVersion":null,"primaryDisplayDevice":null}'
function ToDict($v, $factory) {
    # a parsed receipt (ConvertFrom-Json -AsHashtable, case-SENSITIVE keys) copied into a dictionary kind with the factory's own key comparer
    if ($v -is [System.Collections.IDictionary]) { $d = & $factory; foreach ($k in @([System.Collections.IDictionary].GetProperty('Keys').GetValue($v))) { $d[$k] = ToDict $v[$k] $factory }; return $d }
    return , $v
}
function Marker($rec) { try { @((Test-DvReceiptValid -Receipt $rec -AllowOfflineTestMode).unbound | Where-Object { $_ -like 'DISPLAY_PATH_UNBOUND*' }).Count } catch { "threw: $($_.Exception.Message)" } }
$out = [ordered]@{}
foreach ($outcome in 'PASS', 'FAIL') {
    $spellings = 'Venue', 'venue', 'VENUE', 'vEnUe'
    for ($si = 0; $si -lt $spellings.Count; $si++) {   # the result keys carry the spelling's index: a PowerShell [ordered] table folds key case
        $spelling = $spellings[$si]
        $text = '{"outcome":"' + $outcome + '","card":"C","legId":"L","subject":{"clipId":"x"},"' + $spelling + '":{"name":"bachelor","displayPath":' + $blockJson + '}}'
        $reps = [ordered]@{
            'pscustomobject' = ($text | ConvertFrom-Json)
            'asHashtable' = (ConvertFrom-Json -AsHashtable $text)
            'hashtable' = (ToDict (ConvertFrom-Json -AsHashtable $text) { @{} })
            'ordered' = (ToDict (ConvertFrom-Json -AsHashtable $text) { [ordered]@{} })
        }
        $state = [string](Read-DvReceiptDisplayPath -ReceiptText $text).state
        foreach ($rep in $reps.Keys) { $out["$rep|$si|$outcome"] = [ordered]@{ state = $state; marker = (Marker $reps[$rep]) } }
    }
}
# a receipt that names venue in two letter cases: the reader types it malformed, and the marker is still owed when any of them carries a block
$dup = '{"outcome":"PASS","card":"C","legId":"L","subject":{"clipId":"x"},"venue":{"name":"bachelor"},"Venue":{"name":"bachelor","displayPath":' + $blockJson + '}}'
$out['dup|asHashtable'] = [ordered]@{ state = [string](Read-DvReceiptDisplayPath -ReceiptText $dup).state; marker = (Marker (ConvertFrom-Json -AsHashtable $dup)) }
# a malformed block under a case-variant venue still owes the marker (the reader says malformed, the validator only asks "is a displayPath member there")
$bad = '{"outcome":"PASS","card":"C","legId":"L","subject":{"clipId":"x"},"Venue":{"name":"bachelor","displayPath":null}}'
$out['malformed|asHashtable'] = [ordered]@{ state = [string](Read-DvReceiptDisplayPath -ReceiptText $bad).state; marker = (Marker (ConvertFrom-Json -AsHashtable $bad)) }
# controls: no block anywhere
$none = '{"outcome":"PASS","card":"C","legId":"L","subject":{"clipId":"x"},"Venue":{"name":"bachelor"}}'
$out['none|asHashtable'] = [ordered]@{ state = [string](Read-DvReceiptDisplayPath -ReceiptText $none).state; marker = (Marker (ConvertFrom-Json -AsHashtable $none)) }
$out['none|pscustomobject'] = [ordered]@{ state = [string](Read-DvReceiptDisplayPath -ReceiptText $none).state; marker = (Marker ($none | ConvertFrom-Json)) }
Write-Output ('CASES=' + (ConvertTo-Json -InputObject @(, $out) -Depth 6 -Compress))
"""


@requires_windows_pwsh
class SolR2VenueLookupTests(unittest.TestCase):
    """r3, sol r2 B1 on c3d22033 (DEFECT_CLASS: one fact read through two lookups with two case rules). The strict reader found the `venue` member in any
    letter case; the validator found it with Get-DvProp, which on a case-SENSITIVE dictionary (ConvertFrom-Json -AsHashtable) misses `Venue`, so a present
    block owed no DISPLAY_PATH_UNBOUND marker. Both now resolve the member through one name rule (Test-DvMemberNameIs, OrdinalIgnoreCase)."""

    @classmethod
    def setUpClass(cls) -> None:
        proc = run_pwsh(["-Command", VENUE_LOOKUP_SCRIPT.replace("__MODULE__", psq(MODULE))])
        cls.got = cases_line(proc, 1, "venue-lookup")[0]

    def test_b1_a_present_block_owes_the_marker_in_every_representation_and_spelling(self) -> None:
        n = 0
        for outcome in ("PASS", "FAIL"):
            for si, spelling in enumerate(("Venue", "venue", "VENUE", "vEnUe")):
                for rep in ("pscustomobject", "asHashtable", "hashtable", "ordered"):
                    key = f"{rep}|{si}|{outcome}"
                    with self.subTest(f"{rep}|{spelling}|{outcome}"):
                        n += 1
                        self.assertEqual(self.got[key]["state"], "present")
                        self.assertEqual(self.got[key]["marker"], 1, f"{key}: the reader says present, DISPLAY_PATH_UNBOUND count is {self.got[key]['marker']!r}")
        self.assertEqual(n, 32)

    def test_b1_two_letter_cases_of_venue_read_malformed_and_still_owe_the_marker(self) -> None:
        self.assertEqual(self.got["dup|asHashtable"], {"state": "malformed", "marker": 1})

    def test_b1_a_malformed_block_under_a_case_variant_venue_owes_the_marker(self) -> None:
        self.assertEqual(self.got["malformed|asHashtable"], {"state": "malformed", "marker": 1})

    def test_b1_no_block_owes_no_marker_in_any_representation(self) -> None:
        for key in ("none|asHashtable", "none|pscustomobject"):
            with self.subTest(key):
                self.assertEqual(self.got[key], {"state": "NO_SIGNAL", "marker": 0})

    def test_b1_the_reader_and_the_validator_share_one_name_rule(self) -> None:
        """Static: the JsonElement lookup and the object / dictionary lookup both decide a name match through Test-DvMemberNameIs, and neither
        compares a member name on its own."""
        text = MODULE.read_text(encoding="utf-8")
        rule = re.search(r"^function Test-DvMemberNameIs \{.*?^\}", text, re.S | re.M)
        self.assertIsNotNone(rule, "Test-DvMemberNameIs is defined in DualVenueRunner.psm1")
        self.assertIn("OrdinalIgnoreCase", rule.group(0))
        for fn in ("Get-DvJsonMembersNamed", "Get-DvObjectMembersNamed"):
            m = re.search(rf"^function {fn} \{{.*?^\}}", text, re.S | re.M)
            with self.subTest(fn):
                self.assertIsNotNone(m, f"{fn} is defined")
                self.assertIn("Test-DvMemberNameIs", m.group(0))
                self.assertNotIn("StringComparison", m.group(0), f"{fn} must not carry its own case rule")
        carries = re.search(r"^function Test-DvReceiptCarriesDisplayPath \{.*?^\}", text, re.S | re.M)
        self.assertIsNotNone(carries)
        self.assertIn("Get-DvObjectMembersNamed", carries.group(0))
        self.assertNotIn("Get-DvProp", carries.group(0), "the marker lookup must not use the case-sensitive Get-DvProp")


PROBE_PICK_SCRIPT = r"""
Import-Module '__MODULE__' -Force
$m = Get-Module DualVenueRunner
$soft = [string][char]0xAD
$real = 'DVE_PROBE={"hostName":"UM","gpuNames":["Real GPU"],"driverVersion":"1","displayDevice":"D2"}'
$fake = $soft + 'DVE_PROBE={"hostName":"UM","gpuNames":["Fake GPU"],"driverVersion":"9","displayDevice":"D9"}'
$out = [ordered]@{}
# a line that only LOOKS like the probe line (a soft hyphen before the prefix) is not the probe line: the next real one is picked
$stdout = "pwsh 7`n" + $fake + "`n" + $real + "`n"
$block = New-DvDisplayPathBlock -ProbeStdout $stdout
$out['writer'] = if ($null -eq $block) { $null } else { @($block.adapters) -join ',' }
$parsed = & $m { param($s) ConvertFrom-DvProbeStdout $s } $stdout
$out['parser'] = if ($null -eq $parsed) { $null } else { @($parsed.gpuNames) -join ',' }
# only the look-alike line: no probe line at all, so neither reads one
$only = "pwsh 7`n" + $fake + "`n"
$out['writer-only'] = [bool]($null -ne (New-DvDisplayPathBlock -ProbeStdout $only))
$out['parser-only'] = [bool]($null -ne (& $m { param($s) ConvertFrom-DvProbeStdout $s } $only))
Write-Output ('CASES=' + (ConvertTo-Json -InputObject @(, $out) -Depth 4 -Compress))
"""


@requires_windows_pwsh
class SolR2ProbeLineOrdinalPickTests(unittest.TestCase):
    """sol r2 DV-PROBE-LINE-ORDINAL-PICK-1: the writer and the existing probe parser pick the DVE_PROBE line with an ORDINAL prefix test."""

    def test_a_look_alike_line_is_not_the_probe_line_in_the_writer_or_the_parser(self) -> None:
        proc = run_pwsh(["-Command", PROBE_PICK_SCRIPT.replace("__MODULE__", psq(MODULE))])
        got = cases_line(proc, 1, "probe-pick")[0]
        self.assertEqual(got["writer"], "Real GPU")
        self.assertEqual(got["parser"], "Real GPU")
        self.assertFalse(got["writer-only"])
        self.assertFalse(got["parser-only"])


SESSION_DEFAULTS_SCRIPT = r"""
Import-Module '__MODULE__' -Force
$m = Get-Module DualVenueRunner
$probe = 'DVE_PROBE={"gpuNames":["Intel"],"driverVersion":null,"displayDevice":null}'
$out = [ordered]@{}
# the caller's session defaults, set in the scope the module's own functions are called from (dynamic scope: the callee sees them)
$out['plain-writer'] = & $m { param($p) [bool](New-DvDisplayPathBlock -ProbeStdout $p) } $probe
$out['asarray-writer'] = & $m { param($p) $PSDefaultParameterValues = @{ 'ConvertTo-Json:AsArray' = $true }; [bool](New-DvDisplayPathBlock -ProbeStdout $p) } $probe
$receiptState = {
    param($p, $defaults)
    $PSDefaultParameterValues = $defaults
    $r = New-DvReceipt -Card 'C' -LegId 'L' -DeclaredVenue 'bachelor' -Actor ([string][char]0xd800)
    $r.outcome = 'UNRESOLVED'
    $r.venue['displayPath'] = New-DvDisplayPathBlock -ProbeStdout $p
    $root = Join-Path ([IO.Path]::GetTempPath()) ('dvp-sess-' + [guid]::NewGuid().ToString('N'))
    try { [string](Read-DvReceiptDisplayPath -ReceiptText ([IO.File]::ReadAllText((Write-DvReceipt -Receipt $r -ReceiptRoot $root)))).state }
    finally { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
}
$out['plain-receipt'] = & $m $receiptState $probe @{}
$out['escape-receipt'] = & $m $receiptState $probe @{ 'ConvertTo-Json:EscapeHandling' = 'EscapeNonAscii' }
$out['asarray-receipt'] = & $m $receiptState $probe @{ 'ConvertTo-Json:AsArray' = $true }
Write-Output ('CASES=' + (ConvertTo-Json -InputObject @(, $out) -Depth 4 -Compress))
"""


@requires_windows_pwsh
class AdversaryPass5SessionDefaultsTests(unittest.TestCase):
    """r3 adversary pass A3: `$PSDefaultParameterValues` (ConvertTo-Json:AsArray / :EscapeHandling) is a preference of the session; it must never change the
    JSON text the receipt writer writes or the text the display-path writer proved readable."""

    def test_a3_session_defaults_never_change_what_the_writers_serialize(self) -> None:
        proc = run_pwsh(["-Command", SESSION_DEFAULTS_SCRIPT.replace("__MODULE__", psq(MODULE))])
        got = cases_line(proc, 1, "session-defaults")[0]
        self.assertIs(got["plain-writer"], True)
        self.assertIs(got["asarray-writer"], True, "AsArray in the session made the writer's own readability check read an array root")
        self.assertEqual(got["plain-receipt"], "present")
        self.assertEqual(got["escape-receipt"], "present", "EscapeNonAscii in the session wrote the actor's lone surrogate as an escape the reader rejects")
        self.assertEqual(got["asarray-receipt"], "present")


RECEIPT_TEXT_SCRIPT = r"""
Import-Module '__MODULE__' -Force
$B = '{"schema":"mlv-app/venue-display-path/v2","source":"health-probe","adapters":["Intel"],"firstAdapterDriverVersion":null,"primaryDisplayDevice":null}'
$head = '{"outcome":"PASS","card":"C","legId":"L","subject":{"clipId":"x"},'
function Probe($text) {
    $o = [ordered]@{ state = [string](Read-DvReceiptDisplayPath -ReceiptText $text).state }
    foreach ($rep in 'pscustomobject', 'asHashtable') {
        $rec = $(if ($rep -eq 'asHashtable') { ConvertFrom-Json -AsHashtable $text } else { $text | ConvertFrom-Json })
        foreach ($mode in 'text', 'legacy') {
            try {
                $v = $(if ($mode -eq 'text') { Test-DvReceiptValid -Receipt $rec -AllowOfflineTestMode -ReceiptText $text } else { Test-DvReceiptValid -Receipt $rec -AllowOfflineTestMode })
                $o["$rep|$mode"] = [ordered]@{ status = [string]$v.status
                    marker = @($v.unbound | Where-Object { $_ -like 'DISPLAY_PATH_UNBOUND*' }).Count
                    dup = @($v.reasons | Where-Object { $_ -like 'DISPLAY_PATH_DUPLICATE_MEMBER*' }).Count }
            } catch { $o["$rep|$mode"] = [ordered]@{ threw = $_.Exception.Message } }
        }
    }
    $o
}
$cases = [ordered]@{
    'first-venue-has-block' = $head + '"venue":{"name":"bachelor","displayPath":' + $B + '},"venue":{"name":"bachelor"}}'
    'last-venue-has-block' = $head + '"venue":{"name":"bachelor"},"venue":{"name":"bachelor","displayPath":' + $B + '}}'
    'escaped-second-venue-name' = $head + '"venue":{"name":"bachelor","displayPath":' + $B + '},"venue":{"name":"bachelor"}}'
    'two-venues-no-block' = $head + '"venue":{"name":"bachelor"},"venue":{"name":"bachelor"}}'
    'two-displaypath-members' = $head + '"venue":{"name":"bachelor","displayPath":' + $B + ',"displayPath":' + $B + '}}'
    'lone-surrogate-elsewhere-no-block' = $head + '"actor":"\ud800","venue":{"name":"bachelor"}}'
    'lone-surrogate-elsewhere-with-block' = $head + '"actor":"\ud800","venue":{"name":"bachelor","displayPath":' + $B + '}}'
    'lone-surrogate-in-a-name-no-block' = $head + '"\udc00":1,"venue":{"name":"bachelor"}}'
    'control-block' = $head + '"venue":{"name":"bachelor","displayPath":' + $B + '}}'
    'control-no-block' = $head + '"venue":{"name":"bachelor"}}'
    'comment-text-with-block' = $head + '"venue":{"name":"bachelor",/* c */"displayPath":' + $B + '}}'
}
$out = [ordered]@{}
foreach ($k in $cases.Keys) { $out[$k] = Probe $cases[$k] }
Write-Output ('CASES=' + (ConvertTo-Json -InputObject @(, $out) -Depth 6 -Compress))
"""


@requires_windows_pwsh
class ResidueR1ReceiptTextTests(unittest.TestCase):
    """R3-RESIDUE-1 r1, adversary A1 on 2fdb3e77 (DEFECT_CLASS: one receipt judged through ConvertFrom-Json while its sibling reads the text). ConvertFrom-Json
    (object and -AsHashtable) keeps only the LAST of two same-name members, so the validator never saw a first `venue` block that the System.Text.Json reader
    types malformed. Test-DvReceiptValid takes -ReceiptText, the text the receipt was parsed from; with it a duplicate `venue` or `displayPath` member fails
    validation closed (DISPLAY_PATH_DUPLICATE_MEMBER, INVALID) and the DISPLAY_PATH_UNBOUND marker is owed when ANY venue occurrence in the text carries a
    displayPath member. Without it the validator reads exactly as before."""

    @classmethod
    def setUpClass(cls) -> None:
        proc = run_pwsh(["-Command", RECEIPT_TEXT_SCRIPT.replace("__MODULE__", psq(MODULE))])
        cls.got = cases_line(proc, 1, "receipt-text")[0]

    def each_rep(self, case: str, mode: str = "text"):
        for rep in ("pscustomobject", "asHashtable"):
            yield rep, self.got[case][f"{rep}|{mode}"]

    def test_a1_a_duplicate_venue_member_fails_validation_closed_whichever_one_carries_the_block(self) -> None:
        for case in ("first-venue-has-block", "last-venue-has-block", "escaped-second-venue-name", "two-venues-no-block"):
            self.assertEqual(self.got[case]["state"], "malformed", case)
            for rep, v in self.each_rep(case):
                with self.subTest(f"{case}|{rep}"):
                    self.assertNotIn("threw", v, v)
                    self.assertEqual(v["status"], "INVALID")
                    self.assertEqual(v["dup"], 1, f"{case}|{rep}: DISPLAY_PATH_DUPLICATE_MEMBER reasons {v['dup']!r}")

    def test_a1_a_duplicate_venue_with_a_block_in_any_occurrence_owes_the_marker(self) -> None:
        for case in ("first-venue-has-block", "last-venue-has-block", "escaped-second-venue-name"):
            for rep, v in self.each_rep(case):
                with self.subTest(f"{case}|{rep}"):
                    self.assertNotIn("threw", v, v)
                    self.assertEqual(v["marker"], 1, f"{case}|{rep}: the text carries a displayPath member, DISPLAY_PATH_UNBOUND count {v['marker']!r}")

    def test_a1_two_venues_with_no_block_owe_no_marker(self) -> None:
        for rep, v in self.each_rep("two-venues-no-block"):
            with self.subTest(rep):
                self.assertNotIn("threw", v, v)
                self.assertEqual(v["marker"], 0)

    def test_a1_a_duplicate_displaypath_member_fails_validation_closed(self) -> None:
        self.assertEqual(self.got["two-displaypath-members"]["state"], "malformed")
        for rep, v in self.each_rep("two-displaypath-members"):
            with self.subTest(rep):
                self.assertNotIn("threw", v, v)
                self.assertEqual((v["status"], v["dup"], v["marker"]), ("INVALID", 1, 1))

    def test_a1_a_lone_surrogate_elsewhere_owes_no_spurious_marker_and_no_duplicate_reason(self) -> None:
        for case in ("lone-surrogate-elsewhere-no-block", "lone-surrogate-in-a-name-no-block"):
            self.assertEqual(self.got[case]["state"], "malformed", f"{case}: the reader cannot read the text back")
            for rep, v in self.each_rep(case):
                with self.subTest(f"{case}|{rep}"):
                    self.assertNotIn("threw", v, v)
                    self.assertEqual((v["marker"], v["dup"]), (0, 0), f"{case}|{rep}: {v}")

    def test_a1_a_lone_surrogate_elsewhere_does_not_hide_a_block_that_is_there(self) -> None:
        for rep, v in self.each_rep("lone-surrogate-elsewhere-with-block"):
            with self.subTest(rep):
                self.assertNotIn("threw", v, v)
                self.assertEqual((v["marker"], v["dup"]), (1, 0))

    def test_a1_controls_a_single_venue_reads_as_before(self) -> None:
        for case, marker in (("control-block", 1), ("control-no-block", 0), ("comment-text-with-block", 1)):
            for rep, v in self.each_rep(case):
                with self.subTest(f"{case}|{rep}"):
                    self.assertNotIn("threw", v, v)
                    self.assertEqual((v["marker"], v["dup"]), (marker, 0))

    def test_a1_a_caller_that_omits_the_text_keeps_todays_reading(self) -> None:
        """Legacy callers (no -ReceiptText) get no duplicate reason and the marker from the parsed object alone."""
        for case in self.got:
            if case == "comment-text-with-block":
                continue
            for rep, v in self.each_rep(case, "legacy"):
                with self.subTest(f"{case}|{rep}"):
                    self.assertNotIn("threw", v, v)
                    self.assertEqual(v["dup"], 0)
        for rep, v in self.each_rep("last-venue-has-block", "legacy"):
            self.assertEqual(v["marker"], 1, rep)   # ConvertFrom-Json kept the last venue: what a legacy caller has always seen

    def test_a1_every_validator_caller_passes_the_text_it_parsed(self) -> None:
        """SIBLING_SWEEP: the three file readers and Write-DvReceipt hand the validator the receipt text."""
        module = MODULE.read_text(encoding="utf-8")
        write = re.search(r"^function Write-DvReceipt \{.*?^\}", module, re.S | re.M).group(0)
        self.assertRegex(write, r"Test-DvReceiptValid [^\n]*-ReceiptText", "Write-DvReceipt must pass the receipt text to the validator")
        for name, count in (("Invoke-VenueBaseline.ps1", 1), ("New-VenueSheetPair.ps1", 1), ("New-VenueFlavorPair.ps1", 1)):
            text = (DV / name).read_text(encoding="utf-8")
            calls = re.findall(r"Test-DvReceiptValid -Receipt [^\n]*", text)
            with self.subTest(name):
                self.assertEqual(len(calls), count, calls)
                for call in calls:
                    self.assertIn("-ReceiptText", call, f"{name}: {call}")

    def test_a1_the_validator_names_the_marker_rule_in_one_place(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        carries = re.search(r"^function Test-DvReceiptCarriesDisplayPath \{.*?^\}", module, re.S | re.M).group(0)
        self.assertIn("-ReceiptText", carries)
        self.assertIn("Get-DvReceiptTextDisplayPathFacts", carries)
        facts = re.search(r"^function Get-DvReceiptTextDisplayPathFacts \{.*?^\}", module, re.S | re.M)
        self.assertIsNotNone(facts, "Get-DvReceiptTextDisplayPathFacts is defined in DualVenueRunner.psm1")
        self.assertIn("Test-DvMemberNameIs", facts.group(0))
        self.assertNotIn("StringComparison", facts.group(0), "the facts reader must not carry its own case rule")


class DocsTests(unittest.TestCase):
    def test_the_doc_does_not_attribute_the_historical_speed_change_to_an_adapter(self) -> None:
        """sol r2 DV-DISPLAY-PATH-MOTIVATION-ATTRIBUTION-1: the source investigation left the cause open."""
        text = " ".join(DOC.read_text(encoding="utf-8").split())
        self.assertNotIn("because the venue drove its display through a different adapter", text)
        self.assertIn("left the cause open", text)
        self.assertIn("this block does not say an adapter caused it", text)

    def test_the_doc_states_the_contract(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        for needle in ("VENUE-LEG-DISPLAY-PATH-RECEIPT-NARROW-2", "Read-DvReceiptDisplayPath", "New-DvDisplayPathBlock", SCHEMA, "NO_SIGNAL", "malformed"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
