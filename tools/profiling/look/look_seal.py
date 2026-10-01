#!/usr/bin/env python3
"""Sealing of everything a blind judge must not read (round 2, sol B3 / fable H2). Pure standard library.

THE PROBLEM: a judge process that is a general agent (Codex, Claude with tools) can read any file its user can read.
Flags that ask the CLI not to are policy. This module makes the secrets *unreadable at judge time*, whatever the judge
tries: after `build-session` the session directory holds only what a judge may see (the pair images, the judge-facing
manifest, a minimal public session.json) plus ONE sealed file. The answer key, the full session record, the prepared
source frames and the degraded sources live ONLY inside that file, encrypted and authenticated with a per-session key
that is printed once at build time and never written into the session directory. The judge commands refuse to start
when the key is in their environment, scrub it from the judge's environment, and refuse a session directory that
still holds a plaintext secret. `tally` takes the key, verifies the seal and reads the secrets from memory.

CIPHER (stdlib only, so it runs wherever the hygiene tests run): HMAC-SHA256 in counter mode as the keystream
(a PRF in CTR mode is a stream cipher), a fresh 16-byte random nonce per seal, encrypt-then-MAC with an independent
key derived from the session key. It is built from audited primitives but is a purpose-built container, not a general
crypto library: it exists so the answer key is unreadable without the key, not to protect data for years.

CONTAINER   MAGIC | nonce(16) | E( header_len(8) | header_json | member bytes ... ) | HMAC-SHA256(MAGIC|nonce|E(...))
            header_json = {"members": [{"name", "size", "sha256"}, ...]}
"""
import hashlib
import hmac
import json
import os
import secrets

MAGIC = b"LOOKSEAL1\n"
SEALED_NAME = "sealed.bin"
KEY_ENV = "LOOK_SEAL_KEY"
FULL_SESSION_MEMBER = "session.full.json"
ANSWER_KEY_MEMBER = "answer_key.json"
# What a judge-facing directory must NOT contain once sealed (the plaintext secrets build_session writes).
PLAINTEXT_SECRETS = ("answer_key.json", "source-frames", "degraded-sources")
PUBLIC_SESSION_FIELDS = ("schema", "rubricSha256", "orderSeed", "itemCount", "imageSha256s", "createdUtc", "gutter",
                         "itemIdBinding", "emittedTwiceOrderSwapped")
IDENTITY_FIELDS = ("rubricSha256", "orderSeed", "itemCount", "imageSha256s")
_CHUNK = 1 << 20
_HEX = frozenset("0123456789abcdef")


class SealError(ValueError):
    pass


def new_key():
    return secrets.token_hex(32)


def parse_key(text):
    text = (text or "").strip().lower()
    if len(text) != 64 or not set(text) <= _HEX:
        raise SealError("the seal key must be 64 hex characters (what build-session printed)")
    return bytes.fromhex(text)


def _derive(key, label):
    return hmac.digest(key, b"look-seal-" + label, "sha256")


class _Stream:
    """Keystream = HMAC-SHA256(enc_key, nonce | counter). apply() XORs the next len(data) keystream bytes."""

    def __init__(self, enc_key, nonce):
        self._key, self._nonce, self._counter, self._spare = enc_key, nonce, 0, b""

    def apply(self, data):
        need = len(data)
        if need == 0:
            return b""
        blocks, have = [self._spare], len(self._spare)
        while have < need:
            block = hmac.digest(self._key, self._nonce + self._counter.to_bytes(8, "big"), "sha256")
            self._counter += 1
            blocks.append(block)
            have += len(block)
        stream = b"".join(blocks)
        stream, self._spare = stream[:need], stream[need:]
        return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(need, "big")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sealed_path(session_dir):
    return os.path.join(session_dir, SEALED_NAME)


def seal_files(out_path, key_hex, members):
    """Write a sealed container holding `members` = [(name, source path)] to out_path. Returns the member records."""
    key = parse_key(key_hex)
    names = [name for name, _ in members]
    if len(set(names)) != len(names):
        raise SealError("duplicate member names")
    records = [{"name": name, "size": os.path.getsize(path), "sha256": _sha256_file(path)} for name, path in members]
    header = json.dumps({"members": records}, sort_keys=True).encode("utf-8")
    nonce = secrets.token_bytes(16)
    stream = _Stream(_derive(key, b"enc"), nonce)
    mac = hmac.new(_derive(key, b"mac"), digestmod="sha256")
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as out:
        def emit(data):
            cipher = stream.apply(data)
            mac.update(cipher)
            out.write(cipher)

        out.write(MAGIC + nonce)
        mac.update(MAGIC + nonce)
        emit(len(header).to_bytes(8, "big") + header)
        for (_, path) in members:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(_CHUNK), b""):
                    emit(chunk)
        out.write(mac.digest())
    os.replace(tmp, out_path)
    return records


def _verify_tag(path, key):
    size = os.path.getsize(path)
    if size < len(MAGIC) + 16 + 32:
        raise SealError("sealed file is too short")
    mac = hmac.new(_derive(key, b"mac"), digestmod="sha256")
    with open(path, "rb") as handle:
        remaining = size - 32
        while remaining:
            chunk = handle.read(min(_CHUNK, remaining))
            if not chunk:
                raise SealError("sealed file is truncated")
            mac.update(chunk)
            remaining -= len(chunk)
        tag = handle.read(32)
    if not hmac.compare_digest(mac.digest(), tag):
        raise SealError("the seal does not verify: wrong key, or the sealed file was changed")


def read_members(path, key_hex, wanted=None, sink=None):
    """Verify the container, then return {name: bytes} for the `wanted` members (all when None). With `sink`
    (a callable name -> binary file object or None) members stream to it instead of memory."""
    key = parse_key(key_hex)
    _verify_tag(path, key)
    out = {}
    with open(path, "rb") as handle:
        if handle.read(len(MAGIC)) != MAGIC:
            raise SealError("not a sealed file")
        nonce = handle.read(16)
        stream = _Stream(_derive(key, b"enc"), nonce)
        header_len = int.from_bytes(stream.apply(handle.read(8)), "big")
        if header_len > 64 * 1024 * 1024:
            raise SealError("sealed header is implausibly large")
        header = json.loads(stream.apply(handle.read(header_len)).decode("utf-8"))
        for record in header["members"]:
            name, left = record["name"], record["size"]
            take = wanted is None or name in wanted
            target = sink(name) if (take and sink is not None) else None
            digest, parts = hashlib.sha256(), []
            while left:
                plain = stream.apply(handle.read(min(_CHUNK, left)))
                left -= len(plain)
                digest.update(plain)
                if target is not None:
                    target.write(plain)
                elif take:
                    parts.append(plain)
            if digest.hexdigest() != record["sha256"]:
                raise SealError(f"member {name!r} does not match its recorded digest")
            if target is not None:
                target.close()
            elif take:
                out[name] = b"".join(parts)
    return out


def session_state(session_dir):
    """What a judge could find in this directory: is it sealed, and is any plaintext secret still there?"""
    present = [name for name in PLAINTEXT_SECRETS if os.path.exists(os.path.join(session_dir, name))]
    sealed = os.path.isfile(sealed_path(session_dir))
    return {"sealed": sealed, "plaintextPresent": present,
            "sealSha256": _sha256_file(sealed_path(session_dir)) if sealed else None}


def assert_judgeable(session_dir):
    """Raise SealError unless the session directory is sealed AND holds no plaintext secret. Returns the seal sha256."""
    state = session_state(session_dir)
    if not state["sealed"]:
        raise SealError(f"{session_dir!r} is not sealed ({SEALED_NAME} missing): a real judge must not run next to a "
                        "plaintext answer key; rebuild with build-session (sealing is the default)")
    if state["plaintextPresent"]:
        raise SealError(f"{session_dir!r} still holds plaintext secrets {state['plaintextPresent']} next to "
                        f"{SEALED_NAME}: a judge could read them; refusing")
    return state["sealSha256"]


def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def seal_session(session_dir, key_hex):
    """Seal a freshly built session. Moves the answer key, the full session record, the prepared source frames and the
    degraded sources into sealed.bin, removes the plaintext (only the files that were sealed, then the two now-empty
    directories), and rewrites session.json as the minimal public record. Returns {"sealSha256", "members"}."""
    parse_key(key_hex)
    session_path = os.path.join(session_dir, "session.json")
    key_path = os.path.join(session_dir, ANSWER_KEY_MEMBER)
    if os.path.exists(sealed_path(session_dir)):
        raise SealError(f"{session_dir!r} is already sealed")
    for needed in (session_path, key_path):
        if not os.path.isfile(needed):
            raise SealError(f"{needed!r} is missing: build the session first")
    full = _read_json(session_path)
    members = [(ANSWER_KEY_MEMBER, key_path), (FULL_SESSION_MEMBER, session_path)]
    for folder in ("source-frames", "degraded-sources"):
        base = os.path.join(session_dir, folder)
        for root, _dirs, files in os.walk(base):
            for name in sorted(files):
                full_path = os.path.join(root, name)
                members.append((os.path.relpath(full_path, session_dir).replace(os.sep, "/"), full_path))
    target = sealed_path(session_dir)
    records = seal_files(target, key_hex, members)
    check = read_members(target, key_hex, wanted={ANSWER_KEY_MEMBER})  # prove it opens before anything is removed
    if not check.get(ANSWER_KEY_MEMBER):
        raise SealError("the sealed file does not reopen")
    for _name, path in members:
        if path != session_path:
            os.remove(path)
    for folder in ("source-frames", "degraded-sources"):
        base = os.path.join(session_dir, folder)
        if os.path.isdir(base):
            for root, _dirs, _files in os.walk(base, topdown=False):
                os.rmdir(root)
    seal_sha = _sha256_file(target)
    public = {k: full[k] for k in PUBLIC_SESSION_FIELDS if k in full}
    public.update({"sealed": True, "sealSha256": seal_sha})
    with open(session_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(public, handle, indent=2)
        handle.write("\n")
    return {"sealSha256": seal_sha, "members": [r["name"] for r in records]}


def open_sealed(session_dir, key_hex):
    """(answer_key, full_session, sealSha256) for a sealed session, from memory. Raises SealError when the seal does not
    verify, a member is missing, or the public session.json disagrees with the sealed record on the session identity."""
    path = sealed_path(session_dir)
    if not os.path.isfile(path):
        raise SealError(f"{session_dir!r} has no {SEALED_NAME}: it was not built sealed")
    members = read_members(path, key_hex, wanted={ANSWER_KEY_MEMBER, FULL_SESSION_MEMBER})
    try:
        key = json.loads(members[ANSWER_KEY_MEMBER].decode("utf-8"))
        full = json.loads(members[FULL_SESSION_MEMBER].decode("utf-8"))
    except KeyError as exc:
        raise SealError(f"sealed file lacks {exc}") from exc
    public = _read_json(os.path.join(session_dir, "session.json"))
    seal_sha = _sha256_file(path)
    if public.get("sealed") is not True or public.get("sealSha256") != seal_sha:
        raise SealError("the public session.json does not name this sealed file (sealed/sealSha256 differ)")
    for field in IDENTITY_FIELDS:
        if public.get(field) != full.get(field):
            raise SealError(f"the public session.json disagrees with the sealed record on {field!r}")
    return key, full, seal_sha


def extract_all(session_dir, key_hex, dest_dir):
    """Audit helper: write every sealed member under dest_dir (which must not be inside the session directory)."""
    here, there = os.path.realpath(session_dir), os.path.realpath(dest_dir)
    if there == here or there.startswith(here + os.sep):
        raise SealError("extract outside the session directory: a judge may read it")

    def sink(name):
        target = os.path.join(dest_dir, *name.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        return open(target, "wb")

    read_members(sealed_path(session_dir), key_hex, sink=sink)
    return dest_dir


def key_from_args(explicit=None, key_file=None, environ=None):
    """The seal key for tally/extract: --seal-key, else --seal-key-file, else the environment. None when none is given."""
    environ = os.environ if environ is None else environ
    if explicit:
        return explicit
    if key_file:
        with open(key_file, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    return environ.get(KEY_ENV) or None
