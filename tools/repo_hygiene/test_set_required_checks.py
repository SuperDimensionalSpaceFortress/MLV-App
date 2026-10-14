"""Temporary, offline fixtures for the one-shot required-check transition."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'tools/coordination/set-required-checks.ps1'
PWSH = shutil.which('pwsh')
OLD = ['Repo Hygiene Python (windows-latest)', 'Repo Hygiene Python (ubuntu-latest)',
       'Factory Bridge Regressions', 'Windows GUI Pilot', 'Windows Product Oracles']
NEW = [name.replace('Factory Bridge Regressions', 'Batch Compile') for name in OLD]


def protection(names):
    return {'strict': True, 'contexts': names,
            'checks': [{'context': n, 'app_id': 15368} for n in names]}


@unittest.skipUnless(PWSH, 'PowerShell 7 is required')
class RequiredChecksTransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mlv-checks-test-')
        self.addCleanup(self.temp.cleanup)
        self.board = Path(self.temp.name)
        self.receipts = self.board / '.claude-state/coordination/dual-lane/receipts'
        self.receipts.mkdir(parents=True)
        self.write('0.4a-workflow-base.json', {'mergeSha': 'a' * 40})
        self.write('0.4a-batch-compile-falsifier.json', {
            'workflowBaseSha': 'a' * 40, 'headSha': 'b' * 40,
            'failingContext': 'Batch Compile', 'failingStep': 'Build MLVApp', 'conclusion': 'failure'})
        self.write('0.4c-guardrail-move.json', {
            'headSha': 'c' * 40, 'conclusion': 'success', 'collectedTests': 176,
            'requiredJobs': ['Repo Hygiene Python (windows-latest)']})
        # reviewedHeadSha is the PR HEAD the hosted guardrail workflow actually ran
        # against; mergeSha is deliberately a DIFFERENT commit, matching the real
        # chain (0.4c-guardrail-move headSha=230bb4be.., execution-control-0.4c-i
        # reviewedHeadSha=230bb4be.., mergeSha=4c5b8d3f..) that the old headSha ==
        # mergeSha comparison wrongly refused.
        self.write('execution-control-0.4c-i.json', {'reviewedHeadSha': 'c' * 40, 'mergeSha': '9' * 40})
        (self.board / 'approval.json').write_text(json.dumps({'verdict': 'APPROVE', 'subject_sha': 'd' * 40}), encoding='utf-8')
        self.write('execution-control-0.4b-i.json', {
            'mergeSha': 'e' * 40, 'reviewedHeadSha': 'd' * 40, 'solVerdictPath': 'approval.json',
            'hashes': {'tools/coordination/set-required-checks.ps1': hashlib.sha256(SCRIPT.read_bytes()).hexdigest()}})
        self.snapshot = self.receipts / 'required-checks-live.jsonl'
        self.original = (json.dumps(protection(OLD)) + '\n').encode()
        self.snapshot.write_bytes(self.original)
        (self.board / 'live.json').write_text(json.dumps(protection(OLD)), encoding='utf-8')
        self.fake = self.board / 'fake-gh.ps1'
        self.fake.write_text(r'''
[CmdletBinding(PositionalBinding=$false)]
param([Parameter(Position=0, ValueFromRemainingArguments=$true)][string[]]$Argv,
      [Parameter(ValueFromPipeline=$true)][string]$InputJson)
$ErrorActionPreference = 'Stop'
$dir = $env:MLV_REQUIRED_CHECKS_TEST_DIR
if ($Argv[0] -cne 'api' -or $Argv[1] -cne 'repos/SuperDimensionalSpaceFortress/MLV-App/branches/master/protection/required_status_checks') { exit 77 }
$method = $Argv[[array]::IndexOf($Argv, '--method') + 1]
Add-Content -LiteralPath (Join-Path $dir 'calls.txt') -Value $method
if (Test-Path (Join-Path $dir 'fail-api')) { exit 42 }
$live = Join-Path $dir 'live.json'
if ($method -ceq 'PATCH') {
    if ($Argv[-2] -cne '--input' -or $Argv[-1] -cne '-') { exit 78 }
    $raw = $InputJson
    [IO.File]::WriteAllText((Join-Path $dir 'request.json'), $raw)
    $request = $raw | ConvertFrom-Json -AsHashtable
    $request.contexts = @($request.checks | ForEach-Object { $_.context })
    if (Test-Path (Join-Path $dir 'corrupt-strict-after-patch')) { $request.strict = $false }
    if (Test-Path (Join-Path $dir 'corrupt-app-id-after-patch')) { $request.checks[0].app_id = 1 }
    [IO.File]::WriteAllText($live, ($request | ConvertTo-Json -Depth 8))
    if (Test-Path (Join-Path $dir 'fail-after-patch')) { exit 43 }
}
Get-Content -LiteralPath $live -Raw
exit 0
''', encoding='utf-8')

    def write(self, name, payload):
        (self.receipts / name).write_text(json.dumps(payload), encoding='utf-8')

    def read(self, name):
        return json.loads((self.receipts / name).read_text(encoding='utf-8'))

    def run_actor(self, apply=False):
        env = os.environ.copy()
        env['MLV_REQUIRED_CHECKS_TEST_DIR'] = str(self.board)
        command = [PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(SCRIPT),
                   '-BoardRoot', str(self.board), '-GhExe', str(self.fake)]
        if apply:
            command.append('-Apply')
        return subprocess.run(command, capture_output=True, text=True, encoding='utf-8', env=env, timeout=25)

    def assert_refused_without_api(self, phrase):
        before = {p.relative_to(self.board): p.read_bytes() for p in self.board.rglob('*') if p.is_file()}
        result = self.run_actor(True)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn(phrase, result.stderr + result.stdout)
        self.assertFalse((self.board / 'calls.txt').exists())
        after = {p.relative_to(self.board): p.read_bytes() for p in self.board.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_preview_is_read_only_and_board_root_really_resolves_receipts(self):
        before = {p: p.read_bytes() for p in self.board.rglob('*') if p.is_file()}
        result = self.run_actor()
        self.assertEqual(0, result.returncode, result.stderr)
        preview = json.loads(result.stdout)
        self.assertFalse(preview['applies'])
        self.assertIs(preview['body']['strict'], True)
        self.assertEqual(NEW, [c['context'] for c in preview['body']['checks']])
        self.assertTrue(all(c['app_id'] == 15368 for c in preview['body']['checks']))
        self.assertEqual(before, {p: p.read_bytes() for p in self.board.rglob('*') if p.is_file()})

    def test_missing_falsifier_and_guardrail_each_refuse_without_writes(self):
        for name in ('0.4a-batch-compile-falsifier.json', '0.4c-guardrail-move.json',
                     'execution-control-0.4c-i.json'):
            with self.subTest(name=name):
                path = self.receipts / name
                original = path.read_bytes()
                path.unlink()
                try:
                    self.assert_refused_without_api(name + ' is absent')
                finally:
                    path.write_bytes(original)

    def test_refusal_is_a_single_plain_unwrapped_stderr_line(self):
        # Regression for a hosted-CI-only failure: `throw` reaches PowerShell's
        # default terminating-error host view, which on some hosted-CI
        # terminals (observed Ubuntu, never Windows) adds ANSI color codes and
        # wraps at the console width -- splitting a contiguous refusal phrase
        # across multiple lines and breaking a plain substring match. Assert
        # the exact single plain line so color/width regressions fail loudly
        # instead of only on a narrow hosted runner.
        name = '0.4a-batch-compile-falsifier.json'
        path = self.receipts / name
        original = path.read_bytes()
        path.unlink()
        try:
            result = self.run_actor(True)
        finally:
            path.write_bytes(original)
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertNotIn('\x1b', result.stderr)
        lines = result.stderr.splitlines()
        self.assertEqual(1, len(lines), result.stderr)
        self.assertEqual('set-required-checks refused: ' + name + ' is absent', lines[0])
        self.assertEqual('', result.stdout)
        self.assertFalse((self.board / 'calls.txt').exists())

    def test_guardrail_head_must_match_reviewed_head_not_merge_sha(self):
        # Fixture proof that a genuinely valid receipt chain (reviewedHeadSha ==
        # guardrail headSha, mergeSha legitimately different) is accepted -- this
        # also proves the OLD comparison (headSha against mergeSha) would have
        # refused this exact valid chain, since mergeSha != headSha here.
        name = 'execution-control-0.4c-i.json'
        control = self.read(name)
        guardrail = self.read('0.4c-guardrail-move.json')
        self.assertEqual(control['reviewedHeadSha'], guardrail['headSha'])
        self.assertNotEqual(control['mergeSha'], guardrail['headSha'],
                             'fixture must exercise reviewedHeadSha != mergeSha, the real-world shape')
        result = self.run_actor()
        self.assertEqual(0, result.returncode, result.stderr)

    def test_wrong_reviewed_head_is_refused(self):
        name = 'execution-control-0.4c-i.json'
        original = self.read(name)
        self.write(name, {**original, 'reviewedHeadSha': 'f' * 40})
        self.assert_refused_without_api('guardrail head is not bound to its execution-control-0.4c-i receipt')
        self.write(name, original)

    def test_wrong_merge_sha_alone_no_longer_causes_a_refusal(self):
        # Documents the fix: mergeSha diverging from headSha is expected and must
        # not, by itself, refuse -- only a mismatched reviewedHeadSha may.
        name = 'execution-control-0.4c-i.json'
        original = self.read(name)
        self.write(name, {**original, 'mergeSha': '8' * 40})
        result = self.run_actor()
        self.assertEqual(0, result.returncode, result.stderr)
        self.write(name, original)

    def test_malformed_receipt_json_refuses_without_writes(self):
        name = '0.4a-workflow-base.json'
        path = self.receipts / name
        original = path.read_bytes()
        try:
            path.write_text('{not valid json', encoding='utf-8')
            self.assert_refused_without_api(name + ' is malformed JSON')
            path.write_text(json.dumps(['not', 'an', 'object']), encoding='utf-8')
            self.assert_refused_without_api(name + ' is not a JSON object')
        finally:
            path.write_bytes(original)

    def test_wrong_falsification_is_never_permission_to_patch(self):
        name = '0.4a-batch-compile-falsifier.json'
        original = self.read(name)
        for field, value in (('workflowBaseSha', 'f' * 40), ('headSha', 'a' * 40),
                             ('failingContext', 'Factory Bridge Regressions'),
                             ('failingStep', 'Install Qt'), ('conclusion', 'success')):
            with self.subTest(field=field):
                self.write(name, {**original, field: value})
                self.assert_refused_without_api('falsifier does not prove Batch Compile')
        self.write(name, original)

    def test_zero_or_wrong_guardrail_coverage_refuses(self):
        name = '0.4c-guardrail-move.json'
        original = self.read(name)
        for field, value in (('collectedTests', 0), ('collectedTests', '176'),
                             ('collectedTests', True), ('conclusion', 'failure'),
                             ('requiredJobs', ['Factory Bridge Regressions'])):
            with self.subTest(field=field, value=value):
                self.write(name, {**original, field: value})
                self.assert_refused_without_api('guardrail receipt lacks successful nonempty Windows coverage')
        self.write(name, original)

    def test_actor_hash_and_review_are_enforced(self):
        name = 'execution-control-0.4b-i.json'
        original = self.read(name)
        wrong = copy.deepcopy(original)
        wrong['hashes']['tools/coordination/set-required-checks.ps1'] = '0' * 64
        self.write(name, wrong)
        self.assert_refused_without_api('actor hash differs')
        self.write(name, original)
        (self.board / 'approval.json').write_text(json.dumps({'verdict': 'APPROVE', 'subject_sha': 'f' * 40}), encoding='utf-8')
        self.assert_refused_without_api('actor approval is not bound')

    def test_apply_appends_without_changing_previous_evidence(self):
        result = self.run_actor(True)
        self.assertEqual(0, result.returncode, result.stderr)
        request = json.loads((self.board / 'request.json').read_text(encoding='utf-8'))
        self.assertEqual({'strict', 'checks'}, set(request))
        self.assertIs(request['strict'], True)
        self.assertEqual(NEW, [c['context'] for c in request['checks']])
        self.assertTrue(all(c['app_id'] == 15368 for c in request['checks']))
        updated = self.snapshot.read_bytes()
        self.assertTrue(updated.startswith(self.original))
        self.assertGreater(len(updated), len(self.original))
        final = self.read('0.4b-required-checks.json')
        self.assertEqual(OLD, final['preContexts'])
        self.assertEqual(NEW, final['postContexts'])
        self.assertEqual(hashlib.sha256(updated.splitlines()[-1]).hexdigest(), final['snapshotRowSha256'])
        self.assertRegex(final['recordedUtc'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        prior_files = {p: p.read_bytes() for p in self.board.rglob('*') if p.is_file()}
        second = self.run_actor(True)
        self.assertNotEqual(0, second.returncode)
        self.assertIn('transition receipt already exists', second.stderr)
        self.assertEqual(prior_files, {p: p.read_bytes() for p in self.board.rglob('*') if p.is_file()})

    def test_unexpected_live_context_fails_without_patch(self):
        (self.board / 'live.json').write_text(json.dumps(protection(OLD + ['Extra required check'])), encoding='utf-8')
        result = self.run_actor(True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('contexts differ', result.stderr)
        self.assertEqual(['GET'], (self.board / 'calls.txt').read_text().splitlines())
        self.assertEqual(self.original, self.snapshot.read_bytes())
        self.assertFalse((self.receipts / '0.4b-required-checks.json').exists())

    def test_api_failure_cannot_create_success_receipt(self):
        (self.board / 'fail-api').touch()
        result = self.run_actor(True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('GitHub GET failed', result.stderr)
        self.assertFalse((self.receipts / '0.4b-required-checks.json').exists())
        self.assertEqual(self.original, self.snapshot.read_bytes())

    def test_bad_post_patch_strict_response_refuses_and_preserves_evidence(self):
        (self.board / 'corrupt-strict-after-patch').touch()
        result = self.run_actor(True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('must retain strict=true', result.stderr)
        self.assertEqual(['GET', 'PATCH', 'GET'], (self.board / 'calls.txt').read_text().splitlines())
        self.assertFalse((self.receipts / '0.4b-required-checks.json').exists())
        self.assertEqual(self.original, self.snapshot.read_bytes())

    def test_bad_post_patch_app_id_response_refuses_and_preserves_evidence(self):
        (self.board / 'corrupt-app-id-after-patch').touch()
        result = self.run_actor(True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('must bind every check to app_id 15368', result.stderr)
        self.assertEqual(['GET', 'PATCH', 'GET'], (self.board / 'calls.txt').read_text().splitlines())
        self.assertFalse((self.receipts / '0.4b-required-checks.json').exists())
        self.assertEqual(self.original, self.snapshot.read_bytes())

    def test_successful_patch_with_lost_response_resumes_without_second_patch(self):
        marker = self.board / 'fail-after-patch'
        marker.touch()
        result = self.run_actor(True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('GitHub PATCH failed', result.stderr)
        self.assertTrue((self.receipts / '0.4b-transition-intent.json').exists())
        self.assertFalse((self.receipts / '0.4b-required-checks.json').exists())
        self.assertEqual(self.original, self.snapshot.read_bytes())
        marker.unlink()
        retry = self.run_actor(True)
        self.assertEqual(0, retry.returncode, retry.stderr)
        self.assertEqual(1, (self.board / 'calls.txt').read_text().splitlines().count('PATCH'))
        self.assertTrue((self.receipts / '0.4b-required-checks.json').exists())


if __name__ == '__main__':
    unittest.main()
