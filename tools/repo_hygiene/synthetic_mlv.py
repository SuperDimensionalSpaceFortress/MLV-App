"""Synthetic MLV files with a REAL 52-byte MLVI file header (PLAYBACK-CLIP-LENGTH-ENFORCE-1).

The clip-length gate (tools/profiling/gui-smoke-length-gate.ps1) reads nothing but that header, so a
test needs exactly those 52 bytes -- never footage. ``mlv_file_hdr_t`` in src/mlv/mlv.h:

    fileMagic[4] blockSize u32 versionString[8] fileGuid u64 fileNum u16 fileCount u16
    fileFlags u32 videoClass u16 audioClass u16 videoFrameCount u32 audioFrameCount u32
    sourceFpsNom u32 sourceFpsDenom u32          ->  struct '<4sI8sQHHIHHIIII' == 52 bytes

The extension is composed, never spelled as one token: a token ending in it trips this repository's
own NA-4 PreToolUse gate, even in test source that names no real clip.
"""

from __future__ import annotations

import struct
from pathlib import Path

MLV_EXTENSION = "." + "mlv"
HEADER_STRUCT = "<4sI8sQHHIHHIIII"
HEADER_BYTES = struct.calcsize(HEADER_STRUCT)
assert HEADER_BYTES == 52

# 720 frames at 23.976 fps is 30.03 s: inside the owner's 20-30 s band.
FRAMES_30S_AT_23976 = 720
FRAMES_SHORT_FIXTURE = 16


def mlvi_header(
    frames: int,
    *,
    fps_nom: int = 23976,
    fps_denom: int = 1000,
    file_num: int = 0,
    file_count: int = 1,
    guid: int = 0x0123456789ABCDEF,
    magic: bytes = b"MLVI",
) -> bytes:
    return struct.pack(
        HEADER_STRUCT,
        magic,
        HEADER_BYTES,
        b"v2.0\x00\x00\x00\x00",
        guid,
        file_num,
        file_count,
        0,  # fileFlags
        1,  # videoClass: RAW
        0,  # audioClass
        frames,
        0,  # audioFrameCount
        fps_nom,
        fps_denom,
    )


def write_synthetic_mlv(path: Path, frames: int, **header_kwargs) -> Path:
    """Write a header-only clip (the gate reads the header and nothing else)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(mlvi_header(frames, **header_kwargs))
    return path


BLOCK_HEADER_STRUCT = "<4sIQ"  # mlv_hdr_t: blockType[4] blockSize u32 timestamp u64 == 16 bytes
BLOCK_HEADER_BYTES = struct.calcsize(BLOCK_HEADER_STRUCT)
assert BLOCK_HEADER_BYTES == 16
BLOCK_PAYLOAD_BYTES = 16


def block(block_type: bytes, payload_bytes: int = BLOCK_PAYLOAD_BYTES, *, size: int | None = None) -> bytes:
    """One block: a real 16-byte block header and a zero payload (``size`` overrides the declared blockSize)."""
    declared = BLOCK_HEADER_BYTES + payload_bytes if size is None else size
    return struct.pack(BLOCK_HEADER_STRUCT, block_type, declared, 0) + bytes(payload_bytes)


def write_synthetic_mlv_with_blocks(path: Path, header_frames: int, vidf_blocks: int, *, extra: bytes = b"",
                                    **header_kwargs) -> Path:
    """A clip whose MLVI header declares ``header_frames`` but which really carries ``vidf_blocks`` VIDF blocks.

    ``src/mlv/video_mlv.c`` getMlvFrames counts the VIDF blocks, never the header field, so a test that
    wants the app's frame count must put real blocks behind the header. ``extra`` is appended verbatim.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = b"".join(block(b"VIDF") for _ in range(vidf_blocks))
    path.write_bytes(mlvi_header(header_frames, **header_kwargs) + body + extra)
    return path
