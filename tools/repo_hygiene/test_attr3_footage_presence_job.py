"""Behavioural tests for tools/profiling/bachelor/attr3-footage-presence-job.ps1 and its job-text
builder, tools/profiling/bachelor/Attr3FootagePresenceJob.psm1.

Every row uses SYNTHETIC parts, imported and called directly through the module's
New-Attr3FootagePresenceJob -- a plain PowerShell function call, never a flag on the CLI generator
(the real generator's only path to parts is tools/gates/resolve_consented_clip.py; see the
generator's own header comment). No row calls the real resolver, reads git, touches the hook's
real consent table, or opens a tracked/real clip. Synthetic paths live under a directory named for
a distinctive token so the path-free-output rows can assert the token never reaches stdout or
stderr, and every synthetic fixture uses a ``.raw`` extension per this card's hard rule against the
real footage extension.

ROUND 2 (ATTR3-FOOTAGE-BIND-1-prA): every test here that resolves parts through TEMP is built to
be environment-independent -- it does not matter whether the host's TEMP root is a plain path or
an 8.3 short name containing ``~``, because at least one part below always carries a path with
FORWARD slashes and at least one always carries a literal ``~``, exercising both real-data
properties directly rather than hoping the ambient TEMP shape happens to cover them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "attr3-footage-presence-job.ps1"
MODULE = ROOT / "tools" / "profiling" / "bachelor" / "Attr3FootagePresenceJob.psm1"
PWSH = shutil.which("pwsh")

TOKEN = "ZZPRESENCEJOBTESTTOKENZZ"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
@unittest.skipUnless(__import__("os").name == "nt", "the emitted job targets a Windows measurement host")
class FootagePresenceJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3presence-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "out"
        self.out.mkdir()

        # Two real-data properties, forced explicitly rather than left to the ambient TEMP shape:
        # part 0 always carries a path with FORWARD slashes (every consented part in the real spec
        # uses them); part 1 always carries a literal `~` (a measurement host's own TEMP root can
        # be an 8.3 short name). Both are legal NTFS path components, so the files genuinely exist
        # at these exact strings.
        fwd_dir = self.tmp / f"{TOKEN}-fwd"
        fwd_dir.mkdir()
        tilde_dir = self.tmp / f"{TOKEN}~short"
        tilde_dir.mkdir()

        fwd_native = fwd_dir / "clip.raw"
        tilde_native = tilde_dir / "clip.raw.part1"
        self.contents = (b"synthetic presence part zero " * 131, b"synthetic presence part one")
        fwd_native.write_bytes(self.contents[0])
        tilde_native.write_bytes(self.contents[1])

        self.paths = [str(fwd_native).replace("\\", "/"), str(tilde_native)]
        self.assertIn("/", self.paths[0])
        self.assertNotIn("\\", self.paths[0])
        self.assertIn("~", self.paths[1])

        self.clip_id = "FIX-PRESENCE-0001"
        self.parts_payload = [
            {"index": i, "path": path, "length": len(content), "sha256": _sha256(content)}
            for i, (path, content) in enumerate(zip(self.paths, self.contents))
        ]

    # ---- calling New-Attr3FootagePresenceJob directly, never through the CLI ---------------------

    def call_module(self, parts, clip_id: str | None = None, out_dir: Path | None = None) -> subprocess.CompletedProcess:
        parts_json_path = self.tmp / f"parts-{id(parts)}.json"
        parts_json_path.write_text(json.dumps(parts), encoding="utf-8")
        script = (
            f"Import-Module '{MODULE}' -Force; "
            f"$parts = @(Get-Content -LiteralPath '{parts_json_path}' -Raw | ConvertFrom-Json); "
            f"New-Attr3FootagePresenceJob -ClipId '{clip_id or self.clip_id}' -Parts $parts -OutDir '{out_dir or self.out}'"
        )
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True,
        )

    def generate(self, parts=None, clip_id: str | None = None) -> subprocess.CompletedProcess:
        return self.call_module(self.parts_payload if parts is None else parts, clip_id=clip_id)

    def job_path(self, proc: subprocess.CompletedProcess) -> Path:
        jobs = sorted(self.out.glob("*.job.ps1"))
        self.assertEqual(len(jobs), 1, proc.stdout + proc.stderr)
        return jobs[0]

    def run_job(self, job: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(job)],
            capture_output=True, text=True,
        )

    def _assert_no_token(self, *texts: str) -> None:
        for text in texts:
            self.assertNotIn(TOKEN, text)

    # ---- the injection seam is gone -------------------------------------------------------------

    def test_generator_no_longer_has_an_injection_parameter(self) -> None:
        # The removed parameter's name may still appear in prose comments explaining WHY it is
        # gone (see the generator's own header) -- what must be absent is any live code line
        # (declaring or referencing it as a parameter/variable), so comment lines are excluded.
        code_lines = [
            line for line in GENERATOR.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("#")
        ]
        for line in code_lines:
            self.assertNotIn("InjectResolvedPartsJsonPathForTests", line)

    def test_generator_calls_the_module_not_a_local_template(self) -> None:
        text = GENERATOR.read_text(encoding="utf-8")
        self.assertIn("New-Attr3FootagePresenceJob", text)
        self.assertIn("Attr3FootagePresenceJob.psm1", text)

    # ---- module: builder behaviour ------------------------------------------------------------

    def test_module_emits_one_job_and_prints_no_path(self) -> None:
        proc = self.generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.job_path(proc)
        self._assert_no_token(proc.stdout, proc.stderr)

    def test_module_refuses_zero_parts(self) -> None:
        proc = self.generate(parts=[])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ATTR3_PRESENCE_NO_PARTS", proc.stdout + proc.stderr)
        self.assertEqual(sorted(self.out.glob("*.job.ps1")), [])

    def test_module_refuses_a_path_without_a_drive_prefix(self) -> None:
        bad = [dict(self.parts_payload[0])]
        bad[0]["path"] = "relative/clip.raw"
        proc = self.generate(parts=bad)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ATTR3_PRESENCE_PART_PATH_INVALID", proc.stdout + proc.stderr)
        self.assertEqual(sorted(self.out.glob("*.job.ps1")), [])

    def test_module_refuses_a_control_character_in_the_path(self) -> None:
        bad = [dict(self.parts_payload[0])]
        bad[0]["path"] = bad[0]["path"] + "\x01"
        proc = self.generate(parts=bad)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ATTR3_PRESENCE_PART_PATH_INVALID", proc.stdout + proc.stderr)

    def test_module_accepts_forward_slashes_and_tilde_and_never_embeds_a_bare_path(self) -> None:
        # Requirement A's whole point: neither a forward slash nor a `~` is rejected, and the
        # emitted job's own text never contains the plain path -- only its base64 form.
        proc = self.generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        job = self.job_path(proc)
        job_text = job.read_text(encoding="utf-8")
        for path in self.paths:
            self.assertNotIn(path, job_text)
        self.assertIn("pathBase64", job_text)
        self.assertNotIn('"path"', job_text)

    def test_a_quote_and_semicolon_in_the_path_does_not_break_out_of_the_template(self) -> None:
        # Round 2's actual injection-safety proof: a path containing a single quote (which JSON
        # encoding does NOT escape) used to be able to break out of the single-quoted PowerShell
        # literal the old template wrapped $PartsJson in. Base64 embedding removes the character
        # class entirely: the job must still emit and run cleanly, and the hostile-looking
        # substring must never appear verbatim in the emitted job text.
        hostile_dir = self.tmp / f"{TOKEN}-hostile"
        hostile_dir.mkdir()
        hostile_name = "clip'; $(Get-Date) .raw"
        hostile_path = hostile_dir / hostile_name
        hostile_path.write_bytes(b"synthetic hostile content")
        parts = [
            {
                "index": 0,
                "path": str(hostile_path),
                "length": len(b"synthetic hostile content"),
                "sha256": _sha256(b"synthetic hostile content"),
            }
        ]
        proc = self.generate(parts=parts, clip_id="FIX-PRESENCE-HOSTILE")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        job = self.job_path(proc)
        job_text = job.read_text(encoding="utf-8")
        self.assertNotIn("'; $(Get-Date)", job_text)
        run = self.run_job(job)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("RESULT=FOOTAGE_PRESENT", run.stdout)
        self.assertIn("PART=0 STATUS=PASS", run.stdout)

    def test_a_clip_id_shaped_like_a_template_token_is_not_re_expanded(self) -> None:
        # ATTR3-FOOTAGE-BIND-1 PR-B round 3 (STRUCTURAL). -ClipId's own ValidatePattern
        # (alnum/underscore/dot/hyphen) admits 'a__EMBEDDED_FUNCTIONS__b' -- a value shaped
        # exactly like the LAST placeholder the old chained .Replace() calls substituted. Before
        # Expand-AttrCudaTemplate, that value would have collided with the later
        # __EMBEDDED_FUNCTIONS__ substitution and spliced verifier source into the middle of the
        # $ClipId string literal, breaking the emitted job's own parse. The job must still emit,
        # parse cleanly, and carry the value literally.
        proc = self.generate(clip_id="a__EMBEDDED_FUNCTIONS__b")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        job = self.job_path(proc)
        job_text = job.read_text(encoding="utf-8")
        self.assertIn("$ClipId = 'a__EMBEDDED_FUNCTIONS__b'", job_text)
        parse = subprocess.run(
            [
                PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                "$t=$null; $e=$null; "
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{job}', [ref]$t, [ref]$e); "
                "Write-Output $e.Count",
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(parse.stdout.strip(), "0", parse.stdout + parse.stderr)
        run = self.run_job(job)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("RESULT=FOOTAGE_PRESENT", run.stdout)

    # ---- emitted job: honest per-part statuses (round 4) and the four overall results ------------
    # NOT_FOUND / ACCESS_DENIED / UNREADABLE / LENGTH_MISMATCH / SHA256_MISMATCH / PASS per part;
    # FOOTAGE_PRESENT / FOOTAGE_ABSENT / FOOTAGE_MISMATCH / FOOTAGE_INDETERMINATE overall -- see
    # Attr3FootagePresenceJob.psm1's job template for the exact mapping this exercises.

    def test_pass_when_both_parts_match(self) -> None:
        job = self.job_path(self.generate())
        run = self.run_job(job)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("RESULT=FOOTAGE_PRESENT", run.stdout)
        self.assertIn("PART=0 STATUS=PASS", run.stdout)
        self.assertIn("PART=1 STATUS=PASS", run.stdout)
        self._assert_no_token(run.stdout, run.stderr)
        payload = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["result"], "FOOTAGE_PRESENT")
        self.assertEqual([p["status"] for p in payload["parts"]], ["PASS", "PASS"])

    def test_both_parts_not_found_is_footage_absent(self) -> None:
        job = self.job_path(self.generate())
        for path in self.paths:
            Path(path).unlink()
        run = self.run_job(job)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("RESULT=FOOTAGE_ABSENT", run.stdout)
        self.assertIn("PART=0 STATUS=NOT_FOUND", run.stdout)
        self.assertIn("PART=1 STATUS=NOT_FOUND", run.stdout)
        self._assert_no_token(run.stdout, run.stderr)

    def test_length_mismatch_is_footage_mismatch(self) -> None:
        job = self.job_path(self.generate())
        Path(self.paths[1]).write_bytes(self.contents[1] + b"!")
        run = self.run_job(job)
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        self.assertIn("RESULT=FOOTAGE_MISMATCH", run.stdout)
        self.assertIn("PART=1 STATUS=LENGTH_MISMATCH", run.stdout)
        self.assertIn("PART=0 STATUS=PASS", run.stdout)
        self._assert_no_token(run.stdout, run.stderr)

    def test_sha256_mismatch_at_the_same_length_is_footage_mismatch(self) -> None:
        job = self.job_path(self.generate())
        swapped = bytes(reversed(self.contents[1]))
        self.assertEqual(len(swapped), len(self.contents[1]))
        Path(self.paths[1]).write_bytes(swapped)
        run = self.run_job(job)
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        self.assertIn("RESULT=FOOTAGE_MISMATCH", run.stdout)
        self.assertIn("PART=1 STATUS=SHA256_MISMATCH", run.stdout)
        self.assertIn("PART=0 STATUS=PASS", run.stdout)
        self._assert_no_token(run.stdout, run.stderr)

    def test_one_part_not_found_one_pass_is_footage_indeterminate(self) -> None:
        # A PASS mixed with a NOT_FOUND never observed a byte difference on any part -- it
        # cannot honestly be called PRESENT, ABSENT or MISMATCH (round 4, defect 5): this is a
        # BEHAVIOUR CHANGE from round 2/3, which folded this combination into FOOTAGE_MISMATCH.
        job = self.job_path(self.generate())
        Path(self.paths[1]).unlink()
        run = self.run_job(job)
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        self.assertIn("RESULT=FOOTAGE_INDETERMINATE", run.stdout)
        self.assertIn("PART=0 STATUS=PASS", run.stdout)
        self.assertIn("PART=1 STATUS=NOT_FOUND", run.stdout)
        self._assert_no_token(run.stdout, run.stderr)

    def test_one_part_not_found_one_length_mismatch_is_still_footage_mismatch(self) -> None:
        # An actually-observed byte difference on one part still yields MISMATCH even alongside
        # a NOT_FOUND part, per the documented mapping (MISMATCH is not conditioned on the
        # ABSENCE of NOT_FOUND parts -- only on the absence of ACCESS_DENIED/UNREADABLE ones).
        job = self.job_path(self.generate())
        Path(self.paths[0]).unlink()
        Path(self.paths[1]).write_bytes(self.contents[1] + b"!")
        run = self.run_job(job)
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        self.assertIn("RESULT=FOOTAGE_MISMATCH", run.stdout)
        self.assertIn("PART=0 STATUS=NOT_FOUND", run.stdout)
        self.assertIn("PART=1 STATUS=LENGTH_MISMATCH", run.stdout)
        self._assert_no_token(run.stdout, run.stderr)

    def test_unreadable_status_when_file_is_locked_during_hash(self) -> None:
        # Neither ItemNotFoundException nor UnauthorizedAccessException: an exclusive read lock
        # held by another process makes Get-Item's stat succeed (length unchanged) but
        # Get-FileHash's open fail with a plain sharing-violation IOException, which must land as
        # UNREADABLE -- not escape the try/catch, and not print the path.
        job = self.job_path(self.generate())
        locked_path = self.paths[0]
        script = (
            f"$fs = [System.IO.File]::Open('{locked_path}', [System.IO.FileMode]::Open, "
            f"[System.IO.FileAccess]::Read, [System.IO.FileShare]::None); "
            f"try {{ & '{PWSH}' -NoLogo -NoProfile -NonInteractive -File '{job}'; "
            f"Write-Output \"CHILD_EXIT=$LASTEXITCODE\" }} finally {{ $fs.Dispose() }}"
        )
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True,
        )
        self.assertIn("PART=0 STATUS=UNREADABLE", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("PART=1 STATUS=PASS", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("RESULT=FOOTAGE_INDETERMINATE", proc.stdout)
        self.assertIn("CHILD_EXIT=3", proc.stdout)
        self._assert_no_token(proc.stdout, proc.stderr)

    def test_access_denied_status_when_acl_denies_read(self) -> None:
        # A synthetic file with an ACL that denies read to the current user, created and cleaned
        # up entirely within this test's own temp directory.
        job = self.job_path(self.generate())
        target = self.paths[0]
        user = os.environ.get("USERNAME", "")
        self.assertTrue(user, "USERNAME must be set to run the ACL-denial test")
        deny = subprocess.run(
            ["icacls", target, "/deny", f"{user}:(R)"], capture_output=True, text=True,
        )
        self.assertEqual(deny.returncode, 0, deny.stdout + deny.stderr)
        try:
            run = self.run_job(job)
        finally:
            subprocess.run(
                ["icacls", target, "/remove:d", user], capture_output=True, text=True,
            )
        self.assertIn("PART=0 STATUS=ACCESS_DENIED", run.stdout, run.stdout + run.stderr)
        self.assertIn("PART=1 STATUS=PASS", run.stdout, run.stdout + run.stderr)
        self.assertIn("RESULT=FOOTAGE_INDETERMINATE", run.stdout)
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        self._assert_no_token(run.stdout, run.stderr)

    def test_length_mismatch_with_denied_content_read_is_footage_indeterminate(self) -> None:
        # Round 5: metadata is readable (the length genuinely differs, so Get-Item's stat
        # succeeds), but the CONTENT read is denied by ACL -- that must NOT be reported as
        # LENGTH_MISMATCH/FOOTAGE_MISMATCH, since no byte was ever actually observed to differ.
        # It must land as ACCESS_DENIED / FOOTAGE_INDETERMINATE instead, per the round-4 contract
        # that MISMATCH requires a part that is readable AND differs.
        job = self.job_path(self.generate())
        target = self.paths[0]
        Path(target).write_bytes(self.contents[0] + b"!")
        user = os.environ.get("USERNAME", "")
        self.assertTrue(user, "USERNAME must be set to run the ACL-denial test")
        deny = subprocess.run(
            ["icacls", target, "/deny", f"{user}:(R)"], capture_output=True, text=True,
        )
        self.assertEqual(deny.returncode, 0, deny.stdout + deny.stderr)
        try:
            run = self.run_job(job)
        finally:
            subprocess.run(
                ["icacls", target, "/remove:d", user], capture_output=True, text=True,
            )
        self.assertIn("PART=0 STATUS=ACCESS_DENIED", run.stdout, run.stdout + run.stderr)
        self.assertNotIn("PART=0 STATUS=LENGTH_MISMATCH", run.stdout)
        self.assertIn("PART=1 STATUS=PASS", run.stdout, run.stdout + run.stderr)
        self.assertIn("RESULT=FOOTAGE_INDETERMINATE", run.stdout)
        self.assertNotIn("RESULT=FOOTAGE_MISMATCH", run.stdout)
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        self._assert_no_token(run.stdout, run.stderr)

    def test_repeated_builds_for_identical_content_get_different_job_ids(self) -> None:
        # ATTR3-FOOTAGE-STAGE-1 round 3: a purely content-derived job id meant a repeated presence
        # check for the SAME clip and parts always submitted the SAME id, so a retained result
        # receipt from an earlier probe refused every later one (UMRUN_JOBID_IN_USE). The id must
        # differ per call; the reported SOURCE_SHA256 audit key must not.
        first_proc = self.generate()
        first_job = self.job_path(first_proc)
        first_job_id = first_job.name[: -len(".job.ps1")]
        first_job.unlink()
        second_proc = self.generate()
        second_job = self.job_path(second_proc)
        second_job_id = second_job.name[: -len(".job.ps1")]
        self.assertNotEqual(first_job_id, second_job_id)
        first_sha = first_proc.stdout.split("SOURCE_SHA256=")[1].split()[0]
        second_sha = second_proc.stdout.split("SOURCE_SHA256=")[1].split()[0]
        self.assertEqual(first_sha, second_sha)

    def test_job_emission_prints_the_job_id_never_the_local_job_file_path(self) -> None:
        proc = self.generate()
        job = self.job_path(proc)
        job_id = job.name[: -len(".job.ps1")]
        self.assertIn(f"JOB={job_id}", proc.stdout)
        self.assertNotIn(str(job), proc.stdout + proc.stderr)

    # ---- round 4: link check on the read path (astra 3) ------------------------------------------

    def test_a_junction_above_the_target_path_yields_target_path_unsafe_and_footage_indeterminate(self) -> None:
        # ATTR3-FOOTAGE-STAGE-1 round 4 (astra 3, link checks on read paths): a junction planted
        # at an ANCESTOR of the target path must be refused before a single byte is read or
        # hashed -- this probe applies the SAME target-chain check
        # Attr3FootageStageJob.psm1's own emitted job applies to its own target path.
        linked_container = self.tmp / f"{TOKEN}-linked-container"
        real_container = self.tmp / f"{TOKEN}-real-container"
        real_container.mkdir()
        real_file = real_container / "clip.raw"
        real_file.write_bytes(self.contents[0])
        made = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             f"New-Item -ItemType Junction -Path '{linked_container}' -Target '{real_container}' | Out-Null"],
            capture_output=True, text=True,
        )
        if made.returncode != 0 or not linked_container.exists():
            self.skipTest(f"cannot create a junction here: {made.stderr}")
        linked_path = str(linked_container / "clip.raw")
        parts = [{"index": 0, "path": linked_path, "length": len(self.contents[0]), "sha256": _sha256(self.contents[0])}]
        job = self.job_path(self.generate(parts=parts, clip_id="FIX-PRESENCE-LINK"))
        run = self.run_job(job)
        self.assertIn("PART=0 STATUS=TARGET_PATH_UNSAFE", run.stdout, run.stdout + run.stderr)
        self.assertIn("RESULT=FOOTAGE_INDETERMINATE", run.stdout)
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        self.assertEqual(real_file.read_bytes(), self.contents[0])


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
@unittest.skipUnless(os.name == "nt", "Get-Attr3FootagePresentPartIndexes runs a PowerShell module")
class GetPresentPartIndexesTests(unittest.TestCase):
    """Attr3FootagePresenceJob.psm1's Get-Attr3FootagePresentPartIndexes (ATTR3-FOOTAGE-STAGE-1
    round 4, sol minor / astra 5: per-part resume) -- parses a presence job's own stdout directly,
    without driving the real CLI or the real resolver."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3presentidx-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def resolve(self, stdout_text: str, clip_id: str) -> subprocess.CompletedProcess:
        stdout_file = self.tmp / f"stdout-{id(stdout_text)}.txt"
        stdout_file.write_text(stdout_text, encoding="utf-8")
        script = (
            f"Import-Module '{MODULE}' -Force; "
            f"$stdout = Get-Content -LiteralPath '{stdout_file}' -Raw; "
            f"(Get-Attr3FootagePresentPartIndexes -Stdout $stdout -ClipId '{clip_id}') -join ','"
        )
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True,
        )

    def _payload_stdout(self, clip_id: str, parts) -> str:
        payload = {
            "schema": "mlvapp.attr3-footage-presence.v1", "jobId": "job-x", "clipId": clip_id,
            "result": "FOOTAGE_INDETERMINATE", "partCount": len(parts), "parts": parts,
        }
        lines = [f"PART={p['index']} STATUS={p['status']}" for p in parts]
        lines.append(f"RESULT=FOOTAGE_INDETERMINATE CLIP={clip_id} PARTS={len(parts)}")
        lines.append(json.dumps(payload))
        return "\n".join(lines) + "\n"

    def test_extracts_only_pass_indexes_for_the_matching_clip(self) -> None:
        stdout = self._payload_stdout(
            "FIX-RESUME-0001",
            [{"index": 0, "status": "PASS"}, {"index": 1, "status": "NOT_FOUND"}, {"index": 2, "status": "PASS"}],
        )
        proc = self.resolve(stdout, "FIX-RESUME-0001")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0,2")

    def test_returns_empty_for_a_clip_id_mismatch(self) -> None:
        stdout = self._payload_stdout("FIX-RESUME-OTHER", [{"index": 0, "status": "PASS"}])
        proc = self.resolve(stdout, "FIX-RESUME-0001")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_returns_empty_for_malformed_or_missing_payload(self) -> None:
        proc = self.resolve("not json at all, no schema line here\n", "FIX-RESUME-0001")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_returns_empty_for_empty_stdout(self) -> None:
        proc = self.resolve("", "FIX-RESUME-0001")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_only_the_last_matching_schema_line_is_trusted(self) -> None:
        # Two payload lines in the same text (e.g. concatenated stdout+stderr) -- only the LAST is
        # trusted, matching a real job's own contract of emitting exactly one.
        first = self._payload_stdout("FIX-RESUME-0001", [{"index": 0, "status": "NOT_FOUND"}])
        second = self._payload_stdout("FIX-RESUME-0001", [{"index": 0, "status": "PASS"}])
        proc = self.resolve(first + second, "FIX-RESUME-0001")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
