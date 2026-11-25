"""VENUE-LEG-DISPLAY-PATH-RECEIPT-1: every venue leg receipt carries `venue.displayPath`, the display path the venue drove at leg time.

WHY. The m16-1457-native-fullscreen-s1 CUDA control on Bachelor went from 59.56 fps (display p50 16.67 ms) to 38.78 fps (p50 33.22 ms) with a byte-identical exe,
clip, spec, launch arguments and environment (BACHELOR-1457-NATIVE-GUI-LATE-ENV-DIFF-1 r1). The difference is venue state: Bachelor drives its only display through
the Intel Iris Xe at 3840x2400 / 59 Hz with the NVIDIA GPU headless, and Windows Update rebooted it. No receipt recorded any of that (`venue.driverVersion` is the first
video controller's driver only).

WHAT IS PINNED
  * the venue-side collector (Get-DvDisplayPathCollectorSource -> Get-DvDisplayPath) is READ-ONLY (CIM reads and one registry read, no write verb), bounded to 15 s for
    the whole block, and a failed / timed-out / denied probe is a typed `{"state":"unknown","reason":...}` for THAT field, never a throw;
  * the Bachelor shape reads right: the headless NVIDIA adapter has no active output, the Intel adapter is the one active display at 59 Hz;
  * the validator (Test-DvReceiptValid) reports `displayPath` = absent | present | malformed. A receipt without the block is still valid (older receipts exist); a
    present but malformed block is INVALID; a block whose every probe failed is valid and present;
  * Invoke-VenueLeg copies the probe's block into the receipt through ConvertTo-DvDisplayPath, which never lets a bad block change the leg's verdict.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_dual_venue_evidence import (
    DV, HEALTHY_PROBE, ROOT, STUB_UM_RUN, RunnerHarness, requires_windows_pwsh, run_pwsh,
)

MODULE = DV / "DualVenueRunner.psm1"
SCHEMA = "mlv-app/venue-display-path/v1"
# r3 (A7): the block is the health probe's sample, taken BEFORE the measurement job is submitted; the block says so in a field the validator requires
SAMPLED_BEFORE = "measurement-submit"
# r3 (A4): text that ConvertFrom-Json reads as a [datetime] on reload (a power plan, an adapter, a reason named like a time)
TIME_LOOKING_TEXT = ("2026-11-24T00:00:00Z", "2026-11-24T03:00:00-04:00", "2026-11-24T12:00:00", "2026-11-24T00:00:00.5Z")


def unknown(reason: str) -> dict:
    return {"state": "unknown", "reason": reason}


def good_block() -> dict:
    """What the collector writes on Bachelor after the 2026-11-21 reboot: Intel Iris Xe drives the display, the NVIDIA RTX 3060 is headless."""
    return {
        "schema": SCHEMA,
        "sampledBefore": SAMPLED_BEFORE,
        "collectedUtc": "2026-11-24T16:40:00Z",
        "adapters": [
            {"name": "Intel(R) Iris(R) Xe Graphics", "driverVersion": "31.0.101.5186", "driverDate": "2024-06-12T00:00:00Z",
             "pnpDeviceId": "PCI\\VEN_8086&DEV_9A49\\3&11583659&0&10", "hasActiveOutput": True},
            {"name": "NVIDIA GeForce RTX 3060 Laptop GPU", "driverVersion": "32.0.15.6094", "driverDate": "2024-09-01T00:00:00Z",
             "pnpDeviceId": "PCI\\VEN_10DE&DEV_2560\\4&2C0D5D9&0&0008", "hasActiveOutput": False},
        ],
        "activeDisplays": [
            {"adapterName": "Intel(R) Iris(R) Xe Graphics", "refreshHz": 59, "horizontalResolution": 3840, "verticalResolution": 2400},
        ],
        "os": {"build": "26100", "ubr": 4770, "lastBootUtc": "2026-11-21T09:00:00Z", "hotfixCount": 14},
        "powerPlan": "Balanced",
    }


def all_unknown_block() -> dict:
    """Every probe failed: each field is a typed unknown, the block still has its shape."""
    return {
        "schema": SCHEMA,
        "sampledBefore": SAMPLED_BEFORE,
        "collectedUtc": "2026-11-24T16:40:00Z",
        "adapters": unknown("RPC server unavailable"),
        "activeDisplays": unknown("adapters-unreadable"),
        "os": {"build": unknown("registry-unreadable"), "ubr": unknown("registry-unreadable"),
               "lastBootUtc": unknown("RPC server unavailable"), "hotfixCount": unknown("budget-exhausted")},
        "powerPlan": unknown("access denied"),
    }


def receipt_with(block, outcome: str = "UNRESOLVED", **venue_over) -> dict:
    venue = {"name": "bachelor", "role": "supplementary", "declared": "bachelor", "hostName": "BACHELOR", "gpuNames": ["Intel(R) Iris(R) Xe Graphics"]}
    venue.update(venue_over)
    if block is not None:
        venue["displayPath"] = block
    return {"schema": "mlv-app/dual-venue-receipt/v1", "outcome": outcome, "venue": venue, "subject": {}}


def pwsh_json(script: str, env: dict | None = None) -> dict:
    proc = run_pwsh(["-Command", f"$ErrorActionPreference = 'Stop'\nImport-Module '{MODULE}' -Force\n" + script], env_extra=env or {})
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise AssertionError(proc.stdout + proc.stderr)


def validate(receipt: dict) -> dict:
    return pwsh_json("$r = $env:DVE_RECEIPT | ConvertFrom-Json\n$v = Test-DvReceiptValid -Receipt $r\n"
                     "[ordered]@{ status = $v.status; valid = $v.valid; displayPath = $v.displayPath; reasons = @($v.reasons); unbound = @($v.unbound) } | ConvertTo-Json -Compress -Depth 4",
                     {"DVE_RECEIPT": json.dumps(receipt)})


def block_state(block) -> dict:
    return pwsh_json("$b = $env:DVE_BLOCK | ConvertFrom-Json\n$v = Test-DvDisplayPathBlock -Block $b\n[ordered]@{ state = $v.state; problems = @($v.problems) } | ConvertTo-Json -Compress",
                     {"DVE_BLOCK": json.dumps(block)})


def validate_many(receipts: dict) -> dict:
    """validate() for many receipts in one pwsh: each receipt is its own JSON text, read back by ConvertFrom-Json exactly as a receipt reader reads a file."""
    texts = {label: json.dumps(r) for label, r in receipts.items()}
    with tempfile.TemporaryDirectory(prefix="dv-displaypath-many-") as d:
        path = Path(d) / "receipts.json"   # dozens of receipts do not fit in one environment variable (32767 characters)
        path.write_text(json.dumps(texts), encoding="utf-8")
        return pwsh_json("$in = [IO.File]::ReadAllText($env:DVE_RECEIPTS_FILE) | ConvertFrom-Json -AsHashtable\n$out = [ordered]@{}\n"
                         "foreach ($k in @($in.Keys)) { $r = $in[$k] | ConvertFrom-Json; $v = Test-DvReceiptValid -Receipt $r\n"
                         "  $out[$k] = [ordered]@{ status = $v.status; valid = $v.valid; displayPath = $v.displayPath; reasons = @($v.reasons) } }\n"
                         "$out | ConvertTo-Json -Compress -Depth 5",
                         {"DVE_RECEIPTS_FILE": str(path)})


@requires_windows_pwsh
class ReceiptValidatorReportsTheDisplayPathTests(unittest.TestCase):
    def test_a_receipt_without_the_block_is_valid_and_says_absent(self) -> None:
        got = validate(receipt_with(None))
        self.assertEqual((got["status"], got["valid"], got["displayPath"]), ("NO_SIGNAL", True, "absent"), got)
        self.assertEqual(got["reasons"], [])

    def test_a_null_block_is_the_same_as_an_absent_one(self) -> None:
        rec = receipt_with(None)
        rec["venue"]["displayPath"] = None
        got = validate(rec)
        self.assertEqual((got["status"], got["valid"], got["displayPath"]), ("NO_SIGNAL", True, "absent"), got)

    def test_a_malformed_block_is_a_validator_error(self) -> None:
        bad = {}
        base = good_block()
        bad["a string"] = "Intel Iris Xe"
        bad["an empty object"] = {}
        bad["an array"] = [base]
        bad["wrong schema"] = dict(base, schema="mlv-app/other/v9")
        bad["adapters is an object"] = dict(base, adapters={"name": "x"})
        bad["adapter lacks hasActiveOutput"] = dict(base, adapters=[{k: v for k, v in base["adapters"][0].items() if k != "hasActiveOutput"}])
        bad["hasActiveOutput is a string"] = dict(base, adapters=[dict(base["adapters"][0], hasActiveOutput="yes")])
        bad["refreshHz is a string"] = dict(base, activeDisplays=[dict(base["activeDisplays"][0], refreshHz="59")])
        bad["refreshHz is negative"] = dict(base, activeDisplays=[dict(base["activeDisplays"][0], refreshHz=-1)])
        bad["driverDate is not a date"] = dict(base, adapters=[dict(base["adapters"][0], driverDate="last tuesday")])
        bad["os is missing"] = {k: v for k, v in base.items() if k != "os"}
        bad["os.ubr is a string"] = dict(base, os=dict(base["os"], ubr="4770"))
        bad["lastBootUtc is not a date"] = dict(base, os=dict(base["os"], lastBootUtc="yesterday"))
        bad["powerPlan is a number"] = dict(base, powerPlan=7)
        bad["powerPlan is empty"] = dict(base, powerPlan="")
        bad["unknown without a reason"] = dict(base, powerPlan={"state": "unknown"})
        bad["unknown with an empty reason"] = dict(base, powerPlan={"state": "unknown", "reason": ""})
        bad["a state that is not unknown"] = dict(base, powerPlan={"state": "known", "reason": "x"})
        bad["collectedUtc is missing"] = {k: v for k, v in base.items() if k != "collectedUtc"}
        for label, block in bad.items():
            with self.subTest(label):
                got = validate(receipt_with(block))
                self.assertEqual((got["status"], got["valid"], got["displayPath"]), ("INVALID", False, "malformed"), got)
                self.assertTrue(any(r.startswith("DISPLAY_PATH_MALFORMED") for r in got["reasons"]), got)

    def test_adversary_r2_a2_a8_names_are_exact_strings_and_a_typed_unknown_has_only_its_two_members(self) -> None:
        """r2 adversary A2 / A8: -cne / -ceq coerce an untyped left operand and ignore zero-width and soft-hyphen characters; a typed unknown is exactly {state, reason}."""
        base = good_block()
        bad = {
            "schema is true": dict(base, schema=True),
            "schema is an empty list": dict(base, schema=[]),
            "schema is a list holding the string": dict(base, schema=[SCHEMA]),
            "schema carries a soft hyphen": dict(base, schema=SCHEMA.replace("display-path", "display-pa­th")),
            "the whole block is an unknown with an extra member": {"state": "unknown", "reason": "denied", "adapters": 17},
            "the whole block is an unknown with a zero-width character": {"state": "un​known", "reason": "denied"},
            "os is an unknown with an extra member": dict(base, os={"state": "unknown", "reason": "denied", "build": "26100"}),
            "powerPlan is an unknown with a zero-width character": dict(base, powerPlan={"state": "un​known", "reason": "denied"}),
        }
        for label, block in bad.items():
            with self.subTest(label):
                got = block_state(block)
                self.assertEqual(got["state"], "malformed", got)

    def test_adversary_r2_a3_only_a_utc_spelling_is_a_utc_time(self) -> None:
        """r2 adversary A3: a zone-less or offset time is read on the reader's clock (ConvertFrom-Json hands the validator a DateTime); `$` also matched before a final newline."""
        spellings = {"zone-less": "2026-11-24T12:00:00", "an offset": "2026-11-24T08:00:00-04:00", "a trailing newline": "2026-11-24T12:00:00Z\n"}
        for label, value in spellings.items():
            for where in ("collectedUtc", "driverDate", "lastBootUtc"):
                with self.subTest(label, where=where):
                    block = json.loads(json.dumps(good_block()))
                    if where == "collectedUtc":
                        block["collectedUtc"] = value
                    elif where == "driverDate":
                        block["adapters"][0]["driverDate"] = value
                    else:
                        block["os"]["lastBootUtc"] = value
                    got = block_state(block)
                    self.assertEqual(got["state"], "malformed", got)
        self.assertEqual(block_state(good_block())["state"], "present")

    def test_adversary_r2_a5_the_display_table_must_match_the_adapters_flags(self) -> None:
        """r2 adversary A5: activeDisplays is one entry per adapter whose hasActiveOutput is true, under that adapter's name."""
        base = good_block()
        intel, nvidia = base["adapters"]
        bad = {
            "a display for an adapter flagged inactive": dict(base, adapters=[dict(intel, hasActiveOutput=False), nvidia]),
            "a display named for the headless adapter": dict(base, activeDisplays=[dict(base["activeDisplays"][0], adapterName=nvidia["name"])]),
            "an active adapter with no display entry": dict(base, activeDisplays=[]),
            "two display entries for one adapter": dict(base, activeDisplays=[base["activeDisplays"][0], base["activeDisplays"][0]]),
        }
        for label, block in bad.items():
            with self.subTest(label):
                self.assertEqual(block_state(block)["state"], "malformed", label)
        headless = dict(base, adapters=[dict(intel, hasActiveOutput=False), nvidia], activeDisplays=[])
        self.assertEqual(block_state(headless)["state"], "present")

    def test_adversary_r2_a9_a_venue_that_is_not_an_object_is_malformed_not_absent(self) -> None:
        """r2 adversary A9: Get-DvProp on an array finds nothing, which read as `absent`."""
        rec = {"schema": "mlv-app/dual-venue-receipt/v1", "outcome": "UNRESOLVED", "venue": [{"displayPath": 7}], "subject": {}}
        got = validate(rec)
        self.assertEqual((got["valid"], got["displayPath"]), (False, "malformed"), got)
        self.assertTrue(any(r.startswith("DISPLAY_PATH_MALFORMED") for r in got["reasons"]), got)

    def test_a_block_whose_every_probe_failed_is_valid_and_present(self) -> None:
        got = validate(receipt_with(all_unknown_block()))
        self.assertEqual((got["status"], got["valid"], got["displayPath"]), ("NO_SIGNAL", True, "present"), got)
        got = validate(receipt_with(unknown("collector-error")))   # the whole block failed
        self.assertEqual((got["status"], got["valid"], got["displayPath"]), ("NO_SIGNAL", True, "present"), got)

    def test_a_well_formed_block_is_valid_and_present(self) -> None:
        got = validate(receipt_with(good_block()))
        self.assertEqual((got["status"], got["valid"], got["displayPath"]), ("NO_SIGNAL", True, "present"), got)
        self.assertEqual(got["reasons"], [])

    def test_one_unknown_field_among_known_ones_is_valid(self) -> None:
        block = good_block()
        block["adapters"][1]["driverDate"] = unknown("not-reported")
        block["os"]["hotfixCount"] = unknown("budget-exhausted")
        block["powerPlan"] = unknown("access denied")
        self.assertEqual(block_state(block)["state"], "present")

    def test_a_malformed_block_also_spoils_a_pass_receipt_but_an_absent_one_adds_nothing(self) -> None:
        bad = validate(receipt_with(dict(good_block(), powerPlan=7), outcome="PASS"))
        self.assertEqual((bad["status"], bad["valid"], bad["displayPath"]), ("INVALID", False, "malformed"), bad)
        self.assertTrue(any(r.startswith("DISPLAY_PATH_MALFORMED") for r in bad["reasons"]), bad)
        absent = validate(receipt_with(None, outcome="PASS"))
        self.assertEqual(absent["displayPath"], "absent", absent)
        self.assertFalse(any("DISPLAY_PATH" in r for r in absent["reasons"]), absent)
        present = validate(receipt_with(good_block(), outcome="PASS"))
        self.assertEqual(present["displayPath"], "present", present)
        self.assertFalse(any(r.startswith("DISPLAY_PATH_MALFORMED") for r in present["reasons"]), present)
        # the block is collected by the venue probe and written to no hashed artifact: the validator says so, as it does for hostName / gpuNames
        self.assertTrue(any(u.startswith("DISPLAY_PATH_UNBOUND") for u in present["unbound"]), present)

    def test_adversary_r2_a4_a_text_leaf_that_looks_like_a_time_survives_a_receipt_reload(self) -> None:
        """r2 adversary A4 (closed in r3, route (b)): every receipt reader uses ConvertFrom-Json, which reads `2026-11-24T00:00:00Z` (a power plan, an adapter or a
        reason named like a time) as a [datetime]. The block's validator accepts that round-tripped value at a TEXT leaf, so reload never turns NO_SIGNAL into INVALID."""
        cases = {}
        for text in TIME_LOOKING_TEXT:
            b = good_block()
            b["powerPlan"] = text
            cases[f"powerPlan={text}"] = b
            b = good_block()
            b["adapters"][0]["name"] = text
            b["activeDisplays"][0]["adapterName"] = text
            cases[f"active adapter name={text}"] = b
            b = good_block()
            b["adapters"][1]["driverVersion"] = text
            b["adapters"][1]["pnpDeviceId"] = text
            b["os"]["build"] = text
            cases[f"driverVersion/pnpDeviceId/build={text}"] = b
            b = good_block()
            b["powerPlan"] = unknown(text)
            b["os"] = unknown(text)
            cases[f"unknown reason={text}"] = b
            cases[f"whole-block unknown reason={text}"] = unknown(text)
        receipts = {}
        for label, block in cases.items():
            receipts[label + " NO_SIGNAL"] = receipt_with(block)
            receipts[label + " PASS"] = receipt_with(block, outcome="PASS")
        got = validate_many(receipts)
        for label in receipts:
            with self.subTest(label):
                v = got[label]
                self.assertEqual(v["displayPath"], "present", v)
                self.assertFalse(any(r.startswith("DISPLAY_PATH_MALFORMED") for r in v["reasons"]), v)
                if label.endswith("NO_SIGNAL"):
                    self.assertEqual((v["status"], v["valid"]), ("NO_SIGNAL", True), v)

    def test_adversary_r2_a4_a_time_looking_name_still_has_to_match_between_the_tables(self) -> None:
        """r3: the A5 name match compares the round-tripped names as text, so one adapter named like a time still needs its own display entry."""
        b = good_block()
        b["adapters"][0]["name"] = TIME_LOOKING_TEXT[0]
        self.assertEqual(block_state(b)["state"], "malformed")
        b["activeDisplays"][0]["adapterName"] = TIME_LOOKING_TEXT[1]
        self.assertEqual(block_state(b)["state"], "malformed")
        b["activeDisplays"][0]["adapterName"] = TIME_LOOKING_TEXT[0]
        self.assertEqual(block_state(b)["state"], "present")

    def test_adversary_r2_a7_the_block_says_it_was_sampled_before_the_measurement_was_submitted(self) -> None:
        """r2 adversary A7 (closed in r3 by a binding field): the block is the health probe's pre-submit sample, and a present block must say so."""
        base = good_block()
        bad = {
            "sampledBefore is missing": {k: v for k, v in base.items() if k != "sampledBefore"},
            "sampledBefore names another moment": dict(base, sampledBefore="measurement-end"),
            "sampledBefore is not a string": dict(base, sampledBefore=True),
            "sampledBefore is a typed unknown": dict(base, sampledBefore=unknown("x")),
            "sampledBefore carries a zero-width character": dict(base, sampledBefore="measurement-​submit"),
        }
        for label, block in bad.items():
            with self.subTest(label):
                got = block_state(block)
                self.assertEqual(got["state"], "malformed", got)
                self.assertTrue(any("sampledBefore" in p for p in got["problems"]), got)
        self.assertEqual(block_state(base)["state"], "present")
        self.assertEqual(block_state(all_unknown_block())["state"], "present")

    def test_successor1_w2_a_name_that_is_a_typed_unknown_stays_in_the_table_match(self) -> None:
        """SUCCESSOR-1 W2 (r3 adversary A2): a name that was a typed unknown dropped out of the A5 match, so an active adapter with no display entry (or a display
        entry with no adapter) passed. An unknown name is still a name: the unknown-named active adapters and the unknown-named display entries must agree in number."""
        base = good_block()
        intel, nvidia = base["adapters"]
        display = base["activeDisplays"][0]
        nameless = unknown("not-reported")
        bad = {
            "an active adapter named by an unknown, no display entry": dict(base, adapters=[dict(intel, name=nameless), nvidia], activeDisplays=[]),
            "a display entry named by an unknown, no adapter": dict(base, adapters=[], activeDisplays=[dict(display, adapterName=nameless)]),
            "two unknown-named display entries for one unknown-named active adapter": dict(
                base, adapters=[dict(intel, name=nameless), nvidia], activeDisplays=[dict(display, adapterName=nameless), dict(display, adapterName=nameless)]),
            "an unknown-named display entry where the active adapter has a name": dict(base, activeDisplays=[dict(display, adapterName=nameless)]),
        }
        receipts = {label: receipt_with(block) for label, block in bad.items()}
        paired = dict(base, adapters=[dict(intel, name=nameless), nvidia], activeDisplays=[dict(display, adapterName=nameless)])
        receipts["control: one unknown-named active adapter, one unknown-named display entry"] = receipt_with(paired)
        got = validate_many(receipts)
        for label in bad:
            with self.subTest(label):
                self.assertEqual((got[label]["valid"], got[label]["displayPath"]), (False, "malformed"), got[label])
                self.assertTrue(any(r.startswith("DISPLAY_PATH_MALFORMED") for r in got[label]["reasons"]), got[label])
        control = got["control: one unknown-named active adapter, one unknown-named display entry"]
        self.assertEqual((control["valid"], control["displayPath"]), (True, "present"), control)

    def test_successor1_w3_an_unknown_marker_beside_ordinary_fields_is_malformed(self) -> None:
        """SUCCESSOR-1 W3 (r3 adversary A5): `state`/`reason` added to an object that also carries its complete ordinary fields fell through to ordinary validation and
        passed. A typed unknown is exactly {state, reason}; an object of the schema that carries either member (in any letter case) is the contradictory mixed shape."""
        marker = {"state": "unknown", "reason": "denied"}

        def marked_everywhere(node):
            if isinstance(node, dict):
                return dict({k: marked_everywhere(v) for k, v in node.items()}, **marker)
            if isinstance(node, list):
                return [marked_everywhere(v) for v in node]
            return node

        base = good_block()
        bad = {
            "every object of the block": marked_everywhere(base),
            "the whole block": dict(base, **marker),
            "os": dict(base, os=dict(base["os"], **marker)),
            "an adapter entry": dict(base, adapters=[dict(base["adapters"][0], **marker), base["adapters"][1]]),
            "a display entry": dict(base, activeDisplays=[dict(base["activeDisplays"][0], **marker)]),
            "only a state on os": dict(base, os=dict(base["os"], state="unknown")),
            "only a reason on the block": dict(base, reason="denied"),
            "a State (other case) on os": dict(base, os=dict(base["os"], State="unknown")),
        }
        receipts = {label: receipt_with(block) for label, block in bad.items()}
        got = validate_many(receipts)
        for label in bad:
            with self.subTest(label):
                self.assertEqual((got[label]["valid"], got[label]["displayPath"]), (False, "malformed"), got[label])
        # the runner's normaliser calls the same checker: a mixed block from a probe becomes the typed unknown, never a present block
        probes = {label: json.dumps(dict(HEALTHY_PROBE, displayPath=block)) for label, block in bad.items()}
        norm = pwsh_json("$in = $env:DVE_PROBES | ConvertFrom-Json -AsHashtable\n$out = [ordered]@{}\n"
                         "foreach ($k in @($in.Keys)) { $out[$k] = ConvertTo-DvDisplayPath -Probe ($in[$k] | ConvertFrom-Json) }\n$out | ConvertTo-Json -Compress -Depth 10",
                         {"DVE_PROBES": json.dumps(probes)})
        for label in bad:
            with self.subTest("normaliser: " + label):
                self.assertEqual(norm[label], unknown("probe-block-malformed"), norm[label])

    def test_successor1_r1_adversary_a1_a3_the_key_union_is_ordinal_and_a_key_named_keys_shadows_nothing(self) -> None:
        """SUCCESSOR-1 r1 adversary A1: Select-Object -Unique merged a soft-hyphenated display name into the real one, hiding an extra display entry.
        A3: a dictionary key named `Keys` shadowed .Keys, so a mixed or padded typed unknown read as exactly {state, reason}."""
        base = good_block()
        extra = dict(base["activeDisplays"][0], adapterName="In­tel(R) Iris(R) Xe Graphics")
        self.assertEqual(block_state(dict(base, activeDisplays=base["activeDisplays"] + [extra]))["state"], "malformed")
        got = pwsh_json("$out = [ordered]@{}\n"
                        "$u = [ordered]@{ state = 'unknown'; reason = 'denied'; Keys = @('state', 'reason'); extra = 1 }\n"
                        "$out.wholeUnknown = (Test-DvDisplayPathBlock -Block $u).state\n"
                        "$b = $env:DVE_BLOCK | ConvertFrom-Json -AsHashtable; $b['powerPlan'] = $u; $out.leafUnknown = (Test-DvDisplayPathBlock -Block $b).state\n"
                        "$b = $env:DVE_BLOCK | ConvertFrom-Json -AsHashtable; $b['state'] = 'unknown'; $b['Keys'] = @(); $out.block = (Test-DvDisplayPathBlock -Block $b).state\n"
                        "$b = $env:DVE_BLOCK | ConvertFrom-Json -AsHashtable; $b['os']['state'] = 'unknown'; $b['os']['Keys'] = @(); $out.os = (Test-DvDisplayPathBlock -Block $b).state\n"
                        "$b = $env:DVE_BLOCK | ConvertFrom-Json -AsHashtable; $b['adapters'][0]['state'] = 'unknown'; $b['adapters'][0]['Keys'] = @(); $out.entry = (Test-DvDisplayPathBlock -Block $b).state\n"
                        "$b = $env:DVE_BLOCK | ConvertFrom-Json -AsHashtable; $out.control = (Test-DvDisplayPathBlock -Block $b).state\n"
                        "$out | ConvertTo-Json -Compress", {"DVE_BLOCK": json.dumps(base)})
        self.assertEqual(got, {"wholeUnknown": "malformed", "leafUnknown": "malformed", "block": "malformed", "os": "malformed", "entry": "malformed", "control": "present"}, got)

    def test_successor1_w4_an_explicit_null_venue_is_malformed_not_absent(self) -> None:
        """SUCCESSOR-1 W4 (r3 adversary A6): Get-DvProp gives $null both for a missing member and for an explicit null, so `"venue": null` read as absent. A venue
        that is THERE must be an object; a receipt with no venue member, or a venue whose displayPath is null or missing, stays absent."""
        explicit = {"schema": "mlv-app/dual-venue-receipt/v1", "outcome": "UNRESOLVED", "venue": None, "subject": {}}
        missing = {"schema": "mlv-app/dual-venue-receipt/v1", "outcome": "UNRESOLVED", "subject": {}}
        null_block = {"schema": "mlv-app/dual-venue-receipt/v1", "outcome": "UNRESOLVED", "venue": {"displayPath": None}, "subject": {}}
        got = validate_many({"explicit": explicit, "explicit PASS": dict(explicit, outcome="PASS"), "missing": missing, "null block": null_block})
        for label in ("explicit", "explicit PASS"):
            with self.subTest(label):
                self.assertEqual((got[label]["valid"], got[label]["displayPath"]), (False, "malformed"), got[label])
                self.assertTrue(any(r.startswith("DISPLAY_PATH_MALFORMED") for r in got[label]["reasons"]), got[label])
        for label in ("missing", "null block"):
            with self.subTest(label):
                self.assertEqual((got[label]["status"], got[label]["valid"], got[label]["displayPath"]), ("NO_SIGNAL", True, "absent"), got[label])


COLLECTOR_HARNESS = r"""
$ErrorActionPreference = 'Stop'
Import-Module '__MODULE__' -Force
. ([scriptblock]::Create((Get-DvDisplayPathCollectorSource)))
# ::new(), never New-Object: on PowerShell 7.6 a List[object] made by New-Object throws "Argument types do not match" under @($list), and the final report below wraps it that way
$script:calls = [System.Collections.Generic.List[object]]::new()
$script:regCalls = 0
$script:mode = $env:DVE_MODE
$script:sleepMs = [int]$env:DVE_SLEEP_MS
$script:fx = $env:DVE_FIXTURE | ConvertFrom-Json
function Get-CimInstance {
    [CmdletBinding()] param([string]$ClassName, [string]$Namespace = '', [string]$Filter = '', [int]$OperationTimeoutSec = 0)
    $script:calls.Add([pscustomobject]@{ class = $ClassName; ns = $Namespace; filter = $Filter; timeout = $OperationTimeoutSec })
    if ($script:sleepMs -gt 0) { Start-Sleep -Milliseconds $script:sleepMs }
    if ($script:mode -eq 'fail') { throw 'RPC server unavailable' }
    if ($script:mode -eq 'deny') { throw [System.UnauthorizedAccessException]::new('Access is denied') }
    switch ($ClassName) {
        'Win32_VideoController' { return @($script:fx.video) }
        'Win32_OperatingSystem' { return @([pscustomobject]@{ LastBootUpTime = [datetime]::Parse($script:fx.boot).ToLocalTime() }) }
        # DVE_PLAN, when set, is the plan name as CIM gives it (a string): the fixture's own ConvertFrom-Json would read a time-looking name as a [datetime]
        'Win32_PowerPlan' { return @([pscustomobject]@{ ElementName = $(if ($env:DVE_PLAN) { [string]$env:DVE_PLAN } else { $script:fx.plan }); IsActive = $true }) }
        # SUCCESSOR-2 (pass-2 advisory A10): 1..0 is the two numbers 1 and 0 in PowerShell, so a zero-hotfix fixture gave two rows
        'Win32_QuickFixEngineering' { $script:lastReadUtc = [datetime]::UtcNow; return @(if ([int]$script:fx.hotfixes -gt 0) { 1..[int]$script:fx.hotfixes | ForEach-Object { [pscustomobject]@{ HotFixID = "KB$_" } } }) }
    }
    @()
}
function Get-ItemProperty {
    [CmdletBinding()] param([string]$LiteralPath, [string]$Path)
    $script:regCalls++
    if ($script:mode -eq 'fail' -or $script:mode -eq 'deny') { throw 'Requested registry access is not allowed' }
    # SUCCESSOR-2 W5: DVE_UBR, when set, is the UBR the registry gives (a signed DWORD read back as an [int] can be negative)
    [pscustomobject]@{ CurrentBuild = '26100'; UBR = $(if ($env:DVE_UBR) { [int]$env:DVE_UBR } else { 4770 }) }
}
$script:lastReadUtc = $null
$sw = [Diagnostics.Stopwatch]::StartNew()
$budget = if ($env:DVE_BUDGET) { [int]$env:DVE_BUDGET } else { 15 }
$block = Get-DvDisplayPath -BudgetSec $budget
$ms = $sw.ElapsedMilliseconds
$roundTrip = ($block | ConvertTo-Json -Depth 8 -Compress) | ConvertFrom-Json
$verdict = Test-DvDisplayPathBlock -Block $roundTrip
$lastRead = $(if ($null -ne $script:lastReadUtc) { $script:lastReadUtc.ToString('o', [Globalization.CultureInfo]::InvariantCulture) } else { $null })
[ordered]@{ block = $block; calls = @($script:calls.ToArray()); regCalls = $script:regCalls; elapsedMs = $ms; state = $verdict.state; problems = @($verdict.problems); lastReadUtc = $lastRead } | ConvertTo-Json -Depth 12 -Compress
"""

BACHELOR_FIXTURE = {
    "video": [
        {"Name": "Intel(R) Iris(R) Xe Graphics", "DriverVersion": "31.0.101.5186", "DriverDate": "2024-06-12T00:00:00Z",
         "PNPDeviceID": "PCI\\VEN_8086&DEV_9A49\\3&11583659&0&10", "CurrentRefreshRate": 59, "CurrentHorizontalResolution": 3840, "CurrentVerticalResolution": 2400},
        {"Name": "NVIDIA GeForce RTX 3060 Laptop GPU", "DriverVersion": "32.0.15.6094", "DriverDate": "2024-09-01T00:00:00Z",
         "PNPDeviceID": "PCI\\VEN_10DE&DEV_2560\\4&2C0D5D9&0&0008", "CurrentRefreshRate": None, "CurrentHorizontalResolution": None, "CurrentVerticalResolution": None},
    ],
    "boot": "2026-11-21T09:00:00Z", "plan": "Balanced", "hotfixes": 14,
}


def run_collector(mode: str = "ok", sleep_ms: int = 0, budget: int | None = None, fixture: dict | None = None, plan: str | None = None, ubr: int | None = None) -> dict:
    env = {"DVE_MODE": mode, "DVE_SLEEP_MS": str(sleep_ms), "DVE_FIXTURE": json.dumps(fixture or BACHELOR_FIXTURE)}
    if plan is not None:
        env["DVE_PLAN"] = plan
    if ubr is not None:
        env["DVE_UBR"] = str(ubr)
    if budget is not None:
        env["DVE_BUDGET"] = str(budget)
    with tempfile.TemporaryDirectory(prefix="dv-displaypath-") as d:
        script = Path(d) / "harness.ps1"
        script.write_text(COLLECTOR_HARNESS.replace("__MODULE__", str(MODULE)), encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra=env)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise AssertionError(proc.stdout + proc.stderr)


# SUCCESSOR-1 W1 (r3 adversary A1): the job runs the collector through Get-DvDisplayPathBounded, whose worker sees the reads the prelude defines. A read that blocks
# in native code ([Threading.Thread]::Sleep ignores -OperationTimeoutSec and a pipeline stop, as a hung registry or WMI provider call does) must cost the job the budget
# and no more: the block is its typed unknown, and the job's own last line still ends the process with the abandoned read in flight.
BOUNDED_PRELUDE = r"""
function Get-CimInstance {
    [CmdletBinding()] param([string]$ClassName, [string]$Namespace = '', [string]$Filter = '', [int]$OperationTimeoutSec = 0)
    if ($env:DVE_BLOCK_AT -eq $ClassName) { [System.Threading.Thread]::Sleep([int]$env:DVE_BLOCK_MS) }
    switch ($ClassName) {
        'Win32_VideoController' { return @([pscustomobject]@{ Name = 'Intel(R) Iris(R) Xe Graphics'; DriverVersion = '31.0.101.5186'; DriverDate = [datetime]::new(2024, 6, 12, 0, 0, 0, [DateTimeKind]::Utc); PNPDeviceID = 'PCI\VEN_8086'; CurrentRefreshRate = 59; CurrentHorizontalResolution = 3840; CurrentVerticalResolution = 2400 }) }
        'Win32_OperatingSystem' { return @([pscustomobject]@{ LastBootUpTime = [datetime]::new(2026, 11, 21, 9, 0, 0, [DateTimeKind]::Utc) }) }
        'Win32_PowerPlan' { return @([pscustomobject]@{ ElementName = 'Balanced'; IsActive = $true }) }
        'Win32_QuickFixEngineering' { return @([pscustomobject]@{ HotFixID = 'KB1' }) }
    }
    @()
}
function Get-ItemProperty {
    [CmdletBinding()] param([string]$LiteralPath, [string]$Path)
    if ($env:DVE_BLOCK_AT -eq 'registry') { [System.Threading.Thread]::Sleep([int]$env:DVE_BLOCK_MS) }
    [pscustomobject]@{ CurrentBuild = '26100'; UBR = 4770 }
}
"""

BOUNDED_HARNESS = r"""
$ErrorActionPreference = 'Stop'
Import-Module '__MODULE__' -Force
. ([scriptblock]::Create((Get-DvDisplayPathCollectorSource)))
$jobText = New-DvHealthProbeJobText -AgentRoot 'C:\mlvtmp\mlv-agent'
$endLine = @($jobText -split "`r?`n" | Where-Object { $_.StartsWith('if ($script:DvDisplayPathAbandoned)') })[0]
$sw = [Diagnostics.Stopwatch]::StartNew()
. $env:DVE_PRELUDE_FILE
$stand = [ordered]@{ 'Get-CimInstance' = ${function:Get-CimInstance}.ToString(); 'Get-ItemProperty' = ${function:Get-ItemProperty}.ToString() }
$block = Get-DvDisplayPathBounded -BudgetSec ([int]$env:DVE_BUDGET) -WorkerFunctions $stand
$ms = $sw.ElapsedMilliseconds
$rt = ($block | ConvertTo-Json -Depth 8 -Compress) | ConvertFrom-Json
$v = Test-DvDisplayPathBlock -Block $rt
Write-Output ([ordered]@{ block = $block; elapsedMs = $ms; state = $v.state; endLine = [string]$endLine } | ConvertTo-Json -Depth 12 -Compress)
if ($endLine) { . ([scriptblock]::Create($endLine)) }
"""


def run_bounded(block_at: str = "", block_ms: int = 0, budget: int = 3) -> tuple[dict, float]:
    import time
    with tempfile.TemporaryDirectory(prefix="dv-displaypath-bounded-") as d:
        script = Path(d) / "harness.ps1"
        script.write_text(BOUNDED_HARNESS.replace("__MODULE__", str(MODULE)), encoding="utf-8")
        prelude = Path(d) / "prelude.ps1"
        prelude.write_text(BOUNDED_PRELUDE, encoding="utf-8")
        started = time.monotonic()
        proc = run_pwsh(["-File", str(script)], env_extra={"DVE_BLOCK_AT": block_at, "DVE_BLOCK_MS": str(block_ms), "DVE_BUDGET": str(budget), "DVE_PRELUDE_FILE": str(prelude)})
        wall = time.monotonic() - started
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1]), wall
    except (ValueError, IndexError):
        raise AssertionError(proc.stdout + proc.stderr)


def source() -> str:
    got = pwsh_json("[ordered]@{ text = (Get-DvDisplayPathCollectorSource) } | ConvertTo-Json -Compress")
    return got["text"]


@requires_windows_pwsh
class CollectorReadsTheBachelorShapeTests(unittest.TestCase):
    def test_the_headless_nvidia_has_no_output_and_the_intel_adapter_is_the_one_active_display(self) -> None:
        got = run_collector()
        block = got["block"]
        self.assertEqual(got["state"], "present", got)
        self.assertEqual(block["schema"], SCHEMA)
        by_name = {a["name"]: a for a in block["adapters"]}
        self.assertIs(by_name["Intel(R) Iris(R) Xe Graphics"]["hasActiveOutput"], True)
        self.assertIs(by_name["NVIDIA GeForce RTX 3060 Laptop GPU"]["hasActiveOutput"], False)
        self.assertEqual(by_name["Intel(R) Iris(R) Xe Graphics"]["driverVersion"], "31.0.101.5186")
        self.assertEqual(by_name["Intel(R) Iris(R) Xe Graphics"]["driverDate"], "2024-06-12T00:00:00Z")
        self.assertEqual(by_name["NVIDIA GeForce RTX 3060 Laptop GPU"]["pnpDeviceId"], "PCI\\VEN_10DE&DEV_2560\\4&2C0D5D9&0&0008")
        self.assertEqual(block["activeDisplays"], [{"adapterName": "Intel(R) Iris(R) Xe Graphics", "refreshHz": 59, "horizontalResolution": 3840, "verticalResolution": 2400}])
        self.assertEqual(block["os"], {"build": "26100", "ubr": 4770, "lastBootUtc": "2026-11-21T09:00:00Z", "hotfixCount": 14})
        self.assertEqual(block["powerPlan"], "Balanced")
        self.assertRegex(block["collectedUtc"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_every_cim_call_is_bounded_and_none_asks_for_more_than_the_budget(self) -> None:
        got = run_collector()
        self.assertGreaterEqual(len(got["calls"]), 4, got["calls"])
        for call in got["calls"]:
            self.assertTrue(1 <= call["timeout"] <= 15, call)
        self.assertIn("Win32_PowerPlan", [c["class"] for c in got["calls"]])
        plan = [c for c in got["calls"] if c["class"] == "Win32_PowerPlan"][0]
        self.assertEqual(plan["ns"], "root\\cimv2\\power")

    def test_adversary_r2_a7_the_collector_says_its_sample_was_taken_before_the_measurement_submit(self) -> None:
        """r2 adversary A7 (r3): the collector runs in the health probe, before the measurement job is submitted; its block carries that moment and when it read."""
        for mode in ("ok", "fail"):
            with self.subTest(mode):
                got = run_collector(mode=mode)
                self.assertEqual(got["block"]["sampledBefore"], SAMPLED_BEFORE, got)
                self.assertRegex(got["block"]["collectedUtc"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
                self.assertEqual(got["state"], "present", got)

    def test_adversary_r2_a4_a_power_plan_named_like_a_time_survives_the_round_trip(self) -> None:
        for text in TIME_LOOKING_TEXT:
            with self.subTest(text):
                got = run_collector(plan=text)
                self.assertEqual(got["block"]["powerPlan"], text, got)
                self.assertEqual(got["state"], "present", got)   # after the harness's ConvertTo-Json / ConvertFrom-Json round trip

    def test_no_adapter_at_all_is_an_empty_list_not_a_failure(self) -> None:
        got = run_collector(fixture=dict(BACHELOR_FIXTURE, video=[]))
        self.assertEqual(got["state"], "present", got)
        self.assertEqual(got["block"]["adapters"], [])
        self.assertEqual(got["block"]["activeDisplays"], [])


@requires_windows_pwsh
class CollectorNeverFailsOrDelaysTheLegTests(unittest.TestCase):
    def test_every_probe_failing_gives_typed_unknowns_and_does_not_throw(self) -> None:
        for mode in ("fail", "deny"):
            with self.subTest(mode):
                got = run_collector(mode=mode)
                block = got["block"]
                self.assertEqual(got["state"], "present", got)
                self.assertEqual(block["adapters"]["state"], "unknown")
                self.assertEqual(block["activeDisplays"]["state"], "unknown")
                for field in ("build", "ubr", "lastBootUtc", "hotfixCount"):
                    self.assertEqual(block["os"][field]["state"], "unknown", (field, block["os"]))
                    self.assertTrue(block["os"][field]["reason"], (field, block["os"]))
                self.assertEqual(block["powerPlan"]["state"], "unknown")
                self.assertLess(len(block["powerPlan"]["reason"]), 200)

    def test_a_slow_host_stops_asking_once_the_budget_is_spent(self) -> None:
        # each call takes 1.1 s; the budget is 3 s: the adapters and boot-time reads fit, the later ones are skipped as budget-exhausted, the block returns near 3 s
        got = run_collector(sleep_ms=1100, budget=3)
        block = got["block"]
        self.assertLess(got["elapsedMs"], 6500, got)
        self.assertEqual(block["adapters"][0]["name"], "Intel(R) Iris(R) Xe Graphics")
        self.assertEqual(block["os"]["hotfixCount"], {"state": "unknown", "reason": "budget-exhausted"})
        self.assertEqual(block["powerPlan"], {"state": "unknown", "reason": "budget-exhausted"})
        for call in got["calls"]:
            self.assertGreaterEqual(call["timeout"], 1)
            self.assertLessEqual(call["timeout"], 3)
        self.assertEqual(got["state"], "present")

    def test_adversary_r2_a1_an_answer_that_returns_after_the_cap_is_a_typed_unknown_and_nothing_starts_after_it(self) -> None:
        """r2 adversary A1: one call that takes 1.5 s against a 1 s budget: its answer is `budget-exceeded`, then no CIM call and no registry read starts."""
        got = run_collector(sleep_ms=2500, budget=2)
        block = got["block"]
        self.assertEqual(block["adapters"], {"state": "unknown", "reason": "budget-exceeded"}, got)
        self.assertEqual(len(got["calls"]), 1, got["calls"])
        self.assertEqual(got["regCalls"], 0, got)
        self.assertEqual(block["os"]["build"], {"state": "unknown", "reason": "budget-exhausted"}, got)
        self.assertEqual(block["os"]["hotfixCount"], {"state": "unknown", "reason": "budget-exhausted"}, got)
        self.assertLess(got["elapsedMs"], 3600, got)
        self.assertEqual(got["state"], "present", got)

    def test_adversary_r2_a1_a_spent_budget_asks_nothing(self) -> None:
        got = run_collector(budget=0)
        self.assertEqual(got["calls"], [], got)
        self.assertEqual(got["regCalls"], 0, got)
        self.assertEqual(got["block"]["adapters"], {"state": "unknown", "reason": "budget-exhausted"}, got)
        self.assertEqual(got["state"], "present", got)

    def test_adversary_r2_a11_a_caller_cannot_loosen_the_15_second_bound(self) -> None:
        got = run_collector(budget=60)
        self.assertGreaterEqual(len(got["calls"]), 4, got["calls"])
        for call in got["calls"]:
            self.assertLessEqual(call["timeout"], 15, call)

    def test_adversary_r2_a1_the_probe_job_gives_the_collector_only_what_is_left_of_the_job(self) -> None:
        """r2 adversary A1: the collector used to get a fresh 15 s after a health preamble that can reach 119 s of the agent's 120 s: its budget is now what is left of 105 s of the job's own clock."""
        text = pwsh_json("[ordered]@{ text = (New-DvHealthProbeJobText -AgentRoot 'C:\\mlvtmp\\mlv-agent') } | ConvertTo-Json -Compress")["text"]
        clock = text.find("$dveJobSw = [Diagnostics.Stopwatch]::StartNew()")
        first_work = text.find("$probe = [ordered]@{")
        call = re.search(r"Get-DvDisplayPath(?:Bounded)? -BudgetSec \(.*?(\d+)(?:\.0)? - \$dveJobSw\.Elapsed\.TotalSeconds", text)   # SUCCESSOR-1 W1: the bounded entry
        self.assertGreaterEqual(clock, 0, "the job has no job-wide stopwatch")
        self.assertLess(clock, first_work, "the job clock must start before the preamble does any work")
        self.assertIsNotNone(call, "the collector is called with a budget taken from the job clock")
        self.assertLessEqual(int(call.group(1)), 110, call.group(0))

    def test_r3_w1_the_job_hands_the_collector_the_floor_of_what_is_left_never_a_rounded_up_second(self) -> None:
        """r3 W1: `[math]::Min(15, 105 - <double>)` picked the Int32 overload and ROUNDED the double, so 14.6 s left became a 15 s budget (and the venue lease-batch
        preflight refuses an integer first operand). The expression is lifted from the job text and run against a job clock at fixed elapsed times."""
        got = pwsh_json("$t = New-DvHealthProbeJobText -AgentRoot 'C:\\mlvtmp\\mlv-agent'\n"
                        "$line = @($t -split \"`r?`n\" | Where-Object { $_.StartsWith('$probe.displayPath = ') })[0]\n"
                        "$expr = [regex]::Match($line, '-BudgetSec (.+?) \\} catch').Groups[1].Value\n"
                        "$out = [ordered]@{ expr = $expr }\n"
                        "foreach ($e in 0.0, 90.4, 90.6, 104.2, 104.99, 105.0, 300.0) {\n"
                        "  $dveJobSw = [pscustomobject]@{ Elapsed = [timespan]::FromSeconds($e) }\n"
                        "  $out[[string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $e)] = & ([scriptblock]::Create($expr)) }\n"
                        "$out | ConvertTo-Json -Compress")
        self.assertTrue(got["expr"], got)
        self.assertEqual({k: v for k, v in got.items() if k != "expr"},
                         {"0": 15, "90.4": 14, "90.6": 14, "104.2": 0, "104.99": 0, "105": 0, "300": 0}, got)

    def test_successor1_w1_a_read_that_blocks_past_the_budget_gives_the_blocks_typed_unknown_on_time(self) -> None:
        """SUCCESSOR-1 W1 (r3 adversary A1): a synchronous registry or CIM read that blocks for 40 s against a 3 s budget. The bounded collector returns the block's
        typed unknown inside the budget, and the process (the health job, on the venue) still ends with the read in flight, long before the read would return."""
        for where in ("registry", "Win32_OperatingSystem", "Win32_VideoController"):
            with self.subTest(where):
                got, wall = run_bounded(block_at=where, block_ms=40000, budget=3)
                self.assertEqual(got["block"], {"state": "unknown", "reason": "budget-exceeded"}, got)
                self.assertEqual(got["state"], "present", got)
                self.assertLess(got["elapsedMs"], 4500, got)
                self.assertTrue(got["endLine"], "the probe job has no line that ends it after an abandoned read")
                self.assertLess(wall, 30.0, f"the process waited for the blocked read ({wall:.1f} s)")

    def test_successor1_w1_a_read_inside_the_budget_still_gives_the_full_block(self) -> None:
        got, _ = run_bounded()
        self.assertEqual(got["state"], "present", got)
        self.assertEqual(got["block"]["os"], {"build": "26100", "ubr": 4770, "lastBootUtc": "2026-11-21T09:00:00Z", "hotfixCount": 1}, got)
        self.assertEqual(got["block"]["powerPlan"], "Balanced", got)
        self.assertEqual(got["block"]["activeDisplays"][0]["refreshHz"], 59, got)

    def test_the_slowest_probe_runs_last_so_a_stall_in_it_costs_the_others_nothing(self) -> None:
        got = run_collector()
        classes = [c["class"] for c in got["calls"]]
        self.assertEqual(classes[-1], "Win32_QuickFixEngineering", classes)

    def test_the_real_host_answers_inside_the_bound(self) -> None:
        # the live read-only queries on this machine: a well-formed block, inside 15 s (+ process start), whatever the machine says
        script = (". ([scriptblock]::Create((Get-DvDisplayPathCollectorSource)))\n$sw = [Diagnostics.Stopwatch]::StartNew()\n$b = Get-DvDisplayPath\n$ms = $sw.ElapsedMilliseconds\n"
                  "$rt = ($b | ConvertTo-Json -Depth 8 -Compress) | ConvertFrom-Json\n$v = Test-DvDisplayPathBlock -Block $rt\n"
                  "[ordered]@{ ms = $ms; state = $v.state; problems = @($v.problems) } | ConvertTo-Json -Compress")
        got = pwsh_json(script)
        self.assertEqual(got["state"], "present", got)
        self.assertLess(got["ms"], 16500, got)


@requires_windows_pwsh
class CollectorChangesNothingTests(unittest.TestCase):
    WRITE_VERBS = re.compile(r"\b(Set|New|Remove|Add|Clear|Start|Stop|Restart|Invoke|Enable|Disable|Register|Unregister|Out|Move|Copy|Rename|Write)-(?!Output\b)[A-Za-z]+", re.IGNORECASE)

    def test_the_collector_source_uses_only_read_commands(self) -> None:
        text = source()
        # comments may name anything; strip them before looking for verbs
        code = "\n".join(re.sub(r"(?<![`'\"])#.*$", "", line) for line in text.splitlines())
        verbs = sorted({m.group(0) for m in self.WRITE_VERBS.finditer(code)})
        self.assertEqual(verbs, [], f"the display-path collector must be read-only; found {verbs}")
        self.assertNotRegex(code, r"(?i)powercfg|setactive|\bsc\.exe|netsh|reg\s+(add|delete)|bcdedit|wmic|Invoke-CimMethod|Set-CimInstance|Start-Process")
        # the commands it CALLS: the collector's own `function Get-DvDisplayPath` declaration names it, it is not a call, so drop declarations before looking
        # r2 adversary A6: every time the collector writes goes through the invariant culture (th-TH would write the year 2569)
        self.assertNotRegex(code, r"ToString\('yyyy-MM-ddTHH:mm:ssZ'\)")
        calls_only = re.sub(r"(?im)^\s*function\s+[A-Za-z][\w-]*", "", code)
        called = sorted(set(re.findall(r"\b(Get-[A-Za-z]+)\b", calls_only)))
        # SUCCESSOR-1 W1: Get-DvDisplayPathBounded runs the collector's own reader (Get-DvDisplayPath) in its worker; no other command is called
        self.assertEqual(called, ["Get-CimInstance", "Get-DvDisplayPath", "Get-ItemProperty"], called)

    def test_the_source_parses_clean(self) -> None:
        got = pwsh_json("$t = Get-DvDisplayPathCollectorSource\n$tok = $null; $err = $null\n[void][System.Management.Automation.Language.Parser]::ParseInput($t, [ref]$tok, [ref]$err)\n"
                        "[ordered]@{ errors = @($err).Count; hasFunction = $t.Contains('function Get-DvDisplayPath') } | ConvertTo-Json -Compress")
        self.assertEqual(got, {"errors": 0, "hasFunction": True})


@requires_windows_pwsh
class HealthProbeCarriesTheCollectorTests(unittest.TestCase):
    def test_the_probe_job_embeds_the_collector_and_sets_displaypath_before_it_prints(self) -> None:
        got = pwsh_json("$p = New-DvHealthProbeJobText -AgentRoot 'C:\\mlvtmp\\mlv-agent'\n$fn = Get-DvDisplayPathCollectorSource\n"
                        "$tok = $null; $err = $null; [void][System.Management.Automation.Language.Parser]::ParseInput($p, [ref]$tok, [ref]$err)\n"
                        "[ordered]@{ embedsVerbatim = $p.Contains($fn); parseErrors = @($err).Count; fnAt = $p.IndexOf($fn); assignAt = $p.IndexOf('$probe.displayPath ='); printAt = $p.IndexOf('DVE_PROBE=');"
                        " depth = ([regex]::Match($p, 'ConvertTo-Json -Compress -Depth (\\d+)')).Groups[1].Value } | ConvertTo-Json -Compress")
        self.assertTrue(got["embedsVerbatim"])
        self.assertEqual(got["parseErrors"], 0)
        self.assertGreater(got["assignAt"], got["fnAt"])
        self.assertGreater(got["printAt"], got["assignAt"])
        # a block nests probe > displayPath > adapters > entry > unknown > reason: the probe's JSON depth must reach it or ConvertTo-Json flattens it to a string
        self.assertGreaterEqual(int(got["depth"]), 7, got)

    def test_successor1_w1_the_job_runs_the_collector_bounded_and_ends_itself_after_an_abandoned_read(self) -> None:
        """SUCCESSOR-1 W1 (r3 adversary A1): the job calls the bounded collector (never the bare reader) and, after it prints its line, ends the process when a read
        was abandoned, so a hung provider call can neither delay the DVE_PROBE line nor keep the job alive to the agent's 120 s kill."""
        got = pwsh_json("$p = New-DvHealthProbeJobText -AgentRoot 'C:\\mlvtmp\\mlv-agent'\n$lines = @($p -split \"`r?`n\")\n"
                        "$assign = @($lines | Where-Object { $_.StartsWith('$probe.displayPath = ') })[0]\n"
                        "[ordered]@{ assign = [string]$assign; printAt = $p.IndexOf('DVE_PROBE='); endAt = $p.IndexOf('if ($script:DvDisplayPathAbandoned)') } | ConvertTo-Json -Compress")
        self.assertIn("Get-DvDisplayPathBounded -BudgetSec ", got["assign"], got)
        self.assertGreater(got["endAt"], got["printAt"], got)

    def test_a_collector_that_throws_cannot_stop_the_probe(self) -> None:
        got = pwsh_json("$p = New-DvHealthProbeJobText -AgentRoot 'C:\\mlvtmp\\mlv-agent'\n"
                        "[ordered]@{ guarded = [bool]($p -match '(?s)try \\{ Get-DvDisplayPath(Bounded)?( -BudgetSec [^}]*)? \\} catch') } | ConvertTo-Json -Compress")
        self.assertTrue(got["guarded"])


@requires_windows_pwsh
class RunnerNormalisesTheProbeBlockTests(unittest.TestCase):
    def normalise(self, probe: dict) -> dict:
        return pwsh_json("$p = $env:DVE_PROBE | ConvertFrom-Json\n$b = ConvertTo-DvDisplayPath -Probe $p\n"
                         "$rt = ($b | ConvertTo-Json -Depth 8 -Compress) | ConvertFrom-Json\n$v = Test-DvDisplayPathBlock -Block $rt\n"
                         "[ordered]@{ block = $b; state = $v.state } | ConvertTo-Json -Depth 10 -Compress", {"DVE_PROBE": json.dumps(probe)})

    def test_a_probe_without_the_block_is_typed_unknown_not_absent(self) -> None:
        got = self.normalise(dict(HEALTHY_PROBE))
        self.assertEqual(got["block"], unknown("probe-did-not-report"))
        self.assertEqual(got["state"], "present")

    def test_a_malformed_probe_block_becomes_typed_unknown(self) -> None:
        for label, block in (("a string", "Intel"), ("an array", [1]), ("bad powerPlan", dict(good_block(), powerPlan=7)), ("empty", {})):
            with self.subTest(label):
                got = self.normalise(dict(HEALTHY_PROBE, displayPath=block))
                self.assertEqual(got["block"], unknown("probe-block-malformed"))
                self.assertEqual(got["state"], "present")

    def test_a_good_block_survives_json_date_coercion_as_utc_text(self) -> None:
        got = self.normalise(dict(HEALTHY_PROBE, displayPath=good_block()))
        self.assertEqual(got["block"], good_block())
        self.assertEqual(got["state"], "present")

    def test_adversary_r2_a3_a_zone_less_or_offset_time_is_not_turned_into_a_utc_instant(self) -> None:
        """r2 adversary A3: a zone-less time was rewritten on the reviewing host's clock (12:00 became 18:00Z there) and kept as a valid block."""
        for label, value in (("zone-less", "2026-11-24T12:00:00"), ("an offset", "2026-11-24T08:00:00-04:00")):
            with self.subTest(label):
                got = self.normalise(dict(HEALTHY_PROBE, displayPath=dict(good_block(), collectedUtc=value)))
                self.assertEqual(got["block"], unknown("probe-block-malformed"), got)

    def test_adversary_r2_a4_a_time_looking_text_leaf_survives_the_probe_line_and_a_reload(self) -> None:
        """r2 adversary A4 (r3): the runner parses the probe's `DVE_PROBE=` line itself (ConvertFrom-DvProbeStdout). A text leaf named like a time keeps its exact text
        where the host's ConvertFrom-Json has -DateKind String (pwsh 7.5+), and on every host the block stays present through the write and a reload."""
        blocks = {}
        for text in TIME_LOOKING_TEXT:
            b = good_block()
            b["powerPlan"] = text
            b["adapters"][1]["driverVersion"] = text
            blocks[text] = b
        got = pwsh_json("$in = $env:DVE_BLOCKS | ConvertFrom-Json -AsHashtable\n$out = [ordered]@{ hasDateKind = (Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind') }\n"
                        "foreach ($k in @($in.Keys)) {\n"
                        "  $p = ConvertFrom-DvProbeStdout (\"noise`r`nDVE_PROBE=\" + $in[$k] + \"`r`n\")\n"
                        "  $b = ConvertTo-DvDisplayPath -Probe $p\n"
                        "  $written = $b | ConvertTo-Json -Depth 8 -Compress\n"
                        "  $out[$k] = [ordered]@{ written = $written; state = (Test-DvDisplayPathBlock -Block ($written | ConvertFrom-Json)).state } }\n"
                        "$out | ConvertTo-Json -Compress -Depth 4",
                        {"DVE_BLOCKS": json.dumps({k: json.dumps(dict(HEALTHY_PROBE, displayPath=b)) for k, b in blocks.items()})})
        for text, block in blocks.items():
            with self.subTest(text):
                v = got[text]
                written = json.loads(v["written"])
                self.assertEqual(v["state"], "present", v)
                self.assertNotEqual(written, unknown("probe-block-malformed"), v)
                if got["hasDateKind"]:
                    self.assertEqual(written, block, v)

    def test_adversary_r2_a6_the_utc_text_does_not_depend_on_the_readers_culture(self) -> None:
        """r2 adversary A6: under th-TH the same DateTime formats as year 2569; every formatter takes the invariant culture."""
        got = pwsh_json("[Threading.Thread]::CurrentThread.CurrentCulture = [Globalization.CultureInfo]::new('th-TH')\n"
                        "$d = [datetime]::SpecifyKind([datetime]::new(2026, 11, 24, 12, 0, 0), [DateTimeKind]::Utc)\n"
                        "$text = & (Get-Module DualVenueRunner) { param($x) ConvertTo-DvDisplayPathUtcText $x } $d\n"
                        "[ordered]@{ text = $text } | ConvertTo-Json -Compress")
        self.assertEqual(got["text"], "2026-11-24T12:00:00Z", got)


@requires_windows_pwsh
class VenueLegReceiptTests(RunnerHarness, unittest.TestCase):
    """End to end through Invoke-VenueLeg.ps1 (offline stubs): the probe's block lands in the receipt, and no block, however bad, changes the leg's verdict."""

    def setUp(self) -> None:
        self.make_harness()
        # the shared stub serialises the probe at ConvertTo-Json's default depth 2, which flattens a nested block: this one keeps it
        self.um.write_text(STUB_UM_RUN.replace("($cfg.probe | ConvertTo-Json -Compress)", "($cfg.probe | ConvertTo-Json -Compress -Depth 10)"), encoding="utf-8")

    def sick(self, **extra) -> dict:
        return dict(HEALTHY_PROBE, pwshColdStartMs=9000, **extra)   # a typed VENUE_UNHEALTHY terminal: the receipt is written after the venue block is filled

    def test_a_probe_block_is_written_to_the_receipt(self) -> None:
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=self.sick(displayPath=good_block()))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt["venue"]["displayPath"], good_block())
        self.assertEqual(receipt["outcome"], "VENUE_UNHEALTHY")

    def test_an_all_unknown_block_is_written_and_the_verdict_is_unchanged(self) -> None:
        _, plain, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=self.sick())
        _, withblock, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=self.sick(displayPath=all_unknown_block()))
        self.assertEqual(withblock["venue"]["displayPath"], all_unknown_block())
        for key in ("outcome", "outcomeDetail"):
            self.assertEqual(withblock[key], plain[key], key)
        self.assertEqual(withblock["health"], plain["health"])

    def test_a_missing_or_malformed_probe_block_is_typed_unknown_and_never_changes_the_verdict(self) -> None:
        _, plain, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=self.sick())
        self.assertEqual(plain["venue"]["displayPath"], unknown("probe-did-not-report"))
        for label, block in (("a string", "x"), ("a number", 5), ("wrong shape", {"adapters": 3})):
            with self.subTest(label):
                proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=self.sick(displayPath=block))
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(receipt["venue"]["displayPath"], unknown("probe-block-malformed"))
                self.assertEqual((receipt["outcome"], receipt["outcomeDetail"]), (plain["outcome"], plain["outcomeDetail"]))

    def test_successor3_r2_b1_a_one_element_array_probe_with_a_display_only_parse_failure_keeps_the_leg_verdict(self) -> None:
        """SUCCESSOR-3 r1 sol B1, end to end (Invoke-VenueLeg.ps1:681-687): the probe line is a one-element array whose displayPath stops ConvertFrom-Json. The
        leg keeps the probe's own health verdict and the block is the typed probe-block-malformed, never `health probe returned no DVE_PROBE line`."""
        _, plain, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=self.sick())
        head = json.dumps(self.sick())[:-1]
        for label, raw in (("two casings", "[" + head + ', "displayPath": 5, "DisplayPath": {}}]'), ("reserved member", "[" + head + ', "displayPath": {"PSObject": 1}}]')):
            with self.subTest(label):
                self.um.write_text(STUB_UM_RUN.replace("('DVE_PROBE=' + ($cfg.probe | ConvertTo-Json -Compress))", "('DVE_PROBE=' + '" + raw.replace("'", "''") + "')"),
                                   encoding="utf-8")
                proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=self.sick())
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(receipt["venue"]["displayPath"], unknown("probe-block-malformed"), receipt["venue"])
                self.assertEqual((receipt["outcome"], receipt["outcomeDetail"]), (plain["outcome"], plain["outcomeDetail"]), receipt)
                self.assertEqual(receipt["health"]["pwshColdStartMs"], 9000, receipt["health"])

    def test_a_host_mismatch_terminal_still_carries_the_block(self) -> None:
        wrong = dict(HEALTHY_PROBE, hostName="BACHELOR", displayPath=good_block())
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=wrong)
        self.assertEqual(receipt["outcome"], "VENUE_HOST_MISMATCH")
        self.assertEqual(receipt["venue"]["displayPath"], good_block())


@requires_windows_pwsh
class SchemaAndDocsTests(unittest.TestCase):
    def test_the_receipt_skeleton_names_the_field(self) -> None:
        # SUCCESSOR-2 W3 (pass-2 A4): a new receipt never says absent; until the probe answers its block is the typed unknown probe-not-run
        got = pwsh_json("$r = New-DvReceipt -Card 'c' -LegId 'l' -DeclaredVenue 'bachelor' -Role 'supplementary' -Actor 'a' -Method 'm' -MethodBlobId 'b' -Backend 'cuda' -LookFlavor $null\n"
                        "[ordered]@{ has = $r['venue'].Contains('displayPath'); value = $r['venue']['displayPath'] } | ConvertTo-Json -Compress")
        self.assertEqual(got, {"has": True, "value": unknown("probe-not-run")})

    def test_the_evidence_doc_describes_every_field(self) -> None:
        doc = (ROOT / "docs" / "dual-venue-evidence.md").read_text(encoding="utf-8")
        self.assertIn("venue.displayPath", doc)
        for token in ("adapters", "activeDisplays", "powerPlan", "hotfixCount", "lastBootUtc", "ubr", "driverDate", "hasActiveOutput", "displayPath=absent",
                      '"state":"unknown"', "15 s", "read-only"):
            with self.subTest(token):
                section = doc.split("venue.displayPath", 1)[1]
                self.assertIn(token, section)

    def test_adversary_r2_a7_a4_the_doc_says_the_block_is_the_display_path_at_submit_time(self) -> None:
        """r3: the doc names the binding field, says the block describes the display path at submit time, and that a time-looking text leaf stays a text leaf."""
        section = (ROOT / "docs" / "dual-venue-evidence.md").read_text(encoding="utf-8").split("venue.displayPath", 1)[1].split("\n## ", 1)[0]
        for token in ("`sampledBefore`", '"measurement-submit"', "at submit time", "VENUE-LEG-DISPLAY-PATH-MEASUREMENT-SIDE-1",
                      "looks like a time", "VENUE-RECEIPT-READER-DATEKIND-STRING-1"):
            with self.subTest(token):
                self.assertIn(token, section)

    def test_the_sheet_pair_record_lists_the_block_as_unbound(self) -> None:
        text = (DV / "New-VenueSheetPair.ps1").read_text(encoding="utf-8")
        self.assertIn("'venue.displayPath'", text)


# SUCCESSOR-2 W1 (pass-2 A2): the bounded wrapper reads its clock again after the wait. -Clock is any object with an .Elapsed [timespan]; this one returns the
# DVE_CLOCK readings in order (the last one repeats), so a late completion and a budget spent before the worker starts are both exact, not a race.
CLOCK_HARNESS = r"""
$ErrorActionPreference = 'Stop'
Import-Module '__MODULE__' -Force
. ([scriptblock]::Create((Get-DvDisplayPathCollectorSource)))
. $env:DVE_PRELUDE_FILE
$stand = [ordered]@{ 'Get-CimInstance' = ${function:Get-CimInstance}.ToString(); 'Get-ItemProperty' = ${function:Get-ItemProperty}.ToString() }
$global:dveReads = [System.Collections.Generic.Queue[double]]::new()
foreach ($x in ($env:DVE_CLOCK -split ',')) { $global:dveReads.Enqueue([double]::Parse($x, [Globalization.CultureInfo]::InvariantCulture)) }
$global:dveReadCount = 0
$clock = [pscustomobject]@{}
$clock | Add-Member -MemberType ScriptProperty -Name Elapsed -Value { $global:dveReadCount++; [timespan]::FromSeconds($(if ($global:dveReads.Count -gt 1) { $global:dveReads.Dequeue() } else { $global:dveReads.Peek() })) }
$out = [ordered]@{}
try { $out.block = Get-DvDisplayPathBounded -BudgetSec 15 -WorkerFunctions $stand -Clock $clock } catch { $out.threw = [string]$_.Exception.Message }
$out.reads = $global:dveReadCount
$out | ConvertTo-Json -Depth 12 -Compress
"""


def run_clocked(readings: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="dv-displaypath-clock-") as d:
        script = Path(d) / "harness.ps1"
        script.write_text(CLOCK_HARNESS.replace("__MODULE__", str(MODULE)), encoding="utf-8")
        prelude = Path(d) / "prelude.ps1"
        prelude.write_text(BOUNDED_PRELUDE, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"DVE_CLOCK": readings, "DVE_PRELUDE_FILE": str(prelude), "DVE_BLOCK_AT": "", "DVE_BLOCK_MS": "0"})
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise AssertionError(proc.stdout + proc.stderr)


def dup_key_text(base: dict, path: list, key: str, first, second) -> str:
    """JSON text of `base` where the object at `path` carries `key` twice: first `first`, then `second` (json.dumps cannot write a duplicate key)."""
    marker = "__DVE_DUP_MARKER__"
    node = base
    for p in path:
        node = node[p]
    node[marker] = 0
    text = json.dumps(base)
    return text.replace(f'"{marker}": 0', f'"{key}": {json.dumps(first)}, "{key}": {json.dumps(second)}')


@requires_windows_pwsh
class Successor2Tests(unittest.TestCase):
    def test_w1_a_result_that_lands_after_the_budget_is_budget_exceeded(self) -> None:
        """pass-2 A2: the worker completes, but the caller reads its clock again after the wait and finds 15.3 s gone of a 15 s budget: the block is the typed unknown."""
        got = run_clocked("0,0,15.3")
        self.assertNotIn("threw", got, got)
        self.assertEqual(got["block"], unknown("budget-exceeded"), got)

    def test_w1_a_budget_spent_before_the_worker_starts_starts_nothing(self) -> None:
        got = run_clocked("15.3")
        self.assertNotIn("threw", got, got)
        self.assertEqual(got["block"], unknown("budget-exceeded"), got)

    def test_w1_a_result_inside_the_budget_is_the_block(self) -> None:
        got = run_clocked("0,0,1.0")
        self.assertNotIn("threw", got, got)
        self.assertEqual(got["block"]["powerPlan"], "Balanced", got)

    def test_w2_a_duplicate_key_in_the_block_is_malformed_in_the_receipt_text(self) -> None:
        """pass-2 A3: ConvertFrom-Json keeps the last of two exact-name keys, so a doubled field read as one. Given the receipt's text, the validator refuses it."""
        cases = {
            "state twice": dup_key_text(receipt_with({}), ["venue", "displayPath"], "state", "present", "unknown").replace('"displayPath": {', '"displayPath": {"reason": "x", ', 1),
            "sampledBefore twice": dup_key_text(receipt_with(good_block()), ["venue", "displayPath"], "sampledBefore", "during-measurement", SAMPLED_BEFORE),
            "deep: an adapter field twice": dup_key_text(receipt_with(good_block()), ["venue", "displayPath", "adapters", 0], "driverVersion", "1", "2"),
            "venue twice": dup_key_text({"schema": "mlv-app/dual-venue-receipt/v1", "outcome": "UNRESOLVED", "subject": {}}, [], "venue", [1], {}),
            "displayPath twice": dup_key_text(receipt_with(None), ["venue"], "displayPath", 5, good_block()),
        }
        control = json.dumps(receipt_with(good_block()))
        with tempfile.TemporaryDirectory(prefix="dv-displaypath-dup-") as d:
            path = Path(d) / "texts.json"
            path.write_text(json.dumps(dict(cases, control=control)), encoding="utf-8")
            got = pwsh_json("$in = [IO.File]::ReadAllText($env:DVE_TEXTS_FILE) | ConvertFrom-Json -AsHashtable\n$out = [ordered]@{}\n"
                            "foreach ($k in @($in.Keys)) { try { $r = $in[$k] | ConvertFrom-Json; $v = Test-DvReceiptValid -Receipt $r -ReceiptText $in[$k]\n"
                            "  $out[$k] = [ordered]@{ valid = $v.valid; displayPath = $v.displayPath; reasons = @($v.reasons) } } catch { $out[$k] = [ordered]@{ threw = [string]$_.Exception.Message } } }\n"
                            "$out | ConvertTo-Json -Compress -Depth 5", {"DVE_TEXTS_FILE": str(path)})
        for label in cases:
            with self.subTest(label):
                self.assertEqual((got[label].get("valid"), got[label].get("displayPath")), (False, "malformed"), got[label])
                self.assertTrue(any("duplicate" in r for r in got[label]["reasons"]), got[label])
        self.assertEqual((got["control"]["valid"], got["control"]["displayPath"]), (True, "present"), got["control"])

    def test_w2_a_duplicate_key_in_the_probe_line_is_a_malformed_probe_block(self) -> None:
        dup_state = dup_key_text(dict(HEALTHY_PROBE, displayPath={}), ["displayPath"], "state", "present", "unknown").replace('"displayPath": {', '"displayPath": {"reason": "x", ', 1)
        dup_deep = dup_key_text(dict(HEALTHY_PROBE, displayPath=good_block()), ["displayPath", "os"], "ubr", 1, 2)
        dup_root = dup_key_text(dict(HEALTHY_PROBE), [], "displayPath", 5, good_block())
        good = json.dumps(dict(HEALTHY_PROBE, displayPath=good_block()))
        got = pwsh_json("$in = $env:DVE_LINES | ConvertFrom-Json -AsHashtable\n$out = [ordered]@{}\n"
                        "foreach ($k in @($in.Keys)) { $p = ConvertFrom-DvProbeStdout (\"noise`r`nDVE_PROBE=\" + $in[$k] + \"`r`n\"); $out[$k] = ConvertTo-DvDisplayPath -Probe $p }\n"
                        "$out | ConvertTo-Json -Compress -Depth 10",
                        {"DVE_LINES": json.dumps({"state": dup_state, "deep": dup_deep, "root": dup_root, "good": good})})
        for label in ("state", "deep", "root"):
            with self.subTest(label):
                self.assertEqual(got[label], unknown("probe-block-malformed"), got[label])
        self.assertEqual(got["good"]["powerPlan"], "Balanced", got["good"])

    def test_w4_collected_utc_is_not_before_the_last_read_returned(self) -> None:
        """pass-2 A5: collectedUtc was stamped before the first read, so a mode that changed during the reads was dated to a time it did not yet exist."""
        got = run_collector(sleep_ms=700)
        from datetime import datetime, timezone
        stamp = datetime.strptime(got["block"]["collectedUtc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        last = datetime.fromisoformat(got["lastReadUtc"][:26].rstrip("Z")).replace(tzinfo=timezone.utc)
        self.assertGreaterEqual(stamp, last, got)
        self.assertEqual(got["state"], "present", got)

    def test_w5_a_negative_number_from_the_provider_is_that_leafs_typed_unknown_and_the_block_survives(self) -> None:
        """pass-2 A6: UBR=-1 (and a refresh rate of -1) was emitted as data; the validator then refused it and the runner dropped the whole block."""
        video = [dict(BACHELOR_FIXTURE["video"][0], CurrentRefreshRate=-1), BACHELOR_FIXTURE["video"][1]]
        got = run_collector(fixture=dict(BACHELOR_FIXTURE, video=video), ubr=-1)
        block = got["block"]
        self.assertEqual(block["os"]["ubr"], unknown("not-in-range"), block)
        self.assertEqual(block["activeDisplays"][0]["refreshHz"], unknown("not-in-range"), block)
        self.assertEqual(block["activeDisplays"][0]["horizontalResolution"], 3840, block)
        self.assertEqual(block["os"]["build"], "26100", block)
        self.assertEqual(got["state"], "present", got)
        normalised = pwsh_json("$p = $env:DVE_PROBE | ConvertFrom-Json\nConvertTo-DvDisplayPath -Probe $p | ConvertTo-Json -Depth 10 -Compress",
                               {"DVE_PROBE": json.dumps(dict(HEALTHY_PROBE, displayPath=block))})
        self.assertEqual(normalised["powerPlan"], "Balanced", normalised)

    def test_advisory_a10_a_zero_hotfix_fixture_has_no_hotfix(self) -> None:
        got = run_collector(fixture=dict(BACHELOR_FIXTURE, hotfixes=0))
        self.assertEqual(got["block"]["os"]["hotfixCount"], 0, got)

    def test_w6_a_proof_io_failure_does_not_hide_the_display_path_verdict(self) -> None:
        """pass-2 A7: the core validator's proof I/O threw before the displayPath check ran, so a malformed block surfaced as a sharing exception (or, in the writer,
        DVE_EVIDENCE_VALIDATION_IO). The block is judged first; a malformed one is INVALID with its own reason and the I/O failure beside it. A good block keeps the throw."""
        script = ("& (Get-Module DualVenueRunner) { function script:Test-DvReceiptValidCore { param($Receipt, $RepoRoot, $EvidenceDir, [switch]$AllowOfflineTestMode, $OfflineLegSpecBytes)\n"
                  "  throw \"Exception calling ReadAllBytes: The process cannot access the file 'C:\\x\\summary.json' because it is being used by another process.\" } }\n"
                  "$out = [ordered]@{}\n"
                  "foreach ($k in 'bad', 'good') { $r = $(if ($k -eq 'bad') { $env:DVE_BAD } else { $env:DVE_GOOD }) | ConvertFrom-Json\n"
                  "  try { $v = Test-DvReceiptValid -Receipt $r; $out[$k] = [ordered]@{ status = $v.status; valid = $v.valid; displayPath = $v.displayPath; reasons = @($v.reasons) } }\n"
                  "  catch { $out[$k] = [ordered]@{ threw = [string]$_.Exception.Message } } }\n"
                  "$out | ConvertTo-Json -Compress -Depth 5")
        got = pwsh_json(script, {"DVE_BAD": json.dumps(receipt_with(5, outcome="PASS")), "DVE_GOOD": json.dumps(receipt_with(good_block(), outcome="PASS"))})
        bad = got["bad"]
        self.assertNotIn("threw", bad, bad)
        self.assertEqual((bad["status"], bad["valid"], bad["displayPath"]), ("INVALID", False, "malformed"), bad)
        self.assertTrue(bad["reasons"][0].startswith("DISPLAY_PATH_MALFORMED"), bad)
        self.assertTrue(any(r.startswith("EVIDENCE_VALIDATION_IO") for r in bad["reasons"]), bad)
        self.assertIn("threw", got["good"], got["good"])

    # -- SUCCESSOR-3 (one class, no new shard-weight row) --------------------------------------------------------------------------------------------------------
    def test_successor3_w0_a_statement_error_the_core_survives_is_not_turned_into_a_throw(self) -> None:
        """SUCCESSOR-3 W0 (hosted heavy 3/14 on 4d4db9cd, NarrowedProductionReceiptsAndBoundBackendTests blanket revocation): a statement inside the core validator that
        fails without ending it (on master, the blanket filter over an empty blanket list: `Get-DvProp` gives `,@()`, so `$_` is an empty array) leaves the core's own
        result, CONSENT_REVOKED. The wrapper's `try` made that statement error unwind to its catch and re-throw. No `try` and no `Stop` here, exactly as that test's caller."""
        script = (f"Import-Module '{MODULE}' -Force\n"
                  "& (Get-Module DualVenueRunner) { function script:Test-DvReceiptValidCore { param($Receipt, $RepoRoot, $EvidenceDir, [switch]$AllowOfflineTestMode, $OfflineLegSpecBytes)\n"
                  "  Set-StrictMode -Version Latest\n  $kept = $false\n"
                  "  $kept = (@(@(, @()) | Where-Object { $null -ne $_ -and [string]$_.ownerSentenceSha256 -ceq 'x' }).Count -gt 0)\n"
                  "  [pscustomobject]@{ valid = $false; status = 'INVALID'; reasons = @('CONSENT_REVOKED: stub'); unbound = @(); evidenceDir = $null; legType = $null } } }\n"
                  "$out = [ordered]@{}\n"
                  "$v = Test-DvReceiptValid -Receipt ($env:DVE_GOOD | ConvertFrom-Json)\n$out.good = [ordered]@{ status = $v.status; displayPath = $v.displayPath; reasons = @($v.reasons) }\n"
                  "$v = Test-DvReceiptValid -Receipt ($env:DVE_ABSENT | ConvertFrom-Json)\n$out.absent = [ordered]@{ status = $v.status; displayPath = $v.displayPath; reasons = @($v.reasons) }\n"
                  "Write-Output ('DVE_OUT=' + ($out | ConvertTo-Json -Compress -Depth 5))\n")
        proc = run_pwsh(["-Command", script], env_extra={"DVE_GOOD": json.dumps(receipt_with(good_block(), outcome="PASS")),
                                                         "DVE_ABSENT": json.dumps(receipt_with(None, outcome="PASS"))})
        line = next((l for l in proc.stdout.splitlines() if l.startswith("DVE_OUT=")), None)
        self.assertIsNotNone(line, proc.stdout + proc.stderr)
        got = json.loads(line[len("DVE_OUT="):])
        self.assertEqual(got["good"], {"status": "INVALID", "displayPath": "present", "reasons": ["CONSENT_REVOKED: stub"]}, got)
        self.assertEqual(got["absent"], {"status": "INVALID", "displayPath": "absent", "reasons": ["CONSENT_REVOKED: stub"]}, got)

    def test_successor3_w1_case_variant_or_reserved_members_in_the_probe_block_are_the_malformed_block_not_a_lost_probe(self) -> None:
        """SUCCESSOR-2 adversary A4: two casings of displayPath, or a member named PSObject inside it, make ConvertFrom-Json throw; the probe was dropped and a good leg
        turned VENUE_UNHEALTHY. The probe's own fields are kept and the block is the typed `probe-block-malformed`."""
        head = json.dumps(dict(HEALTHY_PROBE))[:-1]
        lines = {
            "two casings": head + ', "displayPath": 5, "DisplayPath": {"state": "unknown", "reason": "x"}}',
            "reserved member": head + ', "displayPath": {"PSObject": "x"}}',
            "reserved member deep": json.dumps(dict(HEALTHY_PROBE, displayPath=good_block())).replace('"powerPlan": "Balanced"', '"powerPlan": "Balanced", "os2": {"PSObject": 1}'),
            "capital exact duplicate": head + ', "DisplayPath": 5, "DisplayPath": {"state": "unknown", "reason": "x"}}',
            "one-element root array": "[" + dup_key_text(dict(HEALTHY_PROBE), [], "displayPath", 5, unknown("x")) + "]",
        }
        controls = {"good": json.dumps(dict(HEALTHY_PROBE, displayPath=good_block())),
                    "reserved member outside the block": head + ', "PSObject": 1}'}
        got = pwsh_json("$in = $env:DVE_LINES | ConvertFrom-Json -AsHashtable\n$out = [ordered]@{}\n"
                        "foreach ($k in @($in.Keys)) { $p = ConvertFrom-DvProbeStdout (\"noise`r`nDVE_PROBE=\" + $in[$k] + \"`r`n\")\n"
                        "  $out[$k] = $(if ($null -eq $p) { 'probe-null' } else { [ordered]@{ hostName = [string]$p.hostName; sessionLocked = $p.sessionLocked; block = (ConvertTo-DvDisplayPath -Probe $p) } }) }\n"
                        "$out | ConvertTo-Json -Compress -Depth 10",
                        {"DVE_LINES": json.dumps(dict(lines, **controls))})
        for label in lines:
            with self.subTest(label):
                self.assertNotEqual(got[label], "probe-null", label)
                self.assertEqual((got[label]["hostName"], got[label]["sessionLocked"]), ("ULTRA-MAGNUS", False), got[label])
                self.assertEqual(got[label]["block"], unknown("probe-block-malformed"), got[label])
        self.assertEqual(got["good"]["block"]["powerPlan"], "Balanced", got["good"])
        # outside the block nothing changes: a probe ConvertFrom-Json cannot read is still no probe
        self.assertEqual(got["reserved member outside the block"], "probe-null", got)

    def test_successor3_w2_a_case_variant_duplicate_or_a_one_element_root_array_is_malformed_in_the_receipt_text(self) -> None:
        """SUCCESSOR-2 adversary A2: the duplicate scope was ordinal and lowercase, so `Venue` / `DisplayPath` duplicates, or the record inside a one-element root array
        (which ConvertFrom-Json unwraps), escaped it and read as the last copy."""
        venue = {"name": "bachelor", "role": "supplementary", "declared": "bachelor", "hostName": "BACHELOR", "gpuNames": ["Intel(R) Iris(R) Xe Graphics"]}
        head = '{"schema": "mlv-app/dual-venue-receipt/v1", "outcome": "UNRESOLVED", "subject": {}, '
        vfields = json.dumps(venue)[1:-1]
        cases = {
            "Venue twice": head + '"Venue": null, "Venue": {' + vfields + ', "DisplayPath": {"state": "unknown", "reason": "x"}}}',
            "DisplayPath state twice": head + '"Venue": {' + vfields + ', "DisplayPath": {"state": "bad", "state": "unknown", "reason": "x"}}}',
            "venue and Venue": head + '"venue": {' + vfields + '}, "VENUE": {' + vfields + ', "displayPath": {"state": "unknown", "reason": "x"}}}',
            "one-element root array with a duplicate": "[" + dup_key_text(receipt_with(good_block()), ["venue", "displayPath"], "sampledBefore", "during-measurement", SAMPLED_BEFORE) + "]",
            "one-element root array": "[" + json.dumps(receipt_with(good_block())) + "]",
        }
        control = json.dumps(receipt_with(good_block()))
        with tempfile.TemporaryDirectory(prefix="dv-displaypath-dup3-") as d:
            path = Path(d) / "texts.json"
            path.write_text(json.dumps(dict(cases, control=control)), encoding="utf-8")
            got = pwsh_json("$in = [IO.File]::ReadAllText($env:DVE_TEXTS_FILE) | ConvertFrom-Json -AsHashtable\n$out = [ordered]@{}\n"
                            "foreach ($k in @($in.Keys)) { try { $r = $(try { $in[$k] | ConvertFrom-Json } catch { $in[$k] | ConvertFrom-Json -AsHashtable }); $v = Test-DvReceiptValid -Receipt $r -ReceiptText $in[$k]\n"
                            "  $out[$k] = [ordered]@{ valid = $v.valid; displayPath = $v.displayPath; reasons = @($v.reasons) } } catch { $out[$k] = [ordered]@{ threw = [string]$_.Exception.Message } } }\n"
                            "$out | ConvertTo-Json -Compress -Depth 5", {"DVE_TEXTS_FILE": str(path)})
        for label in cases:
            with self.subTest(label):
                self.assertEqual((got[label].get("valid"), got[label].get("displayPath")), (False, "malformed"), got[label])
                self.assertTrue(any(r.startswith("DISPLAY_PATH_MALFORMED") for r in got[label]["reasons"]), got[label])
        self.assertEqual((got["control"]["valid"], got["control"]["displayPath"]), (True, "present"), got["control"])

    def test_successor3_w3_every_reader_that_holds_a_receipts_text_passes_it_to_the_validator(self) -> None:
        """SUCCESSOR-2 adversary A5: New-VenueFlavorPair and Invoke-VenueBaseline read each receipt from its file and called Test-DvReceiptValid without -ReceiptText, so
        the duplicate-key check never ran there. Every call in a script that reads the receipt file carries the text (the writer, New-DvReceipt, holds an object only)."""
        for name in ("New-VenueFlavorPair.ps1", "Invoke-VenueBaseline.ps1", "New-VenueSheetPair.ps1"):
            calls = [l for l in (DV / name).read_text(encoding="utf-8").splitlines() if "Test-DvReceiptValid " in l and not l.lstrip().startswith("#")]
            with self.subTest(name):
                self.assertTrue(calls, name)
                for call in calls:
                    self.assertIn("-ReceiptText", call, f"{name}: {call.strip()}")

    def test_successor3_r2_b1_a_one_element_root_array_with_a_display_only_parse_failure_keeps_the_probe(self) -> None:
        """SUCCESSOR-3 r1 sol B1: the W1 recovery read only a root OBJECT, so the same display-only parse failure inside a one-element root array (which
        ConvertFrom-Json unwraps to its record) dropped the probe, and a healthy leg became VENUE_UNHEALTHY. The literal inputs of the verdict."""
        head = json.dumps(dict(HEALTHY_PROBE))[:-1]
        deep = '{"x": ' * 257 + "0" + "}" * 257
        lines = {
            "array, two casings": "[" + head + ', "displayPath": 5, "DisplayPath": {}}]',
            "array, reserved member": "[" + head + ', "displayPath": {"PSObject": 1}}]',
            # r2 adversary A2: nested one-element arrays, with an exact duplicate (which parses, keeping the last) and with a reserved member
            "nested arrays, exact duplicate": "[[" + head + ', "displayPath": 5, "displayPath": {"state": "unknown", "reason": "last"}}]]',
            "nested arrays, reserved member": "[[" + head + ', "displayPath": {"PSObject": 1}}]]',
            # r2 adversary A3: the block that stops ConvertFrom-Json is deeper than 256 levels
            "deep reserved member": head + ', "displayPath": {"PSObject": 1, "x": ' + deep + "}}",
        }
        controls = {"array, reserved member outside the block": "[" + head + ', "PSObject": 1}]',
                    "array of two probes, two casings": "[" + head + ', "displayPath": 5, "DisplayPath": {}}, ' + json.dumps(dict(HEALTHY_PROBE)) + "]"}
        got = pwsh_json("$in = $env:DVE_LINES | ConvertFrom-Json -AsHashtable\n$out = [ordered]@{}\n"
                        "foreach ($k in @($in.Keys)) { $p = ConvertFrom-DvProbeStdout (\"noise`r`nDVE_PROBE=\" + $in[$k] + \"`r`n\")\n"
                        "  $out[$k] = $(if ($null -eq $p) { 'probe-null' } else { [ordered]@{ hostName = [string]$p.hostName; sessionLocked = $p.sessionLocked; pwshColdStartMs = $p.pwshColdStartMs;\n"
                        "    freeDiskGiB = $p.freeDiskGiB; block = (ConvertTo-DvDisplayPath -Probe $p) } }) }\n"
                        "$out | ConvertTo-Json -Compress -Depth 10",
                        {"DVE_LINES": json.dumps(dict(lines, **controls))})
        for label in lines:
            with self.subTest(label):
                self.assertNotEqual(got[label], "probe-null", label)
                self.assertEqual((got[label]["hostName"], got[label]["sessionLocked"], got[label]["pwshColdStartMs"], got[label]["freeDiskGiB"]),
                                 ("ULTRA-MAGNUS", False, 500, 600.0), got[label])
                self.assertEqual(got[label]["block"], unknown("probe-block-malformed"), got[label])
        # the probe's own fields still decide: a reserved member outside the block, or a line that is not one probe, is still no probe
        self.assertEqual(got["array, reserved member outside the block"], "probe-null", got)
        self.assertEqual(got["array of two probes, two casings"], "probe-null", got)

    def _b2_receipt_text(self, venue_tail: str, **over) -> str:
        """A receipt FILE's text whose venue ends in `venue_tail` (raw JSON members: a case collision or a reserved member json.dumps cannot write)."""
        r = {"schema": "mlv-app/dual-venue-receipt/v1", "receiptId": over.pop("receiptId", "rcpt-b2"), "legId": "m16-1243-look-scale2", "card": "DUAL-VENUE-EVIDENCE-1",
             "outcome": over.pop("outcome", "PASS"), "startedUtc": over.pop("startedUtc", "2026-11-25T00:00:00Z"),
             "subject": {"backend": "cpu", "buildManifestSha256": "ab" * 32, "clipId": "M16-1243", "lookFlavor": "classic"},
             "metrics": {"sourceCommit": "c" * 40}, "scale": {"requestedScale": 2, "effectiveScale": 2},
             "venue": {"name": "bachelor", "role": "supplementary", "declared": "bachelor", "hostName": "BACHELOR", "gpuNames": ["Intel(R) Iris(R) Xe Graphics"]}}
        for k, v in over.items():
            r["subject"][k] = v
        if venue_tail.startswith("ROOT:"):   # a second venue member, in another letter case, after the first (the sweep's sibling: `venue` beside `Venue`)
            return json.dumps(r)[:-1] + ", " + venue_tail[len("ROOT:"):] + "}"
        return json.dumps(r)[:-2] + ", " + venue_tail + "}}"

    B2_TAILS = {"two casings": '"displayPath": 5, "DisplayPath": {"state": "unknown", "reason": "x"}',
                "reserved member": '"displayPath": {"PSObject": 1}',
                "venue and Venue": 'ROOT:"Venue": {"name": "bachelor", "role": "supplementary", "declared": "bachelor", "hostName": "BACHELOR", '
                                   '"displayPath": {"state": "unknown", "reason": "x"}}'}

    def _assert_typed_display_refusal(self, proc, label: str) -> None:
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("PAIR_RECEIPT_INVALID", out, f"{label}: {out}")
        self.assertIn("DISPLAY_PATH_MALFORMED", out, f"{label}: {out}")
        self.assertNotIn("KeysWithDifferentCasing", out, f"{label}: {out}")
        self.assertNotIn("different casing", out.lower(), f"{label}: {out}")

    def test_successor3_r2_b2_sheet_pair_reads_a_case_colliding_receipt_file_to_the_typed_validator(self) -> None:
        """SUCCESSOR-3 r1 sol B2 (New-VenueSheetPair.ps1:41-42): the initial ConvertFrom-Json threw on a case collision inside venue.displayPath before the
        validator ran. End to end through the script and the receipt FILES: the receipt is INVALID with DISPLAY_PATH_MALFORMED."""
        for label, tail in self.B2_TAILS.items():
            with self.subTest(label), tempfile.TemporaryDirectory(prefix="dv-b2-sheet-") as d:
                base = Path(d) / ".claude-state"
                base.mkdir()
                cuda = base / "cuda.json"
                cpu = base / "cpu.json"
                cuda.write_text(self._b2_receipt_text(tail, receiptId="rc-cuda", backend="cuda"), encoding="utf-8")
                cpu.write_text(self._b2_receipt_text(tail, receiptId="rc-cpu"), encoding="utf-8")
                proc = run_pwsh(["-File", str(DV / "New-VenueSheetPair.ps1"), "-CudaReceipt", str(cuda), "-CpuReceipt", str(cpu), "-OutDir", str(base / "sheets")])
                self._assert_typed_display_refusal(proc, label)
                # r2 adversary A7: on a no-signal outcome too, the malformed block is refused as itself, never PAIR_RECEIPT_INCOMPLETE first
                cuda.write_text(self._b2_receipt_text(tail, receiptId="rc-cuda", backend="cuda", outcome="UNRESOLVED"), encoding="utf-8")
                proc = run_pwsh(["-File", str(DV / "New-VenueSheetPair.ps1"), "-CudaReceipt", str(cuda), "-CpuReceipt", str(cpu), "-OutDir", str(base / "sheets")])
                self._assert_typed_display_refusal(proc, label + " UNRESOLVED")
        # control: a no-signal receipt with a good block is still PAIR_RECEIPT_INCOMPLETE
        with tempfile.TemporaryDirectory(prefix="dv-b2-sheet-") as d:
            base = Path(d) / ".claude-state"
            base.mkdir()
            cuda = base / "cuda.json"
            cpu = base / "cpu.json"
            good = '"displayPath": ' + json.dumps(good_block())
            cuda.write_text(self._b2_receipt_text(good, receiptId="rc-cuda", backend="cuda", outcome="UNRESOLVED"), encoding="utf-8")
            cpu.write_text(self._b2_receipt_text(good, receiptId="rc-cpu"), encoding="utf-8")
            proc = run_pwsh(["-File", str(DV / "New-VenueSheetPair.ps1"), "-CudaReceipt", str(cuda), "-CpuReceipt", str(cpu), "-OutDir", str(base / "sheets")])
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("PAIR_RECEIPT_INCOMPLETE receipt rc-cuda is UNRESOLVED", proc.stdout + proc.stderr)

    def test_successor3_r2_b2_flavor_pair_and_trio_read_a_case_colliding_receipt_file_to_the_typed_validator(self) -> None:
        """SUCCESSOR-3 r1 sol B2 (New-VenueFlavorPair.ps1:156-157, :173 for the trio's third receipt): the same, end to end through the script."""
        for label, tail in self.B2_TAILS.items():
            for trio in (False, True):
                with self.subTest(label=label, trio=trio), tempfile.TemporaryDirectory(prefix="dv-b2-flavor-") as d:
                    base = Path(d) / ".claude-state"
                    base.mkdir()
                    paths = {}
                    for flavor in ("classic", "cinematic", "film"):
                        paths[flavor] = base / f"{flavor}.json"
                        # r2 adversary A7: the trio's classic receipt is a no-signal outcome; the malformed block is still refused as itself
                        paths[flavor].write_text(self._b2_receipt_text(tail, receiptId=f"rc-{flavor}", lookFlavor=flavor,
                                                                       outcome="UNRESOLVED" if trio and flavor == "classic" else "PASS"), encoding="utf-8")
                    args = ["-File", str(DV / "New-VenueFlavorPair.ps1"), "-ClassicReceipt", str(paths["classic"]), "-CinematicReceipt", str(paths["cinematic"]),
                            "-OutDir", str(base / "pair")]
                    if trio:
                        args += ["-FilmReceipt", str(paths["film"])]
                    self._assert_typed_display_refusal(run_pwsh(args), label)

    def test_successor3_r2_pass3_a8_a_later_sides_malformed_block_is_not_hidden_by_an_earlier_sides_outcome_gate(self) -> None:
        """r2 pass-3 adversary A8: every side is validated before any side's outcome gate, so a malformed block on the SECOND (or third) side of a no-signal pair
        is refused as itself, never as the first side's PAIR_RECEIPT_INCOMPLETE."""
        good = '"displayPath": ' + json.dumps(good_block())
        bad = '"displayPath": 5'
        with tempfile.TemporaryDirectory(prefix="dv-a8-sheet-") as d:
            base = Path(d) / ".claude-state"
            base.mkdir()
            cuda = base / "cuda.json"
            cpu = base / "cpu.json"
            cuda.write_text(self._b2_receipt_text(good, receiptId="rc-cuda", backend="cuda", outcome="UNRESOLVED"), encoding="utf-8")
            cpu.write_text(self._b2_receipt_text(bad, receiptId="rc-cpu", outcome="UNRESOLVED"), encoding="utf-8")
            proc = run_pwsh(["-File", str(DV / "New-VenueSheetPair.ps1"), "-CudaReceipt", str(cuda), "-CpuReceipt", str(cpu), "-OutDir", str(base / "sheets")])
            self._assert_typed_display_refusal(proc, "sheet pair, cpu side")
        for trio in (False, True):
            with self.subTest(trio=trio), tempfile.TemporaryDirectory(prefix="dv-a8-flavor-") as d:
                base = Path(d) / ".claude-state"
                base.mkdir()
                bad_side = "film" if trio else "cinematic"
                paths = {}
                for flavor in ("classic", "cinematic", "film"):
                    paths[flavor] = base / f"{flavor}.json"
                    paths[flavor].write_text(self._b2_receipt_text(bad if flavor == bad_side else good, receiptId=f"rc-{flavor}", lookFlavor=flavor,
                                                                   outcome="UNRESOLVED"), encoding="utf-8")
                args = ["-File", str(DV / "New-VenueFlavorPair.ps1"), "-ClassicReceipt", str(paths["classic"]), "-CinematicReceipt", str(paths["cinematic"]),
                        "-OutDir", str(base / "pair")]
                if trio:
                    args += ["-FilmReceipt", str(paths["film"])]
                self._assert_typed_display_refusal(run_pwsh(args), f"flavor {'trio' if trio else 'pair'}, {bad_side} side")

    def test_successor3_r2_b2_baseline_reads_a_case_colliding_receipt_file_to_the_typed_validator(self) -> None:
        """SUCCESSOR-3 r1 sol B2 (Invoke-VenueBaseline.ps1:346): Get-RowFigures caught the parse exception and returned its empty row, so the receipt read as
        missing or unreadable. The shipped function, run on a receipt FILE: the row's verification is INVALID with DISPLAY_PATH_MALFORMED and the figures are rejected."""
        for label, tail in dict(self.B2_TAILS, control="\"displayPath\": " + json.dumps(good_block())).items():
            with self.subTest(label), tempfile.TemporaryDirectory(prefix="dv-b2-baseline-") as d:
                path = Path(d) / "rc-base.json"
                path.write_text(self._b2_receipt_text(tail, receiptId="rc-base", startedUtc="__NOW__"), encoding="utf-8")
                script = (f"Import-Module '{MODULE}' -Force\n"
                          f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{DV / 'Invoke-VenueBaseline.ps1'}', [ref]$null, [ref]$null)\n"
                          "$fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Get-RowFigures' }, $true)\n"
                          ". ([scriptblock]::Create($fn.Extent.Text))\n"
                          f"$Venue = 'bachelor'; $manifest = '{'ab' * 32}'; $repo = '{ROOT}'; $ValidatorScript = ''; $startedUtc = [DateTime]::UtcNow.AddSeconds(-2)\n"
                          "$p = $env:DVE_RECEIPT_FILE\n"
                          "[IO.File]::WriteAllText($p, [IO.File]::ReadAllText($p).Replace('__NOW__', [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')))\n"
                          "$f = Get-RowFigures $p ([pscustomobject]@{ legId = 'm16-1243-look-scale2' }) 'cpu'\n"
                          "Write-Output ('DVE_OUT=' + ([ordered]@{ identity = $f.identity; rejected = $f.rejected; verification = $f.verification } | ConvertTo-Json -Compress -Depth 5))\n")
                proc = run_pwsh(["-Command", script], env_extra={"DVE_RECEIPT_FILE": str(path)})
                line = next((l for l in proc.stdout.splitlines() if l.startswith("DVE_OUT=")), None)
                self.assertIsNotNone(line, proc.stdout + proc.stderr)
                got = json.loads(line[len("DVE_OUT="):])
                self.assertIsNone(got["identity"], got)
                self.assertIsNotNone(got["verification"], f"{label}: the receipt read as missing: {got}")
                self.assertEqual((got["verification"]["status"], got["rejected"]), ("INVALID", "INVALID"), got)
                if label == "control":
                    self.assertFalse(any(r.startswith("DISPLAY_PATH_MALFORMED") for r in got["verification"]["reasons"]), got)
                else:
                    self.assertTrue(got["verification"]["reasons"][0].startswith("DISPLAY_PATH_MALFORMED"), got)

    def test_successor3_r2_b2_the_receipt_text_reader_recovers_only_the_display_path(self) -> None:
        """ConvertFrom-DvReceiptText: a parse failure anywhere but venue.displayPath (or the venue that holds it) stays the parser's own exception, and a good
        receipt reads exactly as ConvertFrom-Json reads it."""
        cases = {
            "outcome twice": self._b2_receipt_text('"displayPath": ' + json.dumps(good_block())).replace('"outcome": "PASS"', '"outcome": "PASS", "Outcome": "FAIL"'),
            "venue name twice": self._b2_receipt_text('"Name": "x"'),
            "both": self._b2_receipt_text('"displayPath": 5, "DisplayPath": {}').replace('"outcome": "PASS"', '"outcome": "PASS", "Outcome": "FAIL"'),
            "good": self._b2_receipt_text('"displayPath": ' + json.dumps(good_block())),
            "two casings": self._b2_receipt_text(self.B2_TAILS["two casings"]),
            # r2 adversary A2 / A3: nested one-element arrays, and a block deeper than 256 levels, still reach the typed verdict
            "nested arrays, reserved member": "[[" + self._b2_receipt_text(self.B2_TAILS["reserved member"]) + "]]",
            "deep reserved member": self._b2_receipt_text('"displayPath": {"PSObject": 1, "x": ' + '{"x": ' * 257 + "0" + "}" * 257 + "}"),
            # r2 pass-3 adversary A7: a non-object Venue kept after a good venue is the validator's malformed venue, never the raw parser error; a dropped venue
            # copy whose own fields collide is the parser's failure, never blamed on the display path
            "Venue null after a good venue": self._b2_receipt_text('"displayPath": ' + json.dumps(good_block()))[:-1] + ', "Venue": null}',
            "dropped venue with its own collision": self._b2_receipt_text('"Field": 1, "field": 2')[:-1] + ', "Venue": {"name": "bachelor"}}',
        }
        got = pwsh_json("$in = $env:DVE_TEXTS | ConvertFrom-Json -AsHashtable\n$out = [ordered]@{}\n"
                        "foreach ($k in @($in.Keys)) { try { $r = ConvertFrom-DvReceiptText -Text $in[$k]; $v = Test-DvReceiptValid -Receipt $r -ReceiptText $in[$k]\n"
                        "  $out[$k] = [ordered]@{ outcome = [string]$r.outcome; venue = [string]$r.venue.name; displayPath = $v.displayPath } } catch { $out[$k] = 'threw' } }\n"
                        "$out | ConvertTo-Json -Compress -Depth 5", {"DVE_TEXTS": json.dumps(cases)})
        for label in ("outcome twice", "venue name twice", "both", "dropped venue with its own collision"):
            self.assertEqual(got[label], "threw", f"{label}: {got[label]}")
        self.assertEqual(got["Venue null after a good venue"], {"outcome": "PASS", "venue": "", "displayPath": "malformed"}, got)
        self.assertEqual(got["good"], {"outcome": "PASS", "venue": "bachelor", "displayPath": "present"}, got)
        for label in ("two casings", "nested arrays, reserved member", "deep reserved member"):
            self.assertEqual(got[label], {"outcome": "PASS", "venue": "bachelor", "displayPath": "malformed"}, f"{label}: {got[label]}")

    def test_successor3_r2_b2_no_receipt_file_reader_parses_the_text_outside_the_display_path_reader(self) -> None:
        """SUCCESSOR-3 r1 sol B2, the class: every receipt file the three validating readers read is parsed by ConvertFrom-DvReceiptText, never piped straight
        into ConvertFrom-Json, so a display-only parse failure always reaches the validator."""
        pattern = re.compile(r"(rcText|receiptTexts\.\w+|cudaText|cpuText)\s*\|\s*ConvertFrom-Json")
        for name in ("New-VenueFlavorPair.ps1", "Invoke-VenueBaseline.ps1", "New-VenueSheetPair.ps1"):
            text = (DV / name).read_text(encoding="utf-8")
            with self.subTest(name):
                self.assertEqual(pattern.findall(text), [], name)
                self.assertIn("ConvertFrom-DvReceiptText", text, name)


@requires_windows_pwsh
class Successor2VenueLegTests(RunnerHarness, unittest.TestCase):
    """SUCCESSOR-2 W3 (pass-2 A4): a probe that printed no DVE_PROBE line, or never returned, leaves the typed unknown on the receipt, never absent."""

    def setUp(self) -> None:
        self.make_harness()

    def test_w3_a_probe_with_no_dve_probe_line_is_typed_unknown_not_absent(self) -> None:
        self.um.write_text(STUB_UM_RUN.replace("return [pscustomobject]@{ exitCode = 0; stdout = ('DVE_PROBE=' + ($cfg.probe | ConvertTo-Json -Compress)) }",
                                               "return [pscustomobject]@{ exitCode = 1; stdout = '' }"), encoding="utf-8")
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=dict(HEALTHY_PROBE))
        self.assertEqual(receipt["outcome"], "VENUE_UNHEALTHY", receipt)
        self.assertEqual(receipt["venue"]["displayPath"], unknown("probe-did-not-report"), receipt["venue"])

    def test_w3_a_probe_that_never_returned_is_typed_unknown_not_absent(self) -> None:
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=dict(HEALTHY_PROBE), health_mode="unresolved")
        self.assertEqual(receipt["venue"]["displayPath"], unknown("probe-did-not-report"), receipt["venue"])

    def test_w3_a_leg_refused_before_the_probe_says_probe_not_run(self) -> None:
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=dict(HEALTHY_PROBE), gen_refusal="PLAYBACK_ATTR3_OWNER_NOT_CONSENTED the table has no such clip")
        self.assertEqual(receipt["venue"]["displayPath"], unknown("probe-not-run"), receipt["venue"])


if __name__ == "__main__":
    unittest.main()
