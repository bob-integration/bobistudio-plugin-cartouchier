# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Non-destructive clip editing: the EDIT LIST of a key or a playlist item, rendered at play time.

Operator decision (2026-10-17, docs/chantiers/CARTOUCHIER.md, "Éditeur de clip et montage"): In,
Out, fades, cuts, assembly, volume envelope and gain are SETTINGS of the key; the file is never
touched, the same sound serves several keys with different trims. "Save as new media" renders the
same list into a file (`write_wav`), the original still untouched.

THE EDIT LIST (`item["edit"]`, None = the media plays whole, as before 0.1.3)

  segments      [{media, in_ms, out_ms, cuts, xfade_ms, xfade_shape}]  played end to end.
                media     {asset, file} or None = the item's own media (a single-clip edit follows
                          the key when its sound is replaced… and is cleared then: see assign_media)
                in_ms     start in the SOURCE file;  out_ms  end in the source, 0 = end of file
                cuts      [[a_ms, b_ms]] passages REMOVED, in source time, joined by a micro
                          crossfade of CUT_XFADE_MS (linear: unity gain on continuous material)
                xfade_ms  crossfade with the PREVIOUS segment (ignored on the first one)
  fade_in_ms / fade_in_shape, fade_out_ms / fade_out_shape   on the EDITED timeline
  envelope      [[t_ms, db]] on the edited timeline; linear in dB between points, flat before the
                first and after the last; empty = 0 dB

  shapes        lin (x), eq (sin x·π/2: equal power), s (raised cosine), exp (x²)
  The clip GAIN is the item's existing `gain_db`; normalisation (R128) applies PER SEGMENT, each
  media at the cart's target — an assembly of a loud and a quiet sound comes out level.

Gain laws, k = sample index (exact, independent of the block size — what the test checks):
  fade in over N samples       shape(k / N)                    k < N   (first sample silent)
  global fade out over N       shape(1 − (k − (T − N) + 1) / N) k ≥ T − N (last sample silent)
  crossfade over X samples     outgoing shape(1 − j / X), incoming shape(j / X), j = 0..X−1
                               (linear sums to exactly 1; eq keeps the power of uncorrelated sounds)

RENDER. `compile_edit` turns the list into PIECES (source range, gain, crossfades, position on
the edited timeline) once, at load: cheap, no audio read. `Montage.read(pos, n)` then mixes the
pieces that overlap [pos, pos+n) into a buffer allocated ONCE — the audio thread reads it exactly
like a decoded media (`frames`, `read`), so loop, pause, fades, tails, countdown and /flows work
unchanged and count the EDITED duration. Per block: a few numpy ufuncs with `out=` on views of
preallocated buffers, a memmap slice per overlapping piece (one, two at a crossfade). The buffer
is shared by successive reads: a caller consumes the returned view before the next read (the
engine does — every read is multiplied into the output at once).

Pure: no I/O but `write_wav` (called from a worker thread, never the audio one).
"""
import bisect
import math
import struct

import numpy as np

SHAPES = ("lin", "eq", "s", "exp")
CUT_XFADE_MS = 10
MAX_SEGMENTS = 32
MAX_CUTS = 64
MAX_ENV = 256
MAX_EDIT_MS = 24 * 3600 * 1000
MAX_XFADE_MS = 30000
MAX_FADE_MS = 60000
ENV_DB_MIN, ENV_DB_MAX = -60.0, 12.0
LN10_20 = math.log(10.0) / 20.0


class MontageError(Exception):
    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


def _num(v, lo, hi, default, cast=float):
    try:
        x = cast(v)
    except (TypeError, ValueError):
        return default
    if isinstance(x, float) and not math.isfinite(x):
        return default
    return max(lo, min(hi, x))


def _shape(v, default="lin"):
    return v if v in SHAPES else default


def _media(m):
    if not isinstance(m, dict):
        return None
    asset = str(m.get("asset") or "").strip()[:64] or None
    f = str(m.get("file") or "").strip()[:512]
    if ".." in f.replace("\\", "/").split("/"):
        f = ""
    return {"asset": asset, "file": f} if (asset or f) else None


def _ranges(pairs, lo, hi, limit):
    """[[a, b]] sorted, clipped to [lo, hi], a < b, overlapping ones merged."""
    out = []
    for p in (pairs if isinstance(pairs, list) else [])[:limit]:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            continue
        a, b = _num(p[0], lo, hi, None, int), _num(p[1], lo, hi, None, int)
        if a is None or b is None or b <= a:
            continue
        out.append([a, b])
    out.sort()
    merged = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def is_trivial(e):
    if not e:
        return True
    s = e["segments"]
    return (len(s) == 1 and s[0]["media"] is None and not s[0]["in_ms"] and not s[0]["out_ms"]
            and not s[0]["cuts"] and not e["fade_in_ms"] and not e["fade_out_ms"] and not e["envelope"])


def normalize_edit(e):
    """Canonical edit list, or None when it changes nothing (the plain path plays the media)."""
    if not isinstance(e, dict):
        return None
    segs = []
    for s in (e.get("segments") if isinstance(e.get("segments"), list) else [])[:MAX_SEGMENTS]:
        if not isinstance(s, dict):
            continue
        i = _num(s.get("in_ms", 0), 0, MAX_EDIT_MS, 0, int)
        o = _num(s.get("out_ms", 0), 0, MAX_EDIT_MS, 0, int)
        if o and o <= i:
            o = 0                         # an Out before the In means nothing: back to the end
        segs.append({"media": _media(s.get("media")), "in_ms": i, "out_ms": o,
                     "cuts": _ranges(s.get("cuts"), 0, MAX_EDIT_MS, MAX_CUTS),
                     "xfade_ms": _num(s.get("xfade_ms", 0), 0, MAX_XFADE_MS, 0, int),
                     "xfade_shape": _shape(s.get("xfade_shape"), "eq")})
    if not segs:
        segs = [{"media": None, "in_ms": 0, "out_ms": 0, "cuts": [], "xfade_ms": 0, "xfade_shape": "eq"}]
    env = {}
    for p in (e.get("envelope") if isinstance(e.get("envelope"), list) else [])[:MAX_ENV]:
        if isinstance(p, (list, tuple)) and len(p) == 2:
            t = _num(p[0], 0, MAX_EDIT_MS, None, int)
            db = _num(p[1], ENV_DB_MIN, ENV_DB_MAX, None)
            if t is not None and db is not None:
                env[t] = round(db, 2)
    out = {"segments": segs,
           "fade_in_ms": _num(e.get("fade_in_ms", 0), 0, MAX_FADE_MS, 0, int),
           "fade_in_shape": _shape(e.get("fade_in_shape"), "lin"),
           "fade_out_ms": _num(e.get("fade_out_ms", 0), 0, MAX_FADE_MS, 0, int),
           "fade_out_shape": _shape(e.get("fade_out_shape"), "lin"),
           "envelope": [[t, env[t]] for t in sorted(env)]}
    return None if is_trivial(out) else out


def edit_medias(item):
    """Every media an item needs: its own, and each segment's."""
    out = [item.get("media") or {}]
    for s in ((item.get("edit") or {}).get("segments") or []):
        if s.get("media"):
            out.append(s["media"])
    return [m for m in out if m.get("asset") or m.get("file")]


def _f(ms, sr):
    return int(round(ms * sr / 1000.0))


def compile_edit(edit, item_media, source_for, norm_db_for, sr):
    """Edit list -> Montage. Raises MontageError (media_not_ready, empty_edit)."""
    pieces = []
    for si, s in enumerate(edit["segments"]):
        m = s["media"] or item_media
        src = source_for(m) if m else None
        if src is None or not getattr(src, "frames", 0):
            raise MontageError("media_not_ready", segment=si)
        a = min(src.frames, _f(s["in_ms"], sr))
        b = min(src.frames, _f(s["out_ms"], sr)) if s["out_ms"] else src.frames
        ranges, cur = [], a
        for ca, cb in s["cuts"]:
            ca, cb = max(a, _f(ca, sr)), min(b, _f(cb, sr))
            if cb <= ca:
                continue
            if ca > cur:
                ranges.append((cur, ca))
            cur = max(cur, cb)
        if b > cur:
            ranges.append((cur, b))
        gain = 10.0 ** (float(norm_db_for(m) or 0.0) / 20.0)
        for ri, (ra, rb) in enumerate(ranges):
            first = ri == 0
            pieces.append({"src": src, "s0": ra, "n": rb - ra, "gain": gain, "seg": si, "media": m,
                           "xf": _f(s["xfade_ms"] if first else CUT_XFADE_MS, sr),
                           "shape": s["xfade_shape"] if first else "lin",
                           "xo": 0, "shape_out": "lin", "o": 0})
    if not pieces:
        raise MontageError("empty_edit")
    pieces[0]["xf"] = 0
    for i in range(1, len(pieces)):
        p, q = pieces[i], pieces[i - 1]
        # half of each neighbour at most: a piece never overlaps more than its two neighbours
        xf = max(0, min(p["xf"], q["n"] // 2, p["n"] // 2))
        p["xf"] = xf
        p["o"] = q["o"] + q["n"] - xf
        q["xo"], q["shape_out"] = xf, p["shape"]
    total = pieces[-1]["o"] + pieces[-1]["n"]
    env = [(_f(t, sr), db) for t, db in edit["envelope"]]
    return Montage(pieces, total, _f(edit["fade_in_ms"], sr), edit["fade_in_shape"],
                   _f(edit["fade_out_ms"], sr), edit["fade_out_shape"], env)


def _shape_inplace(name, x):
    if name == "exp":
        np.multiply(x, x, out=x)
    elif name == "eq":
        np.multiply(x, math.pi / 2.0, out=x)
        np.sin(x, out=x)
    elif name == "s":
        np.multiply(x, math.pi, out=x)
        np.cos(x, out=x)
        np.subtract(1.0, x, out=x)
        np.multiply(x, 0.5, out=x)
    return x


def shape_value(name, x):
    """Scalar version of the shapes (tests, UI preview)."""
    if name == "exp":
        return x * x
    if name == "eq":
        return math.sin(x * math.pi / 2.0)
    if name == "s":
        return 0.5 * (1.0 - math.cos(x * math.pi))
    return x


class Montage:
    """An edit list compiled for reading: same two members as a decoded media (`frames`, `read`)."""

    def __init__(self, pieces, total, fade_in, fade_in_shape, fade_out, fade_out_shape, env, block=4800):
        self.pieces = pieces
        self.frames = int(total)
        self.fi, self.fi_shape = min(fade_in, self.frames), fade_in_shape
        self.fo, self.fo_shape = min(fade_out, self.frames), fade_out_shape
        self.env_t = [t for t, _ in env]
        self.env_db = [db for _, db in env]
        self._starts = [p["o"] for p in pieces]
        self._cap = 0
        self._alloc(block)

    def _alloc(self, n):
        self._cap = int(n)
        self.acc = np.zeros((n, 2), np.float32)
        self.tmp = np.zeros((n, 2), np.float32)
        self.g = np.zeros(n, np.float64)
        self.x = np.zeros(n, np.float64)
        self.ramp = np.arange(n, dtype=np.float64)

    def renorm(self, norm_db_for):
        """Settings changed (target, normalisation on/off): each segment re-gained in place."""
        for p in self.pieces:
            p["gain"] = 10.0 ** (float(norm_db_for(p["media"]) or 0.0) / 20.0)

    def layout(self, sr):
        """Pieces on the edited timeline, in ms (editor: segment boundaries, source ↔ edited)."""
        return [{"seg": p["seg"], "src_ms": p["s0"] * 1000.0 / sr, "len_ms": p["n"] * 1000.0 / sr,
                 "at_ms": p["o"] * 1000.0 / sr, "xf_ms": p["xf"] * 1000.0 / sr} for p in self.pieces]

    def _fade(self, start, cnt, origin, length, out, shape, offset=0.0):
        """x = (k − origin + offset) / length over cnt samples starting at k = start, shaped, into out."""
        x = self.x[:cnt]
        np.add(self.ramp[:cnt], float(start - origin) + offset, out=x)
        np.divide(x, float(length), out=x)
        return _shape_inplace(shape, x)

    def read(self, pos, n):
        pos, n = int(pos), int(n)
        end = min(self.frames, pos + n)
        n = max(0, end - pos)
        if n > self._cap:
            self._alloc(n)                     # a larger reader (waveform, file render): once
        acc = self.acc[:n]
        acc.fill(0.0)
        if n == 0:
            return acc
        # the piece starting at or before pos, and the one before it: a crossfade overlaps them
        i = max(0, bisect.bisect_right(self._starts, pos) - 2)
        while i < len(self.pieces):
            p = self.pieces[i]
            i += 1
            if p["o"] >= end:
                break
            a, b = max(pos, p["o"]), min(end, p["o"] + p["n"])
            if b <= a:
                continue
            m, k0 = b - a, a - p["o"]
            g = self.g[:m]
            g.fill(p["gain"])
            if p["xf"] and k0 < p["xf"]:
                j1 = min(m, p["xf"] - k0)
                g[:j1] *= self._fade(k0, j1, 0, p["xf"], None, p["shape"])
            xo = p["xo"]
            if xo and k0 + m > p["n"] - xo:
                j0 = max(0, p["n"] - xo - k0)
                x = self._fade(k0 + j0, m - j0, p["n"] - xo, xo, None, "lin")
                np.subtract(1.0, x, out=x)
                g[j0:] *= _shape_inplace(p["shape_out"], x)
            t = self.tmp[:m]
            np.multiply(p["src"].read(p["s0"] + k0, m), g[:, None], out=t)
            acc[a - pos:b - pos] += t
        g = self.g[:n]
        g.fill(1.0)
        touched = False
        if self.fi and pos < self.fi:
            j1 = min(n, self.fi - pos)
            g[:j1] *= self._fade(pos, j1, 0, self.fi, None, self.fi_shape)
            touched = True
        if self.fo and end > self.frames - self.fo:
            j0 = max(0, self.frames - self.fo - pos)
            x = self._fade(pos + j0, n - j0, self.frames - self.fo, self.fo, None, "lin", 1.0)
            np.subtract(1.0, x, out=x)
            g[j0:] *= _shape_inplace(self.fo_shape, x)
            touched = True
        if self.env_t:
            self._envelope(pos, n, g)
            touched = True
        if touched:
            acc *= g[:, None]
        return acc

    def _envelope(self, pos, n, g):
        """g *= 10^(db(k)/20) over [pos, pos+n), db piecewise linear between the points."""
        t, d = self.env_t, self.env_db
        k, end = pos, pos + n
        while k < end:
            j = bisect.bisect_right(t, k)        # t[j-1] <= k < t[j]
            if j == 0:
                stop, flat = min(end, t[0]), d[0]
            elif j >= len(t):
                stop, flat = end, d[-1]
            else:
                stop, flat = min(end, t[j]), None
            c = stop - k
            x = self.x[:c]
            if flat is not None:
                x.fill(flat * LN10_20)
            else:
                slope = (d[j] - d[j - 1]) / float(t[j] - t[j - 1])
                np.add(self.ramp[:c], float(k - t[j - 1]), out=x)
                np.multiply(x, slope, out=x)
                np.add(x, d[j - 1], out=x)
                np.multiply(x, LN10_20, out=x)
            np.exp(x, out=x)
            g[k - pos:stop - pos] *= x
            k = stop


# ─── Waveform windows ────────────────────────────────────────────────────────────────────────
PEAK_STEP = 256                           # samples per value of the coarse level (5.3 ms)


def peaks_level(read, frames, step=PEAK_STEP, chunk=PEAK_STEP * 1024):
    """Coarse level of the multi-resolution waveform: max |L|,|R| per `step` samples (float32).
    Computed once per media (chunked, memory bounded) and cached by the caller."""
    nb = (frames + step - 1) // step
    out = np.zeros(nb, np.float32)
    pos = 0
    while pos < frames:
        n = min(chunk, frames - pos)
        x = np.abs(np.asarray(read(pos, n), np.float32)).max(axis=1)
        full = n // step
        if full:
            out[pos // step:pos // step + full] = x[:full * step].reshape(full, step).max(axis=1)
        if n % step:
            out[pos // step + full] = x[full * step:].max()
        pos += n
    return out


def window(read, frames, a, b, points, level=None, step=PEAK_STEP):
    """Peaks of [a, b) (samples) in `points` buckets, ints 0..100. Buckets wider than `step` use
    the coarse level (cheap whatever the zoom); narrower ones read the samples themselves (exact,
    at most points × step samples)."""
    a, b = max(0, int(a)), min(frames, int(b))
    points = max(1, min(4000, int(points)))
    if b <= a:
        return [0] * points
    edges = np.linspace(a, b, points + 1)
    out = np.zeros(points, np.float32)
    if level is not None and (b - a) / points >= step:
        idx = np.floor(edges / step).astype(np.int64)
        for i in range(points):
            lo, hi = idx[i], max(idx[i] + 1, idx[i + 1])
            out[i] = level[lo:min(hi, level.shape[0])].max() if lo < level.shape[0] else 0.0
    else:
        x = np.abs(np.asarray(read(a, b - a), np.float32)).max(axis=1)
        e = (edges - a).astype(np.int64)
        for i in range(points):
            lo, hi = e[i], max(e[i] + 1, e[i + 1])
            out[i] = x[lo:min(hi, x.shape[0])].max() if lo < x.shape[0] else 0.0
    return [int(round(min(1.0, float(v)) * 100)) for v in out]


def montage_window(m, a, b, points, chunk=48000):
    """Peaks of an edited timeline: rendered (the true result, fades and envelope included),
    chunked. Linear in the window's length — a cart sound is short; the editor asks for the
    visible window only."""
    a, b = max(0, int(a)), min(m.frames, int(b))
    points = max(1, min(4000, int(points)))
    if b <= a:
        return [0] * points
    edges = np.linspace(a, b, points + 1).astype(np.int64)
    out = np.zeros(points, np.float32)
    pos, bi = a, 0
    while pos < b and bi < points:
        n = min(chunk, b - pos)
        x = np.abs(m.read(pos, n)).max(axis=1)
        while bi < points and edges[bi] < pos + n:
            lo, hi = max(edges[bi], pos), min(max(edges[bi + 1], edges[bi] + 1), pos + n)
            if hi > lo:
                out[bi] = max(out[bi], float(x[lo - pos:hi - pos].max()))
            if edges[bi + 1] <= pos + n:
                bi += 1
            else:
                break
        pos += n
    return [int(round(min(1.0, float(v)) * 100)) for v in out]


# ─── Render to a file ────────────────────────────────────────────────────────────────────────
def write_wav(m, path, gain=1.0, sr=48000, chunk=48000, progress=None):
    """The edited timeline as a 24-bit PCM stereo WAV (pure numpy: no ffmpeg, no temp file).
    Returns {frames, clipped}: samples beyond full scale are clipped AND counted."""
    total = m.frames
    data_bytes = total * 2 * 3
    clipped = 0
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, sr, sr * 2 * 3, 2 * 3, 24))
        f.write(b"data" + struct.pack("<I", data_bytes))
        pos = 0
        while pos < total:
            n = min(chunk, total - pos)
            x = np.asarray(m.read(pos, n), np.float64) * gain
            over = np.abs(x) > 1.0
            clipped += int(over.sum())
            v = np.rint(np.clip(x, -1.0, 1.0) * 8388607.0).astype(np.int32).reshape(-1)
            b = np.empty((v.shape[0], 3), np.uint8)
            b[:, 0] = v & 0xFF
            b[:, 1] = (v >> 8) & 0xFF
            b[:, 2] = (v >> 16) & 0xFF
            f.write(b.tobytes())
            pos += n
            if progress:
                progress(pos, total)
    return {"frames": total, "clipped": clipped}
