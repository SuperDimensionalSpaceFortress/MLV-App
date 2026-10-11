"""Fail-closed validation for the MLV-App shipping-default snapshot."""

from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


MANIFEST_RELATIVE_PATH = Path("tools/gates/shipping-defaults.json")
RECEIPT_SOURCE = Path("platform/qt/ReceiptSettings.cpp")
MAIN_WINDOW_SOURCE = Path("platform/qt/MainWindow.cpp")
MAIN_WINDOW_UI = Path("platform/qt/MainWindow.ui")

EXCLUDED_RECEIPT_MEMBERS = {
    "m_neverLoaded",
    "m_lastPlaybackPosition",
    "m_mark",
}

STRING_RECEIPT_MEMBERS = {
    "m_gradationCurve",
    "m_hueVsHue",
    "m_hueVsSat",
    "m_hueVsLuma",
    "m_lumaVsSat",
    "m_darkFrameSubtractionName",
    "m_lutName",
    "m_transferFunction",
    "m_lookAssistFlavor",
    "m_lookAssistBaselineGradationCurve",
}

BOOL_RECEIPT_MEMBERS = {
    "m_isGradientEnabled",
    "m_highlightReconstruction",
    "m_chromaSeparation",
    "m_rawFixesEnabled",
    "m_upsideDown",
    "m_vidstabEnable",
    "m_vidstabTripod",
    "m_lutEnabled",
    "m_filterEnabled",
    "m_creativeAdjustments",
    "m_exrMode",
    "m_agx",
    "m_lookAssistEnabled",
    "m_lookAssistBaselineValid",
}

FLOAT_RECEIPT_MEMBERS = {
    "m_stretchFactorX",
    "m_stretchFactorY",
    "m_lookAssistBaselineStretchX",
    "m_lookAssistBaselineStretchY",
}

PLAYBACK_DEFAULTS = {
    "qualityMode": {"name": "high_quality", "value": 1},
    "previewMode": {"name": "sharp_smooth", "value": 0},
    "scaleFactorOverride": {"name": "auto", "value": 0},
    "previewResolution": {"name": "auto", "value": 0},
    "autoTargetFps": 30,
    "showQualityIndicator": True,
    "showExperimentalPhase3Modes": False,
    "phase3Acknowledged": False,
    "vsync": True,
    "derived": {
        "proxyLevel": -1,
        "initialScaleRequest": {"nonDualIso": 4, "dualIso": 4},
        "preferHqMean23": True,
    },
}


class ShippingDefaultsError(ValueError):
    """Raised when the tracked snapshot is incomplete, ambiguous, or stale."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ShippingDefaultsError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ShippingDefaultsError(f"non-finite JSON number: {value}")


def load_strict_json_bytes(payload: bytes) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ShippingDefaultsError("manifest is not strict UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
    except ShippingDefaultsError:
        raise
    except json.JSONDecodeError as exc:
        raise ShippingDefaultsError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ShippingDefaultsError("manifest root must be an object")
    return value


def _exact_keys(value: Any, expected: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ShippingDefaultsError(f"{path} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ShippingDefaultsError(f"{path} keys differ; missing={missing}, extra={extra}")
    return value


def _require_bool(value: Any, path: str) -> None:
    if type(value) is not bool:
        raise ShippingDefaultsError(f"{path} must be a boolean")


def _require_int(value: Any, path: str) -> None:
    if type(value) is not int:
        raise ShippingDefaultsError(f"{path} must be an integer")


def _require_number(value: Any, path: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ShippingDefaultsError(f"{path} must be a finite number")


def _validate_enum(value: Any, expected: dict[str, Any], path: str) -> None:
    enum_value = _exact_keys(value, {"name", "value"}, path)
    if type(enum_value["name"]) is not str:
        raise ShippingDefaultsError(f"{path}.name must be a string")
    _require_int(enum_value["value"], f"{path}.value")
    if enum_value != expected:
        raise ShippingDefaultsError(f"{path} differs from the v1 shipping contract")


def _validate_playback(playback: Any) -> dict[str, Any]:
    result = _exact_keys(playback, set(PLAYBACK_DEFAULTS), "playback")
    for key in ("qualityMode", "previewMode", "scaleFactorOverride", "previewResolution"):
        _validate_enum(result[key], PLAYBACK_DEFAULTS[key], f"playback.{key}")
    _require_int(result["autoTargetFps"], "playback.autoTargetFps")
    for key in (
        "showQualityIndicator",
        "showExperimentalPhase3Modes",
        "phase3Acknowledged",
        "vsync",
    ):
        _require_bool(result[key], f"playback.{key}")
    derived = _exact_keys(
        result["derived"], {"proxyLevel", "initialScaleRequest", "preferHqMean23"}, "playback.derived"
    )
    _require_int(derived["proxyLevel"], "playback.derived.proxyLevel")
    _require_bool(derived["preferHqMean23"], "playback.derived.preferHqMean23")
    scale = _exact_keys(
        derived["initialScaleRequest"], {"nonDualIso", "dualIso"}, "playback.derived.initialScaleRequest"
    )
    _require_int(scale["nonDualIso"], "playback.derived.initialScaleRequest.nonDualIso")
    _require_int(scale["dualIso"], "playback.derived.initialScaleRequest.dualIso")
    if result != PLAYBACK_DEFAULTS:
        raise ShippingDefaultsError("playback snapshot differs from the v1 shipping contract")
    return result


def _receipt_constructor_members(source: str) -> set[str]:
    assignments = re.findall(r"^\s*(m_[A-Za-z0-9_]+)\s*=\s*.+;\s*$", source, re.MULTILINE)
    duplicates = sorted({name for name in assignments if assignments.count(name) > 1})
    if duplicates:
        raise ShippingDefaultsError(f"ReceiptSettings constructor assigns members twice: {duplicates}")
    return set(assignments) - EXCLUDED_RECEIPT_MEMBERS


def validate_manifest(document: dict[str, Any], receipt_source: str) -> None:
    root = _exact_keys(document, {"schema", "scope", "playback", "processing"}, "root")
    if root["schema"] != "mlvapp.shipping-defaults.v1":
        raise ShippingDefaultsError("unsupported schema")
    if root["scope"] != "fresh-install-fresh-clip-no-env-overrides":
        raise ShippingDefaultsError("unsupported scope")

    _validate_playback(root["playback"])

    processing = _exact_keys(
        root["processing"], {"useDefaultReceipt", "freshReceipt", "uiMirrors"}, "processing"
    )
    _require_bool(processing["useDefaultReceipt"], "processing.useDefaultReceipt")
    if processing["useDefaultReceipt"]:
        raise ShippingDefaultsError("fresh clips must not silently use a default receipt")

    ui = _exact_keys(processing["uiMirrors"], {"lookAssistChecked"}, "processing.uiMirrors")
    _require_bool(ui["lookAssistChecked"], "processing.uiMirrors.lookAssistChecked")
    if not ui["lookAssistChecked"]:
        raise ShippingDefaultsError("Auto Look Assist UI mirror must default on")

    receipt = processing["freshReceipt"]
    expected_members = _receipt_constructor_members(receipt_source)
    receipt = _exact_keys(receipt, expected_members, "processing.freshReceipt")
    for name, value in receipt.items():
        path = f"processing.freshReceipt.{name}"
        if name in STRING_RECEIPT_MEMBERS:
            if type(value) is not str:
                raise ShippingDefaultsError(f"{path} must be a string")
        elif name in BOOL_RECEIPT_MEMBERS:
            _require_bool(value, path)
        elif name in FLOAT_RECEIPT_MEMBERS:
            _require_number(value, path)
        elif name == "m_debayer":
            _validate_enum(value, {"name": "amaze", "value": 5}, path)
        else:
            _require_int(value, path)


def _verify_ui_mirrors(repo_root: Path, document: dict[str, Any]) -> None:
    expected = document["processing"]["uiMirrors"]["lookAssistChecked"]
    ui_root = ET.fromstring((repo_root / MAIN_WINDOW_UI).read_bytes())
    widgets = [
        node for node in ui_root.iter("widget") if node.attrib.get("name") == "checkBoxLookAssistEnable"
    ]
    if len(widgets) != 1:
        raise ShippingDefaultsError("Auto Look Assist checkbox is missing or ambiguous")
    checked = widgets[0].find("./property[@name='checked']/bool")
    if checked is None or checked.text not in {"true", "false"}:
        raise ShippingDefaultsError("Auto Look Assist checked property is missing or invalid")
    if (checked.text == "true") != expected:
        raise ShippingDefaultsError("Auto Look Assist UI mirror differs from the manifest")

    main_window = (repo_root / MAIN_WINDOW_SOURCE).read_text(encoding="utf-8", errors="strict")
    matches = re.findall(
        r'set\.value\(\s*"defaultReceiptEnabled"\s*,\s*(true|false)\s*\)\.toBool\(\)',
        main_window,
    )
    if matches != ["false"]:
        raise ShippingDefaultsError("defaultReceiptEnabled fallback is missing or ambiguous")
    if document["processing"]["useDefaultReceipt"] is not False:
        raise ShippingDefaultsError("default receipt source mirror differs from the manifest")

    scale_fallbacks = re.findall(
        r'set\.value\(\s*PlaybackQualitySettings::kKeyScaleFactorOverride\(\)\s*,\s*'
        r'PlaybackQualitySettings::kDefaultScaleFactorOverride\(\)\s*\)\.toInt\(\)',
        main_window,
    )
    if len(scale_fallbacks) != 1:
        raise ShippingDefaultsError("playback scale override fallback is missing or ambiguous")


def verify_repository(repo_root: Path) -> dict[str, Any]:
    root = repo_root.resolve()
    manifest_path = root / MANIFEST_RELATIVE_PATH
    if not manifest_path.is_file():
        raise ShippingDefaultsError(f"missing manifest: {MANIFEST_RELATIVE_PATH.as_posix()}")
    receipt_source = (root / RECEIPT_SOURCE).read_text(encoding="utf-8", errors="strict")
    document = load_strict_json_bytes(manifest_path.read_bytes())
    validate_manifest(document, receipt_source)
    _verify_ui_mirrors(root, document)
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        verify_repository(args.repo_root)
    except (OSError, ET.ParseError, ShippingDefaultsError) as exc:
        print(f"shipping-defaults: FAIL: {exc}")
        return 2
    print("shipping-defaults: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
