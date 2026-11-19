"""EXPORT-DEBAYER-QUALITY-1: the debayer rule table, built HUB-SIDE and OFFLINE from published dual-venue export receipts only.

Inputs are receipt JSON files (Invoke-VenueLeg.ps1, legType "export" with export.quality). Each receipt's evidence directory holds the
hashed files the venue published (export/quality.json, export/stage-timing.log, export/summary.json); every one is re-hashed against
receipt.export.files before it is read. No capture, frame or footage is ever an input (they never leave the venue).

Rule (thresholds live in the emitted JSON; the rule is computed on S4_processing, the owner-shaped output):
  * reference = the BILINEAR floor of the same clip and stage. detailRatio = NCGE / NCGE_bilinear, noiseRatio = sigmaY / sigmaY_bilinear,
    chromaNoiseRatio = sigmaC / sigmaC_bilinear, falseColourRatio = FC / FC_bilinear. AMaZE is NOT the reference: there is no ground truth and
    AMaZE is the owner default (receipt debayer 5), so an AMaZE reference would make the advisor rule confirm itself.
  * recommended = the highest detailRatio among non-bilinear debayers with falseColourRatio <= 1.10 x the class minimum and noiseRatio <= 1.10 x
    the class minimum; two within 2% on detail are separated by the lower debayerMsMean.
  * fastestAcceptable = the lowest debayerMsMean whose three ratios are each within 5% of the recommended one's.
  * s3AgreesWithS4 = the same rule on S3_debayer picks the same recommended debayer.
Cross-leg integrity (typed refusals): S2 sha256s identical across the debayers of a clip (DEBAYER_INPUT_DIVERGED); S3 sha256s pairwise
distinct per frame (DEBAYER_NOT_APPLIED). A class needs all six debayers PASS with a captured quality pass, else it stays "unmeasured".
DEBAYER-ADVISOR-1 must never extrapolate into an unmeasured row.

Usage:  py -3 tools/profiling/dual-venue/debayer_rule_table.py --receipt <r.json> [--receipt ...] --out <table.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys

SCHEMA = "mlv-app/debayer-rule-table/v1"
DEBAYERS = ("bilinear", "amaze", "rcd", "dcb", "lmmse", "igv")
REFERENCE = "bilinear"
STAGE_BASIS = "S4_processing"
STAGE_CHECK = "S3_debayer"
THRESHOLDS = {"falseColourVsClassMin": 1.10, "noiseVsClassMin": 1.10, "detailTieFraction": 0.02, "fastestWithinFraction": 0.05,
              "isoBandLowMax": 400, "isoBandHighMin": 1600}
STAGE_LINE = re.compile(r"^\[mlv-stage-timing\] frame=(?P<f>[0-9]+) stage=(?P<s>[A-Za-z0-9_]+) ms=(?P<ms>[0-9]+(?:\.[0-9]+)?)\s*$")


class RuleTableError(Exception):
    def __init__(self, token: str, detail: str) -> None:
        super().__init__(f"{token}: {detail}")
        self.token = token


def iso_band(dual_iso: bool, iso_value) -> str:
    if dual_iso:
        return "dual"
    if iso_value is None:
        return "unknown"
    if iso_value <= THRESHOLDS["isoBandLowMax"]:
        return "low"
    if iso_value >= THRESHOLDS["isoBandHighMin"]:
        return "high"
    return "mid"


def percentile(values: list, p: float) -> float:
    """Nearest-rank percentile (deterministic, no interpolation)."""
    v = sorted(values)
    k = max(1, int(math.ceil(p * len(v))))
    return v[k - 1]


def debayer_times(stage_log_text: str) -> dict:
    per = {}
    for line in stage_log_text.splitlines():
        m = STAGE_LINE.match(line.strip())
        if m and m.group("s") == "debayered_frame":
            f = int(m.group("f"))
            per[f] = per.get(f, 0.0) + float(m.group("ms"))
    if not per:
        raise RuleTableError("DEBAYER_TIMING_ABSENT", "the timed run's stage-timing.log has no debayered_frame record")
    vals = [per[k] for k in sorted(per)]
    return {"debayerMsMean": math.fsum(vals) / len(vals), "debayerMsP10": percentile(vals, 0.10), "debayerMsP90": percentile(vals, 0.90)}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_leg(receipt_path: str) -> dict:
    """One receipt -> its quality row, every input re-hashed against receipt.export.files."""
    with open(receipt_path, "r", encoding="utf-8-sig") as fh:
        r = json.load(fh)
    ex = r.get("export") or {}
    q = ex.get("quality") or {}
    files = ex.get("files") or {}
    ev = (r.get("evidence") or {}).get("localEvidenceDir")
    texts = {}
    for name in ("quality.json", "stage-timing.log", "summary.json"):
        if name not in files or not ev:
            raise RuleTableError("RECEIPT_EVIDENCE_ABSENT", f"{os.path.basename(receipt_path)}: export.files lists no {name}")
        with open(os.path.join(ev, "export", name), "rb") as fh:
            b = fh.read()
        if _sha(b) != files[name]:
            raise RuleTableError("RECEIPT_EVIDENCE_HASH_MISMATCH", f"{os.path.basename(receipt_path)}: export/{name} does not hash to export.files")
        texts[name] = b.decode("utf-8-sig")
    quality = json.loads(texts["quality.json"])
    summary = json.loads(texts["summary.json"])
    subject = r.get("subject") or {}
    return {
        "receiptId": r.get("receiptId"), "clipId": subject.get("clipId"), "legId": subject.get("legId"), "outcome": r.get("outcome"),
        "qualityVerdict": q.get("verdict"), "debayer": quality.get("debayer"), "receiptDebayer": quality.get("receiptDebayer"),
        "dualIso": bool(ex.get("dualIsoExpected")), "isoValue": quality.get("isoValue"), "sourceCommit": subject.get("sourceCommit"),
        "buildManifestSha256": subject.get("buildManifestSha256"), "quality": quality,
        "exportWallSeconds": summary.get("exportWallSeconds"), "times": debayer_times(texts["stage-timing.log"]),
    }


def check_integrity(legs: list) -> None:
    """legs: the six debayer legs of ONE clip."""
    s2 = {}
    for lg in legs:
        s2[lg["debayer"]] = tuple(row["sha256"].get("S2_post_dualiso") for row in lg["quality"]["perFrame"])
    if len(set(s2.values())) != 1:
        raise RuleTableError("DEBAYER_INPUT_DIVERGED", f"{legs[0]['clipId']}: the S2 (Bayer input) sha256s differ across debayers")
    frames = len(next(iter(s2.values())))
    for i in range(frames):
        s3 = [lg["quality"]["perFrame"][i]["sha256"].get("S3_debayer") for lg in legs]
        if len(set(s3)) != len(s3):
            raise RuleTableError("DEBAYER_NOT_APPLIED", f"{legs[0]['clipId']}: two debayers produced the same S3 buffer on frame row {i}")


def ratios(by: dict, stage: str) -> dict:
    ref = by[REFERENCE]["quality"]["metrics"][stage]
    out = {}
    for name, lg in by.items():
        m = lg["quality"]["metrics"][stage]
        out[name] = {
            "detailRatio": m["ncge"] / ref["ncge"] if ref["ncge"] else None,
            "noiseRatio": m["sigmaY"] / ref["sigmaY"] if ref["sigmaY"] else None,
            "chromaNoiseRatio": m["sigmaC"] / ref["sigmaC"] if ref["sigmaC"] else None,
            "falseColourRatio": m["fc"] / ref["fc"] if ref["fc"] else None,
        }
    return out


def apply_rule(per: dict, times: dict) -> tuple:
    """per: {debayer: {detailRatio, noiseRatio, chromaNoiseRatio, falseColourRatio}}; times: {debayer: debayerMsMean}.
    Returns (recommended, fastestAcceptable)."""
    cands = [n for n in per if n != REFERENCE and None not in (per[n]["detailRatio"], per[n]["noiseRatio"], per[n]["falseColourRatio"])]
    if not cands:
        return None, None
    fc_min = min(per[n]["falseColourRatio"] for n in cands)
    nz_min = min(per[n]["noiseRatio"] for n in cands)
    ok = [n for n in cands if per[n]["falseColourRatio"] <= THRESHOLDS["falseColourVsClassMin"] * fc_min
          and per[n]["noiseRatio"] <= THRESHOLDS["noiseVsClassMin"] * nz_min]
    if not ok:
        return None, None
    best = max(per[n]["detailRatio"] for n in ok)
    near = [n for n in ok if per[n]["detailRatio"] >= best * (1.0 - THRESHOLDS["detailTieFraction"])]
    rec = sorted(near, key=lambda n: (times[n], -per[n]["detailRatio"], n))[0]
    tol = THRESHOLDS["fastestWithinFraction"]

    def within(n: str) -> bool:
        for k in ("detailRatio", "noiseRatio", "falseColourRatio"):
            a, b = per[n][k], per[rec][k]
            if a is None or b is None or abs(a - b) > tol * abs(b):
                return False
        return True
    fast = sorted((n for n in per if n != REFERENCE and within(n)), key=lambda n: (times[n], n))
    return rec, (fast[0] if fast else rec)


def unmeasured_row(dual_iso: bool, band: str, reason: str) -> dict:
    return {"classKey": {"dualIso": dual_iso, "isoBand": band}, "status": "unmeasured", "clips": [], "stageBasis": STAGE_BASIS,
            "recommended": None, "fastestAcceptable": None, "perDebayer": {}, "s3AgreesWithS4": None, "reason": reason}


def build_table(legs: list) -> dict:
    by_clip = {}
    for lg in legs:
        by_clip.setdefault(lg["clipId"], []).append(lg)
    commits = sorted({lg["sourceCommit"] for lg in legs if lg.get("sourceCommit")})
    manifests = sorted({lg["buildManifestSha256"] for lg in legs if lg.get("buildManifestSha256")})
    if len(commits) > 1 or len(manifests) > 1:
        raise RuleTableError("DEBAYER_BUILD_DIVERGED", "the receipts come from more than one build")
    classes = {}
    unmeasured_reasons = {}
    for clip in sorted(by_clip):
        group = by_clip[clip]
        passed = [lg for lg in group if lg["outcome"] == "PASS" and lg["qualityVerdict"] == "PASS"]
        by = {}
        for lg in passed:
            by.setdefault(lg["debayer"], lg)
        key = (group[0]["dualIso"], iso_band(group[0]["dualIso"], group[0]["isoValue"]))
        missing = [d for d in DEBAYERS if d not in by]
        if missing:
            unmeasured_reasons.setdefault(key, []).append(f"{clip}: no PASS quality receipt for {', '.join(missing)}")
            continue
        legs6 = [by[d] for d in DEBAYERS]
        check_integrity(legs6)
        classes.setdefault(key, []).append((clip, {d: by[d] for d in DEBAYERS}))
    rows = []
    for key in sorted(set(classes) | set(unmeasured_reasons) | {(True, "dual"), (False, "low"), (False, "high")}, key=lambda k: (not k[0], k[1])):
        if key not in classes:
            reason = "; ".join(unmeasured_reasons.get(key, [])) or "no consented clip of this class has been measured (EXPORT-NODUALISO-CLIP-CLASS-1)"
            rows.append(unmeasured_row(key[0], key[1], reason))
            continue
        clips = classes[key]
        # one clip per class in slice 1; with several the per-debayer ratios are the mean over the class's clips
        per = {}
        per_s3 = {}
        for d in DEBAYERS:
            r4 = [ratios(c[1], STAGE_BASIS)[d] for c in clips]
            r3 = [ratios(c[1], STAGE_CHECK)[d] for c in clips] if all(STAGE_CHECK in c[1][d]["quality"]["metrics"] for c in clips) else None
            per[d] = {k: (math.fsum(x[k] for x in r4) / len(r4) if all(x[k] is not None for x in r4) else None) for k in r4[0]}
            if r3 is not None:
                per_s3[d] = {k: (math.fsum(x[k] for x in r3) / len(r3) if all(x[k] is not None for x in r3) else None) for k in r3[0]}
            per[d]["debayerMsMean"] = math.fsum(c[1][d]["times"]["debayerMsMean"] for c in clips) / len(clips)
            per[d]["debayerMsP10"] = math.fsum(c[1][d]["times"]["debayerMsP10"] for c in clips) / len(clips)
            per[d]["debayerMsP90"] = math.fsum(c[1][d]["times"]["debayerMsP90"] for c in clips) / len(clips)
            per[d]["exportWallSeconds"] = math.fsum(float(c[1][d]["exportWallSeconds"] or 0.0) for c in clips) / len(clips)
            per[d]["receiptIds"] = [c[1][d]["receiptId"] for c in clips]
        times = {d: per[d]["debayerMsMean"] for d in DEBAYERS}
        rec, fast = apply_rule(per, times)
        rec3 = apply_rule(per_s3, times)[0] if len(per_s3) == len(DEBAYERS) else None
        rows.append({"classKey": {"dualIso": key[0], "isoBand": key[1]}, "status": "measured", "clips": [c[0] for c in clips],
                     "stageBasis": STAGE_BASIS, "recommended": rec, "fastestAcceptable": fast,
                     "perDebayer": per, "s3AgreesWithS4": (rec3 == rec) if rec3 is not None else None,
                     "reason": "all six debayers PASS with a captured quality pass; rule on S4_processing, bilinear reference"})
    return {"schema": SCHEMA, "card": "EXPORT-DEBAYER-QUALITY-1", "sourceCommit": commits[0] if commits else None,
            "buildManifestSha256": manifests[0] if manifests else None, "thresholds": dict(THRESHOLDS), "reference": REFERENCE,
            "classes": rows}


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the debayer rule table from published export quality receipts.")
    ap.add_argument("--receipt", action="append", default=[])
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    try:
        table = build_table([load_leg(p) for p in a.receipt])
    except RuleTableError as e:
        print(f"RULE_TABLE_REFUSED={e.token}")
        print(str(e))
        return 3
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(table, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"RULE_TABLE_WRITTEN classes={len(table['classes'])} measured={sum(1 for c in table['classes'] if c['status'] == 'measured')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
