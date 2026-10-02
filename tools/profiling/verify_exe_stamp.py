#!/usr/bin/env python3
"""Does this MLVApp.exe carry the commit it claims? (stdlib only)

An evidence build must identify its subject. The app embeds a uniquely tagged stamp
`MLVAPP_BUILDSTAMP_v1|sha=<40 hex>|dirty=<0|1>` (tools/gen-buildinfo.ps1 writes it into build_buildinfo.h at BUILD
time, MainWindow.cpp compiles it in); a build that skipped the generator carries `sha=unknown`, or an older SHA
that qmake pinned long ago -- that is how PR #222 r1's binary could not be tied to its subject commit.

usage: verify_exe_stamp.py <exe> --sha <40-hex subject sha>
exit 0 = the stamp is present and equals the subject, 1 = missing / unknown / different SHA, 2 = unreadable exe.
Prints one line: the stamp, the dirty flag and the SHA-256 of the executable (so a copy can be told from the build).
"""
import argparse
import hashlib
import re
import sys

STAMP = re.compile(rb"MLVAPP_BUILDSTAMP_v1\|sha=([0-9a-f]{40}|unknown)\|dirty=([01])")


def read_stamp(path):
    """-> (sha, dirty, sha256_hex) of the LAST stamp in the file, or (None, None, sha256_hex) when there is none."""
    with open(path, "rb") as handle:
        data = handle.read()
    digest = hashlib.sha256(data).hexdigest()
    found = STAMP.findall(data)
    if not found:
        return None, None, digest
    sha, dirty = found[-1]
    return sha.decode("ascii"), int(dirty), digest


def check(path, subject_sha):
    """-> (ok, message)."""
    try:
        sha, dirty, digest = read_stamp(path)
    except OSError as error:
        return False, "cannot read %s: %s" % (path, error)
    if not re.fullmatch(r"[0-9a-f]{40}", subject_sha or ""):
        return False, "the subject sha must be 40 lowercase hex digits, got %r" % (subject_sha,)
    if sha is None:
        return False, "no MLVAPP_BUILDSTAMP_v1 stamp in %s (sha256 %s)" % (path, digest)
    if sha != subject_sha:
        return False, "stamp sha=%s dirty=%d is not the subject %s (sha256 %s)" % (sha, dirty, subject_sha, digest)
    return True, "stamp sha=%s dirty=%d sha256=%s" % (sha, dirty, digest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("exe")
    parser.add_argument("--sha", required=True)
    args = parser.parse_args(argv)
    ok, message = check(args.exe, args.sha)
    print(("OK " if ok else "FAIL ") + message)
    if ok:
        return 0
    return 2 if message.startswith("cannot read") else 1


if __name__ == "__main__":
    sys.exit(main())
