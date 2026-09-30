"""UM-DISPLAY-QT-WINDOWS-MAPPING-PROOF-1: the Windows monitor -> Qt screen mapping, pinned to a RECORDING.

PR #191 (UM-DISPLAY-SELECT-AND-LOG-1) shipped with a disclosed-unproven premise: that QScreen::name()
on Windows equals the GDI device name (\\\\.\\DISPLAYn), so a job-resolved '\\\\.\\DISPLAY1' preference
could be compared with it. On 2026-09-30 a UM job ran a Qt 6.10.2 probe in interactive session 1 next to
the Windows inventory and recorded both (tests/fixtures/display/um-qscreen-windows-mapping-20260930.txt).
The premise FAILED there: QScreen::name() is the EDID friendly name ("PA329C", "LG TV"); only a monitor
with no EDID name (a VM's virtual display) reports the GDI name.

This file pins, against that recording and by EXECUTING the real shared parser / resolver (never a
reimplementation):
  - the measured fact (no Qt name is a GDI name; every Qt screen is corroborated by two independent
    Windows views: QueryDisplayConfig and WMI EDID),
  - the app's derived per-screen GDI device (the new `device=` field) is the corroborated one,
  - the resolver's hand-off ('PA329C' -> '\\\\.\\DISPLAY1') against the recorded inventory,
  - the C++ unit test's constants equal the recording (they cannot drift),
  - an unmatched or ambiguous mapping yields UNKNOWN, never a wrong screen.
The C++ matcher itself is unit-tested in tests/console/test_display_device_mapping.cpp.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
SHARED = ROOT / "tools" / "profiling" / "gui-smoke-display-identity.ps1"
FIXTURE = ROOT / "tests" / "fixtures" / "display" / "um-qscreen-windows-mapping-20260930.txt"
CPP_TEST = ROOT / "tests" / "console" / "test_display_device_mapping.cpp"

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")

D1 = "\\\\.\\DISPLAY1"
D2 = "\\\\.\\DISPLAY2"


def _sections() -> dict[str, str]:
    text = FIXTURE.read_text(encoding="utf-8").replace("\r\n", "\n")
    return {m.group(1): m.group(2) for m in re.finditer(r"=====BEGIN (\w+)=====\n(.*?)\n=====END \1=====", text, re.S)}


def _json_section(name: str):
    return json.loads(_sections()[name])


def _qt_screen_lines() -> list[str]:
    return [ln for ln in _sections()["QT_PROBE"].split("\n") if ln.startswith("gui_smoke.display_screen ")]


class _PwshCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="um-display-mapping-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _run(self, body: str) -> subprocess.CompletedProcess:
        script = self.tmp / "probe.ps1"
        script.write_text("$ErrorActionPreference = 'Stop'\n" + body, encoding="utf-8")
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True,
        )

    def _json(self, body: str):
        proc = self._run(body)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def parse_log(self, lines: list[str]) -> dict:
        log = self.tmp / "smoke.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self._json(
            f". '{SHARED}'\n"
            f"$sel = ConvertFrom-GuiSmokeDisplayLog -LogText ([IO.File]::ReadAllText('{log}'))\n"
            "ConvertTo-Json -InputObject $sel -Depth 6 -Compress\n"
        )

    def identity(self, lines: list[str]) -> dict:
        log = self.tmp / "smoke.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self._json(
            f". '{SHARED}'\n"
            f"$sel = ConvertFrom-GuiSmokeDisplayLog -LogText ([IO.File]::ReadAllText('{log}'))\n"
            "ConvertTo-Json -InputObject (Get-GuiSmokeDisplayIdentity -Selection $sel) -Depth 6 -Compress\n"
        )


def _placement(name: str, physical: str = "3840x2160") -> str:
    return (
        f'gui_smoke.window_placement mode=fullscreen screen="{name}" verified=1 window=0,0 2560x1440 '
        f'preview={physical} target_screen="{name}" presentation_screen="{name}" presentation_physical={physical}'
    )


def _target(name: str) -> str:
    return f'gui_smoke.display_target screen="{name}" reason=max_physical_pixels_tie_preferred candidates=2 fallback=1'


@requires_pwsh
class MeasuredPremiseTests(_PwshCase):
    """The recording itself: what Qt reported, cross-checked against two independent Windows views."""

    def test_no_qt_screen_name_is_a_gdi_device_name_on_the_recorded_um_session(self) -> None:
        # MUTATION CAUGHT: restoring the #191 assumption in the fixture reading ('name IS \\.\DISPLAYn').
        sel = self.parse_log(_qt_screen_lines())
        names = [s["name"] for s in sel["screens"]]
        self.assertEqual(names, ["LG TV", "PA329C"])
        for name in names:
            self.assertFalse(name.startswith("\\\\.\\"), name)

    def test_the_recording_was_taken_in_an_interactive_session_on_um_with_two_active_monitors(self) -> None:
        ctx = _sections()["CONTEXT"]
        self.assertIn("computer=ULTRA-MAGNUS", ctx)
        self.assertIn("session_id=1", ctx)
        self.assertIn("sm_cmonitors=2", ctx)
        self.assertIn("qscreen_probe.qt_version 6.10.2", _sections()["QT_PROBE"])

    def test_every_derived_device_is_corroborated_by_queryDisplayConfig_wmi_and_the_enumerated_position(self) -> None:
        sel = self.parse_log(_qt_screen_lines())
        enum = {r["deviceName"]: r for r in _json_section("ENUM_DISPLAY_DEVICES") if r["attached"]}
        config = {r["gdiName"]: r for r in _json_section("QUERY_DISPLAY_CONFIG")}
        wmi = {r["serialNumberId"]: r for r in _json_section("WMI_MONITOR_ID")}
        self.assertEqual(len(sel["screens"]), 2)
        for s in sel["screens"]:
            with self.subTest(screen=s["name"]):
                device = s["device"]
                # (1) the enumerated Windows adapter sits where Qt says the screen's native origin is
                row = enum[device]
                self.assertEqual((row["posX"], row["posY"]), (s["geometryX"], s["geometryY"]))
                self.assertEqual((row["width"], row["height"]), (s["physicalWidth"], s["physicalHeight"]))
                # (2) QueryDisplayConfig pairs that GDI name with the very friendly name Qt reported
                self.assertEqual(config[device]["friendlyName"], s["name"])
                # (3) WMI EDID for the Qt-reported serial names the same monitor, same EDID product code
                w = wmi[s["serial"]]
                self.assertEqual(w["userFriendlyName"], s["name"])
                self.assertEqual(w["productCodeId"], config[device]["edidProductCodeHex"])
                # ...and its instance is the same PnP node QueryDisplayConfig's target path names
                instance = w["instanceName"].rsplit("_", 1)[0].split("\\")
                path = config[device]["devicePath"].split("#")
                self.assertEqual(instance[1:3], path[1:3])

    def test_the_app_derived_device_field_is_present_and_unique_on_every_recorded_screen(self) -> None:
        sel = self.parse_log(_qt_screen_lines())
        by_name = {s["name"]: s["device"] for s in sel["screens"]}
        self.assertEqual(by_name, {"PA329C": D1, "LG TV": D2})

    def test_the_apps_own_preference_rule_run_in_the_um_session_selects_exactly_the_intended_screen(self) -> None:
        # The probe evaluated DisplayDeviceMapping::preferenceMatchedField (the function MainWindow.cpp
        # calls) for each --display-prefer value on the real UM screens. MUTATION CAUGHT: any regression
        # to "compare the device preference with QScreen::name()" (no screen would match '\\.\DISPLAY1').
        matches: dict[tuple[str, str], str] = {}
        for ln in _sections()["QT_PROBE"].split("\n"):
            m = re.match(r'qscreen_probe\.prefer_match index=\d+ name="([^"]*)" prefer="([^"]*)" matched="([^"]*)"', ln)
            if m:
                matches[(m.group(2), m.group(1))] = m.group(3)
        self.assertEqual(len(matches), 10)  # 5 preference values x 2 screens
        selected = lambda prefer: sorted((n, f) for (p, n), f in matches.items() if p == prefer and f)
        self.assertEqual(selected(D1), [("PA329C", "device_name")])   # what the resolver hands over for the ASUS
        self.assertEqual(selected(D2), [("LG TV", "device_name")])
        self.assertEqual(selected("PA329C"), [("PA329C", "name")])    # the pre-#191-round-2 substring path
        self.assertEqual(selected("\\\\.\\DISPLAY9"), [])              # a device that is not attached selects nothing
        self.assertEqual(selected("\\\\.\\DISPLAY"), [])               # a device PREFIX is not a device name

    def test_the_cpp_unit_test_constants_equal_the_recording(self) -> None:
        # The C++ suite runs without the fixture, so its umMonitors() copy must not drift from it.
        cpp = CPP_TEST.read_text(encoding="utf-8")
        found = {
            f"\\\\.\\DISPLAY{n}": tuple(int(v) for v in rect)
            for n, *rect in re.findall(
                r'QStringLiteral\("\\\\\\\\\.\\\\DISPLAY(\d)"\), QRect\( (\d+), (\d+), (\d+), (\d+) \)', cpp)
        }
        recorded = {
            r["deviceName"]: (r["posX"], r["posY"], r["width"], r["height"])
            for r in _json_section("ENUM_DISPLAY_DEVICES") if r["attached"]
        }
        self.assertEqual(found, recorded)
        sel = self.parse_log(_qt_screen_lines())
        for s in sel["screens"]:
            self.assertIn(
                f'QPoint( {s["geometryX"]}, {s["geometryY"]} ), QSize( {s["physicalWidth"]}, {s["physicalHeight"]} )', cpp)


@requires_pwsh
class ResolverHandOffTests(_PwshCase):
    """Resolve-AttrCudaPreferredDisplay against the RECORDED Windows inventory (shape of
    Get-AttrCudaWindowsDisplayInventory: deviceName + monitorName = the monitor's DeviceString)."""

    def resolve(self, substring: str) -> dict:
        rows = self.tmp / "enum.json"
        rows.write_text(json.dumps(_json_section("ENUM_DISPLAY_DEVICES")), encoding="utf-8")
        return self._json(
            f"Import-Module '{MODULE}' -Force\n"
            f"$rows = @(Get-Content -Raw '{rows}' | ConvertFrom-Json)\n"
            "$devices = @($rows | Where-Object { $_.attached } | ForEach-Object {\n"
            "    [pscustomobject]@{ deviceName = $_.deviceName; monitorName = $_.monitors[0].deviceString } })\n"
            "$inv = [pscustomobject]@{ collected = $true; devices = $devices; error = $null }\n"
            f"ConvertTo-Json -InputObject (Resolve-AttrCudaPreferredDisplay -WindowsInventory $inv -Substring '{substring}') -Compress\n"
        )

    def test_the_venue_preference_resolves_to_the_asus_device_the_app_now_derives_for_the_asus_screen(self) -> None:
        r = self.resolve("PA329C")
        self.assertEqual((r["status"], r["argument"], r["deviceName"]), ("mapped", D1, D1))
        # ...and that device IS the one the app derives for the Qt screen named "PA329C".
        sel = self.parse_log(_qt_screen_lines())
        derived = {s["name"]: s["device"] for s in sel["screens"]}
        self.assertEqual(r["argument"], derived["PA329C"])
        self.assertNotEqual(r["argument"], derived["LG TV"])

    def test_a_preference_that_names_no_recorded_monitor_stays_absent_and_keeps_the_substring(self) -> None:
        r = self.resolve("NOSUCHMONITOR")
        self.assertEqual((r["status"], r["argument"]), ("absent", "NOSUCHMONITOR"))


@requires_pwsh
class ParserAndIdentityTests(_PwshCase):
    def test_the_shared_parser_reads_the_device_field_and_a_legacy_line_reads_null(self) -> None:
        legacy = _qt_screen_lines()[0].rsplit(' device="', 1)[0]
        self.assertNotIn("device=", legacy)
        sel = self.parse_log([legacy] + _qt_screen_lines()[1:])
        self.assertIsNone(sel["screens"][0]["device"])
        self.assertEqual(sel["screens"][1]["device"], D1)

    def test_an_empty_device_field_is_not_a_known_device(self) -> None:
        line = _qt_screen_lines()[1].replace(f'device="{D1}"', 'device=""')
        sel = self.parse_log([line])
        self.assertEqual(sel["screens"][0]["device"], "")

    def test_the_recorded_um_presentation_on_the_asus_yields_that_monitors_identity(self) -> None:
        identity = self.identity(_qt_screen_lines() + [_target("PA329C"), _placement("PA329C")])
        self.assertIsNone(identity["identityUnknownReason"])
        self.assertEqual(identity["presentationScreenName"], "PA329C")
        self.assertEqual(identity["presentationManufacturer"], "ASUSTek COMPUTER INC")
        self.assertEqual(identity["presentationModel"], "PA329C")
        self.assertEqual(identity["presentationSerial"], "M1LMQSXXXXXX")
        self.assertEqual((identity["physicalWidth"], identity["physicalHeight"]), (3840, 2160))
        self.assertEqual(identity["refreshHzRounded"], 60)

    def test_the_old_gdi_name_assumption_against_the_real_um_log_is_unknown_never_the_wrong_screen(self) -> None:
        # A placement that still names '\\.\DISPLAY1' (what #191 assumed Qt would log) matches NO recorded
        # display_screen name: identity UNKNOWN, not "the first screen" and not the LG TV.
        identity = self.identity(_qt_screen_lines() + [_target(D1), _placement(D1)])
        self.assertIn("no matching gui_smoke.display_screen line", identity["identityUnknownReason"])
        self.assertIsNone(identity["presentationScreenName"])
        self.assertIsNone(identity["presentationSerial"])
        self.assertFalse(identity["verified"])

    def test_two_attached_screens_sharing_a_name_are_unknown_because_the_name_cannot_tell_them_apart(self) -> None:
        # Friendly names are not unique (two monitors of one model): a name-keyed lookup would return
        # whichever record came last -- possibly the OTHER monitor's serial and refresh. MUTATION CAUGHT:
        # reverting Find-GuiSmokeDisplayScreen to "last record with this name wins" across distinct indexes.
        a, b = _qt_screen_lines()[1], _qt_screen_lines()[1]
        b = b.replace("index=1", "index=2").replace("M1LMQSXXXXXX", "M1LMQSYYYYYY").replace(
            "geometry=3840,0", "geometry=6400,0").replace(f'device="{D1}"', f'device="\\\\.\\DISPLAY3"')
        identity = self.identity([a, b, _target("PA329C"), _placement("PA329C")])
        self.assertIn("shared by 2 attached screens", identity["identityUnknownReason"])
        self.assertIsNone(identity["presentationSerial"])
        self.assertIsNone(identity["presentationModel"])
        self.assertFalse(identity["verified"])
        found = self._json(
            f". '{SHARED}'\n"
            f"$sel = ConvertFrom-GuiSmokeDisplayLog -LogText ([IO.File]::ReadAllText('{self.tmp / 'smoke.log'}'))\n"
            "$r = Find-GuiSmokeDisplayScreen -Screens $sel.screens -Name 'PA329C'\n"
            "ConvertTo-Json -InputObject ([pscustomobject]@{ isNull = ($null -eq $r) }) -Compress\n"
        )
        self.assertTrue(found["isNull"])

    def test_one_screen_logged_twice_at_the_same_index_is_still_last_wins_not_ambiguous(self) -> None:
        # The round-4 rule (fable DISPLAY-BLOCK-LOOKUP-LAST-WINS-1) is unchanged for a re-logged screen.
        first = _qt_screen_lines()[1].replace("refresh_hz=59.997", "refresh_hz=30.000")
        identity = self.identity([first, _qt_screen_lines()[1], _target("PA329C"), _placement("PA329C")])
        self.assertIsNone(identity["identityUnknownReason"])
        self.assertEqual(identity["refreshHzRounded"], 60)


JOB_SCRIPT = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"


def _extract_function(text: str, name: str) -> str:
    start = text.index(f"function {name} {{")
    depth = 0
    i = text.index("{", start)
    j = i
    while True:
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return text[start:j + 1]


@requires_pwsh
class JobPresentationDeviceMappingTests(_PwshCase):
    """The attribution job's degraded verdict needs the presented screen mapped to a Windows device
    whose mode was readable (round 2 BLOCKER b). It used to match the Qt NAME against the GDI device
    name, which on UM ("PA329C" vs "\\\\.\\DISPLAY1") never matched: the verdict was permanently
    'unknown'. It now maps by the app's derived `device`, with the name as a fallback only when the
    name itself is a GDI device name -- and anything else stays 'unmapped' / 'unknown'."""

    def block(self, lines: list[str], venue: str = "ultra-magnus") -> dict:
        log = self.tmp / "smoke.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        enum = self.tmp / "enum.json"
        enum.write_text(json.dumps(_json_section("ENUM_DISPLAY_DEVICES")), encoding="utf-8")
        job_text = JOB_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
        build = _extract_function(job_text, "Build-AttrCudaDisplayBlock")
        return self._json(
            f"Import-Module '{MODULE}' -Force\n"
            f". '{SHARED}'\n"
            + build + "\n"
            f"$rows = @(Get-Content -Raw '{enum}' | ConvertFrom-Json)\n"
            "$devices = @($rows | Where-Object { $_.attached } | ForEach-Object {\n"
            "    [pscustomobject]@{ deviceName = $_.deviceName; monitorName = $_.monitors[0].deviceString\n"
            "        modeCollected = $_.modeCollected; width = $_.width; height = $_.height; refreshHz = $_.refreshHz } })\n"
            "$inv = [pscustomobject]@{ collected = $true; devices = $devices; error = $null }\n"
            f"$sel = Get-AttrCudaGuiSmokeDisplaySelection -LogText ([IO.File]::ReadAllText('{log}'))\n"
            f"$b = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue '{venue}' -ExpectedWidth 3840 -ExpectedHeight 2160 -AppSelection $sel\n"
            "ConvertTo-Json -InputObject $b -Depth 8 -Compress\n"
        )

    def test_the_recorded_um_presentation_on_the_asus_maps_to_its_windows_device_and_the_verdict_is_known(self) -> None:
        b = self.block(_qt_screen_lines() + [_target("PA329C"), _placement("PA329C")])
        self.assertEqual(b["presentationWindowsDevice"]["status"], "mapped")
        self.assertEqual(b["presentationWindowsDevice"]["deviceName"], D1)
        self.assertEqual(b["presentationWindowsDevice"]["monitorName"], "ASUS PA329C(DisplayPort)")
        self.assertIs(b["displayDegraded"], False)  # 3840x2160 presented vs 3840x2160 expected

    def test_the_recorded_um_presentation_on_the_lg_tv_maps_to_the_other_device(self) -> None:
        b = self.block(_qt_screen_lines() + [_target("LG TV"), _placement("LG TV")])
        self.assertEqual(b["presentationWindowsDevice"]["status"], "mapped")
        self.assertEqual(b["presentationWindowsDevice"]["deviceName"], D2)
        self.assertEqual(b["presentationWindowsDevice"]["monitorName"], "Generic PnP Monitor")

    def test_an_unestablished_device_is_unmapped_and_the_verdict_unknown_never_a_guess_by_name(self) -> None:
        lines = [ln.replace(f'device="{D1}"', 'device=""') for ln in _qt_screen_lines()]
        b = self.block(lines + [_target("PA329C"), _placement("PA329C")])
        self.assertEqual(b["presentationWindowsDevice"]["status"], "unmapped")
        self.assertIsNone(b["presentationWindowsDevice"]["deviceName"])
        self.assertEqual(b["displayDegraded"], "unknown")

    def test_a_derived_device_the_windows_inventory_does_not_carry_is_unmapped(self) -> None:
        lines = [ln.replace(f'device="{D1}"', 'device="\\\\.\\DISPLAY9"') for ln in _qt_screen_lines()]
        b = self.block(lines + [_target("PA329C"), _placement("PA329C")])
        self.assertEqual(b["presentationWindowsDevice"]["status"], "unmapped")
        self.assertEqual(b["displayDegraded"], "unknown")

    def test_a_qt_name_that_is_a_gdi_name_on_a_legacy_line_still_maps_by_name(self) -> None:
        # A monitor with no EDID name (a VM): Qt's name IS the GDI name and there is no device= field.
        line = (f'gui_smoke.display_screen index=0 name="{D1}" manufacturer="" model="" serial="" '
                'geometry=3840,0 2560x1440 physical=3840x2160 dpr=1.50 refresh_hz=60.000 primary=0')
        b = self.block([line, _target(D1), _placement(D1)])
        self.assertEqual(b["presentationWindowsDevice"]["status"], "mapped")
        self.assertEqual(b["presentationWindowsDevice"]["deviceName"], D1)

    def test_a_gdi_name_that_contradicts_the_derived_device_is_unmapped_not_resolved_by_either(self) -> None:
        line = (f'gui_smoke.display_screen index=0 name="{D1}" manufacturer="" model="" serial="" '
                f'geometry=0,0 2560x1440 physical=3840x2160 dpr=1.50 refresh_hz=60.000 primary=1 device="{D2}"')
        b = self.block([line, _target(D1), _placement(D1)])
        self.assertEqual(b["presentationWindowsDevice"]["status"], "unmapped")
        self.assertEqual(b["displayDegraded"], "unknown")


if __name__ == "__main__":
    unittest.main()
