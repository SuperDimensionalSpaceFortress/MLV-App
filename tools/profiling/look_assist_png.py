#!/usr/bin/env python3
"""Stdlib-only PNG reader and the deck-cast metric of the Look Assist picture gates.

The profile / sidecar checkers measure the CIELAB chroma of the tracked pool deck (rows 65-98 %, columns
2-25 % of a frame, mean colour; an ESTIMATE -- concrete is not a calibrated grey). They used to need Pillow,
which the hosted CI image does not pin, so the picture half could never run there. This module decodes the
8-bit RGB / RGBA, non-interlaced PNGs the app (Qt) and Pillow write, with zlib only, so the gate can run in CI
against a recorded app run.
"""
import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def read_png_rgb(path):
    """-> (width, height, rows) with rows a list of bytes objects of width*3 RGB bytes. ValueError if unsupported."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:8] != _SIGNATURE:
        raise ValueError("%s: not a PNG" % path)
    pos = 8
    ihdr = None
    idat = []
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if ihdr is None or not idat:
        raise ValueError("%s: truncated PNG" % path)
    width, height, depth, color, _compression, _filter, interlace = ihdr
    if depth != 8 or color not in (2, 6) or interlace != 0:
        raise ValueError("%s: only 8-bit RGB/RGBA non-interlaced PNGs are supported (depth=%d color=%d interlace=%d)"
                         % (path, depth, color, interlace))
    channels = 3 if color == 2 else 4
    stride = width * channels
    raw = zlib.decompress(b"".join(idat))
    if len(raw) < height * (stride + 1):
        raise ValueError("%s: short image data" % path)
    rows = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        kind = raw[offset]
        line = bytearray(raw[offset + 1:offset + 1 + stride])
        offset += stride + 1
        if kind == 1:      # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 255
        elif kind == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 255
        elif kind == 3:    # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 255
        elif kind == 4:    # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = previous[i]
                c = previous[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                predictor = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + predictor) & 255
        elif kind != 0:
            raise ValueError("%s: unknown PNG filter %d" % (path, kind))
        previous = line
        if channels == 4:
            rows.append(bytes(line[i] for i in range(stride) if i % 4 != 3))
        else:
            rows.append(bytes(line))
    return width, height, rows


def deck_region_mean_rgb(path):
    """Mean colour of the deck region of one frame (same region as the real-app sheet metrics)."""
    width, height, rows = read_png_rgb(path)
    x0, x1 = int(width * 0.02), int(width * 0.25)
    y0, y1 = int(height * 0.65), int(height * 0.98)
    totals = [0, 0, 0]
    count = 0
    for y in range(y0, y1):
        row = rows[y]
        for x in range(x0, x1):
            totals[0] += row[x * 3]
            totals[1] += row[x * 3 + 1]
            totals[2] += row[x * 3 + 2]
            count += 1
    if count == 0:
        return None
    return [t / count for t in totals]


def lab_chroma(mean_rgb):
    linear = []
    for value in mean_rgb:
        v = value / 255.0
        linear.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
    x = (0.4124 * linear[0] + 0.3576 * linear[1] + 0.1805 * linear[2]) / 0.95047
    y = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    z = (0.0193 * linear[0] + 0.1192 * linear[1] + 0.9505 * linear[2]) / 1.08883

    def f(t):
        return t ** (1.0 / 3.0) if t > 0.008856 else 7.787 * t + 16.0 / 116.0

    a = 500.0 * (f(x) - f(y))
    b = 200.0 * (f(y) - f(z))
    return (a * a + b * b) ** 0.5


def deck_cast_chroma(path):
    """CIELAB chroma of the mean deck colour of one frame; inf when the region is empty."""
    mean = deck_region_mean_rgb(path)
    return float("inf") if mean is None else lab_chroma(mean)
