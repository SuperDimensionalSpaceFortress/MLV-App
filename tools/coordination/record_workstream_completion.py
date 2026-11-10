#!/usr/bin/env python3
import argparse, hashlib, json, math, os, subprocess, sys, tempfile
from datetime import datetime, timezone
from pathlib import PurePosixPath

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

SCHEMA='mlv-app/workstream-completion/v1'
LANE='mlv-app/fleet-lane-receipt/v1'
REVIEW='mlv-app/workstream-review/v1'
TEST='mlv-app/workstream-test/v1'
HEX40=set('0123456789abcdef')
HEX64=HEX40


def fail(code, detail=''):
    print(json.dumps({'error':code,'detail':str(detail)},separators=(',',':')),file=sys.stderr)
    raise SystemExit(2)


def pairs(items):
    out={}
    for key,value in items:
        if key in out: raise ValueError('duplicate key: '+key)
        out[key]=value
    return out


def reject_constant(value):
    raise ValueError('non-finite number: '+value)


def digest(raw): return hashlib.sha256(raw).hexdigest()
def sha40(value): return isinstance(value,str) and len(value)==40 and set(value.lower())<=HEX40
def sha64(value): return isinstance(value,str) and len(value)==64 and set(value.lower())<=HEX64
def integer(value): return type(value) is int


def canonical(path):
    try: return os.path.realpath(os.path.abspath(os.fspath(path)))
    except (OSError,TypeError,ValueError) as exc: fail('invalid-filesystem-path',exc)


def same_fs(a,b): return os.path.normcase(canonical(a))==os.path.normcase(canonical(b))


def relative_path(value):
    if not isinstance(value,str) or not value or '\\' in value or '\x00' in value:
        fail('invalid-repo-relative-path',repr(value))
    path=PurePosixPath(value)
    if path.is_absolute() or path.parts in ((),('.',)) or any(x in ('','.','..') for x in path.parts):
        fail('invalid-repo-relative-path',value)
    if ':' in path.parts[0] or any(x in value for x in '*?[]'):
        fail('invalid-repo-relative-path',value)
    normalized=path.as_posix()
    if normalized!=value: fail('noncanonical-repo-relative-path',value)
    return normalized


class Registry:
    def __init__(self,output):
        self.output=canonical(output)
        self.files={}
    def capture(self,path,code='missing-file'):
        resolved=canonical(path)
        if same_fs(resolved,self.output): fail('output-overlaps-input',resolved)
        prior=self.files.get(resolved)
        if prior is not None: return prior
        try:
            with open(resolved,'rb') as stream: raw=stream.read()
        except (OSError,ValueError) as exc: fail(code,f'{resolved}: {exc}')
        item={'path':resolved,'bytes':len(raw),'sha256':digest(raw),'raw':raw}
        self.files[resolved]=item
        return item
    def parse(self,path,code='malformed-json'):
        item=self.capture(path)
        try:
            value=json.loads(item['raw'].decode('utf-8','strict'),object_pairs_hook=pairs,parse_constant=reject_constant)
        except (UnicodeError,ValueError,TypeError) as exc: fail(code,f"{item['path']}: {exc}")
        if not isinstance(value,dict): fail(code,f"{item['path']}: root must be object")
        return item,value
    def descriptors(self):
        return [{'path':x['path'],'bytes':x['bytes'],'sha256':x['sha256']} for x in sorted(self.files.values(),key=lambda y:y['path'])]
    def verify(self,code):
        for item in self.files.values():
            try:
                with open(item['path'],'rb') as stream: raw=stream.read()
            except (OSError,ValueError) as exc: fail(code,f"{item['path']}: {exc}")
            if raw!=item['raw']: fail(code,item['path'])


def required(obj,keys,code):
    missing=[key for key in keys if key not in obj]
    if missing: fail(code,'missing: '+','.join(missing))


def git(worktree,*args,allow_failure=False):
    try:
        proc=subprocess.run(['git','-C',worktree,*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,check=False, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired: fail('git-timeout',' '.join(args))
    except OSError as exc: fail('git-unavailable',exc)
    if proc.returncode and not allow_failure:
        fail('git-readonly-failure',proc.stderr.decode('utf-8','replace'))
    return proc


def git_text(worktree,*args):
    raw=git(worktree,*args).stdout
    try: return raw.decode('utf-8','strict').strip()
    except UnicodeError as exc: fail('git-invalid-utf8',exc)


def repo_state(worktree,head):
    current=git_text(worktree,'rev-parse','--verify','HEAD')
    if current!=head: fail('repository-head-changed',f'{head} -> {current}')
    status=git(worktree,'status','--porcelain=v1','-z','--untracked-files=all').stdout
    if status: fail('dirty-worktree','')


def changed_paths(worktree,base,head):
    raw=git(worktree,'diff','--name-only','-z','--no-renames',base,head,'--').stdout
    try: names=raw.decode('utf-8','strict').split('\0')
    except UnicodeError as exc: fail('git-invalid-utf8',exc)
    if names and names[-1]=='': names.pop()
    return sorted(set(relative_path(name) for name in names))


def bound_file(registry,path,expected_bytes,expected_hash,code):
    if not integer(expected_bytes) or expected_bytes<0 or not sha64(expected_hash): fail(code,'invalid bytes/hash')
    item=registry.capture(path)
    if item['bytes']!=expected_bytes or item['sha256']!=expected_hash.lower(): fail(code,item['path'])
    return item


def validate_existing(raw,expected_body):
    try:
        value=json.loads(raw.decode('utf-8','strict'),object_pairs_hook=pairs,parse_constant=reject_constant)
    except (UnicodeError,ValueError,TypeError) as exc: fail('malformed-existing-receipt',exc)
    if not isinstance(value,dict): fail('malformed-existing-receipt','root must be object')
    expected_keys=set(expected_body)|{'semanticDigest','recordedUtc'}
    if set(value)!=expected_keys or value.get('schema')!=SCHEMA or value.get('decision')!='reviewed-ready':
        fail('invalid-existing-receipt-schema','unexpected fields or constants')
    recorded=value.get('recordedUtc')
    if not isinstance(recorded,str): fail('invalid-existing-recorded-utc','not a string')
    try:
        parsed=datetime.fromisoformat(recorded.replace('Z','+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None: raise ValueError('timezone required')
    except ValueError as exc: fail('invalid-existing-recorded-utc',exc)
    body={key:value[key] for key in expected_body}
    body_bytes=json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    expected_bytes=json.dumps(expected_body,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    if body_bytes!=expected_bytes: fail('conflicting-existing-receipt','semantic body differs')
    expected_digest=digest(body_bytes)
    if value.get('semanticDigest')!=expected_digest: fail('invalid-existing-semantic-digest','')
    return value


def read_existing(path):
    try:
        with open(path,'rb') as stream: return stream.read()
    except FileNotFoundError: return None
    except (OSError,ValueError) as exc: fail('existing-receipt-read-failure',exc)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--card-id',required=True)
    parser.add_argument('--lane-receipt',required=True)
    parser.add_argument('--review-verdict',required=True)
    parser.add_argument('--worktree',required=True)
    parser.add_argument('--allowed-path',action='append',required=True)
    parser.add_argument('--test-receipt',action='append',required=True)
    parser.add_argument('--artifact',action='append',default=[])
    parser.add_argument('--output-receipt',required=True)
    args=parser.parse_args()
    if not args.card_id: fail('invalid-card-id','empty')
    worktree=canonical(args.worktree)
    output=canonical(args.output_receipt)
    parent=os.path.dirname(output)
    if not os.path.isdir(parent): fail('output-parent-missing',parent)
    registry=Registry(output)

    allowed=sorted(set(relative_path(path) for path in args.allowed_path))
    lane_info,lane=registry.parse(args.lane_receipt,'malformed-lane-receipt')
    required(lane,['schema','card','workDir','allowEdits','state','complete','exitCode','failure','providerRefusal','timedOut','baseSha','outputPath','outputBytes','outputSha256'],'invalid-lane-receipt')
    if lane['schema']!=LANE or lane['card']!=args.card_id: fail('lane-card-mismatch','')
    if not isinstance(lane['workDir'],str) or not same_fs(lane['workDir'],worktree) or lane['allowEdits'] is not True:
        fail('lane-workdir-or-edit-mismatch','')
    if lane['state']!='complete' or lane['complete'] is not True or not integer(lane['exitCode']) or lane['exitCode']!=0:
        fail('lane-not-successful','')
    if lane['failure'] is not None or lane['providerRefusal'] is not None or lane['timedOut'] is not False:
        fail('lane-refused-or-timed-out','')
    base=lane['baseSha'].lower() if sha40(lane['baseSha']) else fail('invalid-base-sha','')
    head=git_text(worktree,'rev-parse','--verify','HEAD')
    if not sha40(head): fail('invalid-head-sha',head)
    head=head.lower()
    if base==head: fail('base-equals-head','')
    if git(worktree,'merge-base','--is-ancestor',base,head,allow_failure=True).returncode!=0:
        fail('base-not-ancestor','')
    repo_state(worktree,head)
    changed=changed_paths(worktree,base,head)
    if not changed: fail('no-source-changes','')
    outside=sorted(set(changed)-set(allowed))
    if outside: fail('changed-paths-not-allowed',','.join(outside))

    lane_output=bound_file(registry,lane['outputPath'],lane['outputBytes'],lane['outputSha256'],'lane-output-mismatch')
    review_info,review=registry.parse(args.review_verdict,'malformed-review-verdict')
    required(review,['schema','verdict','cardId','subject_sha','laneOutputSha256'],'invalid-review-verdict')
    if review['schema']!=REVIEW or review['verdict']!='APPROVE' or review['cardId']!=args.card_id:
        fail('review-not-approved','')
    if review['subject_sha']!=head or not sha64(review['laneOutputSha256']) or review['laneOutputSha256'].lower()!=lane_output['sha256']:
        fail('review-not-bound','')

    tests=[]
    for path in args.test_receipt:
        receipt_info,test=registry.parse(path,'malformed-test-receipt')
        required(test,['schema','cardId','subject_sha','exitCode','commandPath','commandSha256','outputPath','outputBytes','outputSha256'],'invalid-test-receipt')
        if test['schema']!=TEST or test['cardId']!=args.card_id or test['subject_sha']!=head:
            fail('test-not-bound',receipt_info['path'])
        if not integer(test['exitCode']) or test['exitCode']!=0: fail('test-failed',receipt_info['path'])
        if not sha64(test['commandSha256']): fail('test-command-mismatch',receipt_info['path'])
        command=registry.capture(test['commandPath'])
        if command['sha256']!=test['commandSha256'].lower(): fail('test-command-mismatch',receipt_info['path'])
        output_info=bound_file(registry,test['outputPath'],test['outputBytes'],test['outputSha256'],'test-output-mismatch')
        tests.append({'receiptPath':receipt_info['path'],'receiptSha256':receipt_info['sha256'],'commandPath':command['path'],'commandSha256':command['sha256'],'outputPath':output_info['path'],'outputBytes':output_info['bytes'],'outputSha256':output_info['sha256'],'subjectSha':head})
    tests.sort(key=lambda item:item['receiptPath'])

    artifacts=[]
    for path in args.artifact:
        item=registry.capture(path,'missing-artifact')
        artifacts.append({'path':item['path'],'bytes':item['bytes'],'sha256':item['sha256']})
    artifacts.sort(key=lambda item:item['path'])

    body={'schema':SCHEMA,'decision':'reviewed-ready','cardId':args.card_id,'subjectSha':head,'baseSha':base,'changedPaths':changed,'allowedPaths':allowed,'laneReceiptPath':lane_info['path'],'laneReceiptSha256':lane_info['sha256'],'reviewPath':review_info['path'],'reviewSha256':review_info['sha256'],'tests':tests,'artifacts':artifacts,'snapshotFiles':registry.descriptors()}
    semantic=digest(json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8'))

    registry.verify('inputs-changed-before-publication')
    repo_state(worktree,head)
    existing=read_existing(output)
    if existing is not None:
        validate_existing(existing,body)
        registry.verify('inputs-changed-during-replay')
        repo_state(worktree,head)
        if read_existing(output)!=existing: fail('receipt-changed-during-replay','')
        sys.stdout.buffer.write(existing)
        return

    result=dict(body)
    result['semanticDigest']=semantic
    result['recordedUtc']=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    data=(json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode('utf-8')
    temporary=None
    created=False
    returned=data
    try:
        fd,temporary=tempfile.mkstemp(prefix='.completion.',dir=parent)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary,output)
            created=True
        except FileExistsError:
            returned=read_existing(output)
            if returned is None: fail('publication-race-read-failure','')
            validate_existing(returned,body)
        readback=read_existing(output)
        if readback is None: fail('publication-readback-missing','')
        if created and readback!=data: fail('publication-readback-mismatch','')
        if not created and readback!=returned: fail('publication-race-changed','')
        registry.verify('stale-after-publication')
        repo_state(worktree,head)
        validate_existing(readback,body)
        if read_existing(output)!=readback: fail('receipt-changed-after-publication','')
        sys.stdout.buffer.write(readback)
    finally:
        if temporary is not None:
            try: os.unlink(temporary)
            except FileNotFoundError: pass
            except OSError: pass


if __name__=='__main__':
    try: main()
    except SystemExit: raise
    except Exception as exc: fail('unexpected-local-error',f'{type(exc).__name__}: {exc}')
