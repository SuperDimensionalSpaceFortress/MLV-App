"""EXPORT-DEBAYER-QUALITY-1: the twelve ProRes debayer quality legs, their receipts, export.quality, the job's untimed quality pass and the
receipt's export.quality block.

Pinned:
  * all 12 legs validate against leg-spec.schema.json and Get-DvLegSpecShapeRefusal; the leg sets plan six distinct cells per clip;
  * each variant receipt differs from its base receipt in exactly the <debayer> line, with the expected value; AMaZE reuses the base receipt; every
    receiptSha256 matches the committed bytes; the clip classes agree with KNOWN_CLIP_DUAL_ISO_CLASS;
  * export.quality is refused on cdng-lossless and with more than 64 capture frames (schema, shape refusal and generator);
  * the job generator embeds debayer_quality.py with a matching sha256, and the TIMED run never sets MLVAPP_PIPELINE_CAPTURE_* (from the job text);
  * Get-DvExportQualityEvidence (the one reader of the writer and the validator): PASS, VENUE_TOOLING, capture absent, captures not deleted,
    hash and debayer mismatches;
  * offline end to end (Invoke-VenueLeg.ps1 -OfflineTestMode, stub generator and um-run): a quality receipt verifies, a tampered quality.json or a
    pass that left captures behind does not.
No test launches MLVApp and no test opens a clip (owner rule 2026-10-11).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_dual_venue_evidence import DV, ROOT, RunnerHarness, requires_windows_pwsh, run_pwsh, run_pwsh_json
from tools.repo_hygiene.test_dual_venue_export_leg import KNOWN_CLIP_DUAL_ISO_CLASS, batch_log, output_manifest, stage_log

CARD = "EXPORT-DEBAYER-QUALITY-1"
DEBAYER_VALUES = {"bilinear": 2, "amaze": 5, "rcd": 7, "dcb": 8, "lmmse": 3, "igv": 4}   # platform/qt/ReceiptSettings.h:16
CLIPS = {"M16-1243": ("m16-1243", "dualiso", 20), "M16-1456": ("m16-1456", "nodualiso", 10)}
QUALITY = {"captureOffsets": [3, 7, 11, 15, 19, 23], "stages": ["S2_post_dualiso", "S3_debayer", "S4_processing"], "tileSize": 64, "tilesPerClass": 16}
EXPORT_JOB = ROOT / "tools" / "profiling" / "bachelor" / "export-stage-job.ps1"
LEGS = [f"{slug}-export-prores-debayer-{d}" for slug, _f, _p in CLIPS.values() for d in DEBAYER_VALUES]


def leg(name: str) -> dict:
    return json.loads((DV / "legs" / f"{name}.json").read_text(encoding="utf-8"))


def committed(rel: str) -> bytes:
    """The committed bytes the runner reads (an autocrlf checkout may hold CRLF)."""
    return (DV / rel).read_bytes().replace(b"\r\n", b"\n")


class DebayerLegSpecTests(unittest.TestCase):
    def test_the_twelve_legs_have_the_card_shape(self) -> None:
        self.assertEqual(len(LEGS), 12)
        for name in LEGS:
            with self.subTest(leg=name):
                s = leg(name)
                clip = s["clipId"]
                slug, fam, play = CLIPS[clip]
                d = name.rsplit("-", 1)[1]
                self.assertEqual((s["legId"], s["card"], s["legType"], s["backends"], s["scaleFactor"], s["playSeconds"]), (name, CARD, "export", ["cpu"], 1, play))
                ex = s["export"]
                self.assertEqual((ex["format"], ex["maxFrames"], ex["dualIsoExpected"]), ("prores", 120, True))
                self.assertIs(ex["dualIsoExpected"], KNOWN_CLIP_DUAL_ISO_CLASS[clip], "the declared class is the clip's measured class")
                want_receipt = f"look-receipts/export-owner-hq-{fam}.marxml" if d == "amaze" else f"look-receipts/export-owner-hq-{fam}-debayer-{d}.marxml"
                self.assertEqual(ex["receipt"], want_receipt)
                self.assertEqual(hashlib.sha256(committed(ex["receipt"])).hexdigest(), ex["receiptSha256"])
                self.assertEqual(ex["quality"], QUALITY)
                crit = [{"metric": "framesExported", "op": "eq", "value": 120}, {"metric": "exportFramesFailed", "op": "eq", "value": 0},
                        {"metric": "stageFramesSeen", "op": "eq", "value": 120}]
                self.assertEqual(s["criteria"], {"acceptance": {"cpu": crit}, "supplementary": {"cpu": crit}})
                self.assertEqual(s["generatorArgs"], {"cpuQuiescenceThresholdPercent": 20})

    def test_each_variant_receipt_differs_from_its_base_in_exactly_the_debayer_line(self) -> None:
        for fam in ("dualiso", "nodualiso"):
            base = committed(f"look-receipts/export-owner-hq-{fam}.marxml").decode("utf-8").split("\n")
            self.assertEqual([l.strip() for l in base if l.strip().startswith("<debayer>")], ["<debayer>5</debayer>"], "the owner receipt is AMaZE (5)")
            for d, v in DEBAYER_VALUES.items():
                if d == "amaze":
                    continue
                with self.subTest(family=fam, debayer=d):
                    var = committed(f"look-receipts/export-owner-hq-{fam}-debayer-{d}.marxml").decode("utf-8").split("\n")
                    self.assertEqual(len(var), len(base))
                    diff = [(a.strip(), b.strip()) for a, b in zip(base, var) if a != b]
                    self.assertEqual(diff, [("<debayer>5</debayer>", f"<debayer>{v}</debayer>")])

    def test_the_base_receipts_keep_their_hashes(self) -> None:
        # the shipped #838 legs bind them; this card must not move them
        for name in ("m16-1243-export-prores", "m16-1456-export-prores"):
            ex = leg(name)["export"]
            self.assertEqual(hashlib.sha256(committed(ex["receipt"])).hexdigest(), ex["receiptSha256"])
            self.assertNotIn("quality", ex)

    def test_the_schema_admits_the_legs_and_refuses_bad_quality_shapes(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))
        for name in LEGS:
            jsonschema.validate(leg(name), schema)
        base = leg(LEGS[0])
        for label, mutate in (("cdng", lambda s: s["export"].__setitem__("format", "cdng-lossless")),
                              ("65 frames", lambda s: s["export"]["quality"].__setitem__("captureOffsets", list(range(65)))),
                              ("offset 24", lambda s: s["export"]["quality"].__setitem__("captureOffsets", [24])),
                              ("no S4", lambda s: s["export"]["quality"].__setitem__("stages", ["S2_post_dualiso", "S3_debayer"])),
                              ("odd tile", lambda s: s["export"]["quality"].__setitem__("tileSize", 63)),
                              ("extra key", lambda s: s["export"]["quality"].__setitem__("label", "x"))):
            with self.subTest(case=label):
                s = json.loads(json.dumps(base))
                mutate(s)
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(s, schema)


@requires_windows_pwsh
class DebayerLegRunnerShapeTests(unittest.TestCase):
    def test_the_shape_refusal_admits_the_legs_and_refuses_bad_quality_typed(self) -> None:
        base = leg(LEGS[0])

        def mut(f):
            s = json.loads(json.dumps(base))
            f(s)
            return s
        cases = [("ok-" + n, leg(n), None) for n in LEGS] + [
            ("cdng", mut(lambda s: s["export"].__setitem__("format", "cdng-lossless")), "prores export only"),
            ("65", mut(lambda s: s["export"]["quality"].__setitem__("captureOffsets", list(range(65)))), "more than 64 capture frames"),
            ("offset-24", mut(lambda s: s["export"]["quality"].__setitem__("captureOffsets", [24])), "from 0 to 23"),
            ("repeat", mut(lambda s: s["export"]["quality"].__setitem__("captureOffsets", [3, 3])), "must not repeat"),
            ("no-s2", mut(lambda s: s["export"]["quality"].__setitem__("stages", ["S3_debayer", "S4_processing"])), "must include S2_post_dualiso"),
            ("bad-stage", mut(lambda s: s["export"]["quality"].__setitem__("stages", ["S2_post_dualiso", "S4_processing", "S5_processed8"])), "entries must be among"),
            ("odd-tile", mut(lambda s: s["export"]["quality"].__setitem__("tileSize", 63)), "tileSize"),
            ("extra", mut(lambda s: s["export"]["quality"].__setitem__("label", "x")), "export.quality.label"),
            ("missing", mut(lambda s: s["export"]["quality"].pop("tilesPerClass")), "needs tilesPerClass"),
            ("array", mut(lambda s: s["export"].__setitem__("quality", [QUALITY])), "single JSON object"),
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.json"
            path.write_text(json.dumps([{"name": n, "spec": s} for n, s, _ in cases]), encoding="utf-8")
            proc, got = run_pwsh_json("$cases = Get-Content -Raw -LiteralPath $env:C -Encoding utf8 | ConvertFrom-Json\n"
                                      "@($cases | ForEach-Object { [ordered]@{ name = $_.name; r = (Get-DvLegSpecShapeRefusal -Spec $_.spec) } }) | ConvertTo-Json -Compress -AsArray", C=str(path))
        self.assertIsNotNone(got, proc.stdout + proc.stderr)
        for (name, _s, want), row in zip(cases, got):
            with self.subTest(case=name):
                if want is None:
                    self.assertIsNone(row["r"], row)
                else:
                    self.assertIsNotNone(row["r"], name)
                    self.assertIn(want, row["r"])

    def test_each_leg_set_plans_six_distinct_cells(self) -> None:
        for slug in ("m16-1243", "m16-1456"):
            proc, got = run_pwsh_json(f"@((Get-DvLegSetPlan -LegSetPath '{DV / 'legsets' / f'debayer-quality-{slug}.json'}' -Repeats 1).plan | ForEach-Object {{ $_.cellId }}) | ConvertTo-Json -Compress -AsArray")
            self.assertIsNotNone(got, proc.stdout + proc.stderr)
            self.assertEqual(len(got), 6)
            self.assertEqual(len(set(got)), 6, got)
            self.assertTrue(all(c.startswith("cpu-export-prores-") for c in got), got)
        # the shipped export legs keep their cell ids
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "legsets").mkdir()
            (root / "legs").mkdir()
            for n in ("m16-1243-export-cdng-lossless", "m16-1243-export-prores"):
                (root / "legs" / f"{n}.json").write_text(json.dumps(leg(n)), encoding="utf-8")
            (root / "legsets" / "x.json").write_text(json.dumps({"schema": "mlv-app/dual-venue-legset/v1", "legSet": "xx", "card": "EXPORT-VENUE-LEG-1", "clipId": "M16-1243",
                                                                  "legs": ["legs/m16-1243-export-cdng-lossless.json", "legs/m16-1243-export-prores.json"]}), encoding="utf-8")
            proc, got = run_pwsh_json(f"@((Get-DvLegSetPlan -LegSetPath '{root / 'legsets' / 'x.json'}' -Repeats 1).plan | ForEach-Object {{ $_.cellId }}) | ConvertTo-Json -Compress -AsArray")
        self.assertEqual(got, ["cpu-export-cdng-lossless", "cpu-export-prores"], proc.stdout + proc.stderr)


@requires_windows_pwsh
class DebayerQualityJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        proc = run_pwsh(["-Command", f"$d = & '{EXPORT_JOB}' -DescribeTemplate; $d | ConvertTo-Json -Depth 4 -Compress"])
        if proc.returncode != 0:
            raise AssertionError(proc.stdout + proc.stderr)
        cls.d = json.loads(proc.stdout)
        cls.template = cls.d["template"]

    def test_the_generator_embeds_the_metric_script_with_a_matching_sha256(self) -> None:
        want = hashlib.sha256((DV / "debayer_quality.py").read_bytes()).hexdigest()
        self.assertEqual(self.d["qualityScriptSha256"], want)
        self.assertTrue(self.d["pythonResolverEmbedded"])
        for token in ("__QUALITY_JSON__", "__QUALITY_SCRIPT_BASE64__", "__QUALITY_SCRIPT_SHA256__", "__PYTHON_RESOLVER__"):
            self.assertIn(token, self.template, token)
        src = EXPORT_JOB.read_text(encoding="utf-8")
        self.assertIn("QUALITY_SCRIPT_BASE64 = $(if ($null -ne $qualityObject) { [Convert]::ToBase64String($qualityScriptBytes) }", src)
        self.assertIn("QUALITY_SCRIPT_SHA256 = $(if ($null -ne $qualityObject) { $qualityScriptSha256 }", src)
        # the job verifies the decoded bytes against the baked sha256 before it writes them, and again on disk before it runs them
        body = self.template[self.template.index("function Invoke-ExportQualityPass"):]
        verify = body.index("(Get-QualityBytesSha256 $scriptBytes) -cne $QualityScriptSha256")
        write = body.index("Publish-AttrCudaBytes -Path $scriptLocal")
        reverify = body.index("(Get-QualityBytesSha256 ([IO.File]::ReadAllBytes($scriptLocal))) -cne $QualityScriptSha256")
        run = body.index("$mApp = [Diagnostics.Process]::Start($mpsi)")
        self.assertLess(verify, write)
        self.assertLess(write, reverify)
        self.assertLess(reverify, run)
        self.assertIn("'-I', $scriptLocal", body, "the metric runs isolated (no script-dir or cwd imports)")

    def test_the_timed_run_never_sets_the_capture_variables(self) -> None:
        t = self.template
        sets = [m.start() for m in re.finditer(r"Environment\['MLVAPP_PIPELINE_CAPTURE_", t)]
        self.assertEqual(len(sets), 3)
        start = t.index("function Invoke-ExportQualityPass")
        end = t.index("\ntry {", start)
        self.assertTrue(all(start < s < end for s in sets), "every capture variable is set inside the quality pass")
        for line in t.splitlines():
            if "MLVAPP_PIPELINE_CAPTURE_" in line and "Environment[" in line:
                self.assertTrue(line.strip().startswith("$qpsi.Environment["), line)
        timed = t[t.index("# --- the export: MLVApp.exe --batch"):t.index("# --- classify from the app's own records")]
        self.assertNotIn("PIPELINE_CAPTURE", timed)
        self.assertIn("MLVAPP_STAGE_TIMING", timed)
        # the quality pass starts only after the timed run is classified and its output deleted
        self.assertLess(t.index("$manifest.deleted = $(try { Remove-ExportOutput }"), t.index("Invoke-ExportQualityPass -Record $qualityRecord"))
        self.assertIn("'--max-frames', [string]$QualityRunFrames", t)
        self.assertIn("$QualityRunFrames = 24", t)

    def test_the_quality_pass_derives_frames_from_the_cut_in_and_deletes_its_captures_by_proof(self) -> None:
        t = self.template
        self.assertIn("[int64]$_ + $cutIn - 1", t)
        self.assertIn("if ($cutIn -eq 0) { $cutIn = 1 }", t)
        self.assertIn("New-AttrCudaOwnedRoot -TrustedRoot '__SCRATCH_ROOT__' -Path (Join-Path $Work 'quality-capture')", t)
        self.assertIn("Remove-AttrCudaTree -TrustedRoot '__SCRATCH_ROOT__' -Path $d -OwnedJournal $OwnerJournal", t)
        self.assertIn("$qualityRecord.captureDeleted = $(try { Remove-QualityCapture }", t)
        self.assertIn("try { [void](Remove-QualityCapture) }", t, "the finally block also sweeps the quality dirs")
        self.assertIn("'EXPORT_QUALITY_TOOLING_ABSENT'", t)
        self.assertNotIn("Publish-AttrCudaFileCopy -Source $script:qualityCaptureDir", t)

    def test_the_generator_refuses_quality_on_cdng_and_more_than_64_frames(self) -> None:
        for fmt, q, want in (("cdng-lossless", QUALITY, "prores export only"),
                             ("prores", dict(QUALITY, captureOffsets=list(range(65))), "more than 64 capture frames"),
                             ("prores", dict(QUALITY, captureOffsets=[30]), "0..23")):
            with self.subTest(fmt=fmt, n=len(q["captureOffsets"])):
                with tempfile.TemporaryDirectory() as d:
                    qf = Path(d) / "q.json"
                    qf.write_text(json.dumps(q), encoding="utf-8")
                    proc = run_pwsh(["-Command", f"try {{ & '{EXPORT_JOB}' -ClipId M16-1243 -Format {fmt} -MaxFrames 120 -QualityJson (Get-Content -Raw '{qf}') -ErrorAction Stop; 'NOT REFUSED' }} catch {{ 'REFUSED ' + $_.Exception.Message }}"])
                self.assertIn("REFUSED DUAL_VENUE_EXPORT_QUALITY_INVALID", proc.stdout, proc.stdout + proc.stderr)
                self.assertIn(want, proc.stdout)


def quality_doc(debayer: int = 5, frames=(3, 7, 11, 15, 19, 23)) -> dict:
    names = ["none", "simple", "bilinear", "lmmse", "igv", "amaze", "ahd", "rcd", "dcb"]
    mset = {"ncge": 1.5, "sigmaY": 2.0, "sigmaC1": 1.0, "sigmaC2": 1.0, "sigmaC": 1.0, "fc": 0.1}
    return {"schema": "mlv-app/debayer-quality/v1", "card": CARD, "result": "QUALITY_CAPTURED", "receiptDebayer": debayer, "debayer": names[debayer],
            "captureOffsets": QUALITY["captureOffsets"], "frames": list(frames), "stages": QUALITY["stages"], "tileSize": 64, "tilesPerClass": 16, "border": 32,
            "isoValue": 1600, "levelSource": "rawi", "cfaSource": "rawi",
            "perFrame": [{"frame": f, "sha256": {s: hashlib.sha256(f"{s}{f}{debayer}".encode()).hexdigest() for s in QUALITY["stages"]},
                          "flatTiles": list(range(16)), "edgeTiles": list(range(16, 32)), "metrics": {"S3_debayer": mset, "S4_processing": mset}} for f in frames],
            "metrics": {"S3_debayer": mset, "S4_processing": mset}}


def summary_quality(qtext: str | None, result: str = "QUALITY_CAPTURED", deleted=True, debayer: int = 5) -> dict:
    return {"requested": True, "result": result, "receiptDebayer": debayer, "captureOffsets": QUALITY["captureOffsets"], "frames": [3, 7, 11, 15, 19, 23],
            "runFrames": 24, "appExitCode": 0, "wallSeconds": 9.0, "pythonPresent": result != "EXPORT_QUALITY_TOOLING_ABSENT", "scriptSha256": "a" * 64,
            "qualitySha256": hashlib.sha256(qtext.encode("utf-8")).hexdigest() if qtext else None, "captureDeleted": deleted, "detail": None}


@requires_windows_pwsh
class QualityEvidenceReaderTests(unittest.TestCase):
    def evidence(self, cases: list[dict]) -> list[dict]:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.json"
            path.write_text(json.dumps(cases), encoding="utf-8")
            proc, got = run_pwsh_json("$cases = Get-Content -Raw -LiteralPath $env:C -Encoding utf8 | ConvertFrom-Json\n"
                                      "@($cases | ForEach-Object { Get-DvExportQualityEvidence -SpecQuality $_.spec -Summary $_.summary -QualityText ([string]$_.text) "
                                      "-QualitySha256 ([string]$_.sha) -TimedResult $_.timed }) | ConvertTo-Json -Compress -Depth 6 -AsArray", C=str(path))
        self.assertIsNotNone(got, proc.stdout + proc.stderr)
        return got

    def case(self, text: str | None, summary: dict | None, sha: str | None = None, timed: str = "EXPORT_CAPTURED") -> dict:
        return {"spec": QUALITY, "summary": {"result": timed} if summary is None else {"result": timed, "quality": summary}, "text": text or "",
                "sha": sha if sha is not None else (hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""), "timed": timed}

    def test_the_reader_types_every_quality_outcome(self) -> None:
        good = json.dumps(quality_doc())
        wrong_debayer = json.dumps(quality_doc(debayer=7))
        bad_shape = json.dumps(dict(quality_doc(), extra=1))
        rows = self.evidence([
            self.case(good, summary_quality(good)),
            self.case(None, summary_quality(None, "EXPORT_QUALITY_TOOLING_ABSENT")),
            self.case(None, summary_quality(None, "EXPORT_QUALITY_CAPTURE_ABSENT")),
            self.case(good, summary_quality(good, deleted=False)),
            self.case(good, summary_quality(good), sha="0" * 64),
            self.case(wrong_debayer, summary_quality(wrong_debayer, debayer=5)),
            self.case(bad_shape, summary_quality(bad_shape)),
            self.case(None, summary_quality(None)),
            self.case(None, None),
            self.case(None, None, timed="EXPORT_TIMEOUT"),
        ])
        want = [("PASS", None, "QUALITY_CAPTURED"), ("VENUE_TOOLING", None, "EXPORT_QUALITY_TOOLING_ABSENT"), ("INVALID", None, "EXPORT_QUALITY_CAPTURE_ABSENT"),
                ("PASS", "EXPORT_CAPTURE_NOT_DELETED", "QUALITY_CAPTURED"), ("INVALID", None, "EXPORT_QUALITY_HASH_MISMATCH"),
                ("INVALID", None, "EXPORT_QUALITY_DEBAYER_MISMATCH"), ("INVALID", None, "EXPORT_QUALITY_SCHEMA_INVALID"), ("INVALID", None, "EXPORT_QUALITY_ABSENT"),
                ("INVALID", None, "EXPORT_QUALITY_RECORD_ABSENT"), ("SKIPPED", None, "EXPORT_QUALITY_RECORD_ABSENT")]
        for i, (row, (verdict, leg_invalid, needle)) in enumerate(zip(rows, want)):
            with self.subTest(case=i):
                self.assertEqual(row["verdict"], verdict, row)
                if leg_invalid is None:
                    self.assertIsNone(row["legInvalid"], row)
                else:
                    self.assertIn(leg_invalid, row["legInvalid"])
                self.assertIn(needle, row["detail"], row)
        self.assertEqual((rows[0]["debayer"], rows[0]["isoValue"]), ("amaze", 1600))


EXPORT_QUALITY_STUB_GENERATOR = r"""
param($SourceCommit,$BuildManifestSha256,$ClipId,$OutFile,$RepoRoot,$Venue,$Backend,$CpuQuiescenceThresholdPercent,$VenueTablePath,$PlaySeconds,$Format,$MaxFrames,$ReceiptPath,$DualIsoExpected,$ExpectReceiptSha256,$QualityJson)
$cfg = Get-Content -LiteralPath $env:DVE_STUB -Raw | ConvertFrom-Json
Add-Content -LiteralPath $cfg.genLog -Value (($PSBoundParameters.Keys | Sort-Object | ForEach-Object { $_ + '=' + $PSBoundParameters[$_] }) -join ';')
Set-Content -LiteralPath $OutFile -Value "# stub export job"
[pscustomobject]@{ outFile = $OutFile; recommendedJobTimeoutSec = 900; clipContentSha256 = $cfg.clipContentSha256; playSeconds = $PlaySeconds; fixtureRehearsal = $false; legType = 'export' }
"""


@requires_windows_pwsh
class QualityReceiptOfflineTests(RunnerHarness, unittest.TestCase):
    """The AMaZE leg (its receipt is the committed owner receipt) through Invoke-VenueLeg.ps1 -OfflineTestMode with 30 frames."""

    def setUp(self) -> None:
        self.make_harness()
        self.gen.write_text(EXPORT_QUALITY_STUB_GENERATOR, encoding="utf-8")
        s = leg("m16-1243-export-prores-debayer-amaze")
        s["export"]["maxFrames"] = 30
        for role in ("acceptance", "supplementary"):
            for c in s["criteria"][role]["cpu"]:
                if c["metric"] in ("framesExported", "stageFramesSeen"):
                    c["value"] = 30
        self.spec_obj = s
        self.spec = self.tmp / "quality-leg.json"
        self.spec.write_text(json.dumps(s), encoding="utf-8")

    def write(self, quality: dict | None, deleted=True, result: str = "QUALITY_CAPTURED") -> None:
        import shutil
        a = self.artifacts
        if a.exists():
            shutil.rmtree(a)
        a.mkdir(parents=True)
        qtext = json.dumps(quality) if quality is not None else None
        summary = {"schema": "export-stage-job.v1", "result": "EXPORT_CAPTURED", "legType": "export", "format": "prores", "maxFrames": 30, "dualIsoExpected": True,
                   "clipId": "M16-1243", "backend": "cpu", "declaredVenue": "bachelor", "venue": "bachelor", "sourceCommit": self.sha, "buildManifestSha256": self.build_sha,
                   "receiptSha256": self.spec_obj["export"]["receiptSha256"], "exportWallSeconds": 30.0, "appExitCode": 0, "timedOut": False, "fixtureRehearsal": False,
                   "quality": summary_quality(qtext, result=result, deleted=deleted)}
        (a / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        (a / "batch.log").write_text(batch_log(30, verified=30), encoding="utf-8")
        (a / "stage-timing.log").write_text(stage_log(30), encoding="utf-8")
        (a / "output-manifest.json").write_text(output_manifest(1, "mov"), encoding="utf-8")
        if qtext is not None:
            (a / "quality.json").write_bytes(qtext.encode("utf-8"))

    def validate(self, receipt: dict, evidence_dir: str = "") -> dict:
        rp = self.tmp / "receipt-under-test.json"
        rp.write_text(json.dumps(receipt), encoding="utf-8")
        ev = f" -EvidenceDir '{evidence_dir}'" if evidence_dir else ""
        proc, got = run_pwsh_json(f"Import-Module '{DV / 'DualVenueRunner.psm1'}' -Force\n"
                                  f"$r = Get-Content -Raw '{rp}' | ConvertFrom-Json\n"
                                  f"$v = Test-DvReceiptValid -Receipt $r -AllowOfflineTestMode -OfflineLegSpecBytes ([IO.File]::ReadAllBytes('{self.spec}')){ev}\n"
                                  "[ordered]@{ valid = $v.valid; status = $v.status; reasons = @($v.reasons) } | ConvertTo-Json -Compress")
        self.assertIsNotNone(got, proc.stdout + proc.stderr)
        return got

    def test_a_quality_receipt_verifies_and_carries_the_quality_block(self) -> None:
        self.write(quality_doc(frames=(3, 7, 11, 15, 19, 23)))
        proc, receipt, _ = self.run_leg("bachelor", self.spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        q = receipt["export"]["quality"]
        self.assertEqual((q["verdict"], q["debayer"], q["isoValue"], q["receiptDebayer"]), ("PASS", "amaze", 1600, 5))
        self.assertIn("quality.json", receipt["export"]["files"])
        self.assertTrue(any("QualityJson=" in c for c in self.generator_calls()), self.generator_calls())
        self.assertEqual(self.validate(receipt)["status"], "VERIFIED_OFFLINE_TEST")
        # tampering with the published quality.json is caught by the re-hash
        evq = Path(receipt["evidence"]["localEvidenceDir"]) / "export" / "quality.json"
        evq.write_text(json.dumps(quality_doc(debayer=7)), encoding="utf-8")
        self.assertEqual(self.validate(receipt)["status"], "INVALID")

    def test_captures_left_on_the_venue_make_the_leg_invalid(self) -> None:
        self.write(quality_doc(), deleted=False)
        proc, receipt, _ = self.run_leg("bachelor", self.spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(receipt["outcome"], "INVALID")
        self.assertIn("EXPORT_CAPTURE_NOT_DELETED", receipt["outcomeDetail"])

    def test_a_venue_without_python_keeps_the_timed_outcome_and_types_the_quality_verdict(self) -> None:
        self.write(None, result="EXPORT_QUALITY_TOOLING_ABSENT")
        proc, receipt, _ = self.run_leg("bachelor", self.spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual(receipt["export"]["quality"]["verdict"], "VENUE_TOOLING")
        self.assertEqual(self.validate(receipt)["status"], "VERIFIED_OFFLINE_TEST")


if __name__ == "__main__":
    unittest.main()
