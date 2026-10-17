# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""EBU R128 / ITU-R BS.1770-4 integrated loudness, sample peak and waveform — numpy only.

WHY NUMPY AND NOT `ffmpeg -af ebur128`. The sound has to be decoded to PCM anyway (it is played
from a decoded cache), so the measurement runs on samples already in hand: no second decode of
the file, no stderr to parse, and the same code is exercised offline by the tests on a synthetic
signal of KNOWN loudness. The console's real-time R128 lives in its C kernel; this is the
load-time measurement, run once per media and cached.

PRECISION. K-weighting is the exact BS.1770 48 kHz biquad pair, applied as its impulse response
(FFT of the analytic frequency response, 4096 taps: the slowest pole, r ≈ 0.995, has decayed
to ~1e-9 by then — truncation is far below the 0.1 LU display step) by overlap-save FFT
convolution. Gating is the normative one: 400 ms blocks, 75 % overlap, absolute gate −70 LUFS,
relative gate −10 LU. Checked against `ffmpeg ebur128` when this module was written
(2026-10-17, 30 s of coloured stereo noise: −22.52 here, −22.5 for ffmpeg, which prints one
decimal); the offline test checks the reference sine (stereo 997 Hz at −23 dBFS peak →
−23.0 LUFS) and a left-only 0 dBFS sine (−3.01 LUFS, the BS.1770 calibration point).

COST. Linear in the duration, measured 2026-10-17: 180 s of stereo in 1.0 s on one core (≈ 180×
real time), chunked by 2.5 s so memory stays bounded whatever the length (a memmap of a
20-minute file is streamed, never loaded). It runs in the loader thread, never the audio one.

Sample peak, not true peak: the normalisation only uses it as a ceiling guard on BOOSTS
(`peak_ceiling_db`), where the ~0.5 dB inter-sample excess of true peak does not change the
decision enough to justify 4× oversampling.
"""
import math

import numpy as np

SR = 48000
HOP = SR // 10                   # 100 ms: blocks of 400 ms overlap by 75 %
BLOCK_HOPS = 4
ABS_GATE = -70.0
REL_GATE = -10.0
FIR_LEN = 4096
CHUNK = HOP * 25                 # 2.5 s per FFT pass (multiple of HOP)

# BS.1770-4, 48 kHz: pre-filter (high shelf) then RLB high-pass.
_B1 = (1.53512485958697, -2.69169618940638, 1.19839281085285)
_A1 = (1.0, -1.69065929318241, 0.73248077421585)
_B2 = (1.0, -2.0, 1.0)
_A2 = (1.0, -1.99004745483398, 0.99007225036621)

_fir_cache = {}


def _k_fir():
    """Impulse response of the K-weighting filter (float64, FIR_LEN taps)."""
    h = _fir_cache.get("k")
    if h is None:
        nfft = FIR_LEN * 4
        z = np.exp(-1j * np.pi * np.arange(nfft // 2 + 1) / (nfft // 2))

        def resp(b, a):
            return ((b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z))

        h = np.fft.irfft(resp(_B1, _A1) * resp(_B2, _A2), nfft)[:FIR_LEN]
        _fir_cache["k"] = h
    return h


def hop_energies(read, frames, channels=2):
    """Sum of squares of the K-weighted signal per 100 ms hop and per channel.

    `read(pos, n)` returns float32 (n, channels). Streamed by CHUNK with overlap-save, so a memmap
    of any length is never loaded whole. Returns (hops, channels) float64 plus the sample peak."""
    h = _k_fir()
    L = len(h)
    n_hops = frames // HOP
    out = np.zeros((n_hops, channels), dtype=np.float64)
    peak = 0.0
    nfft = 1 << int(math.ceil(math.log2(CHUNK + L)))
    H = np.fft.rfft(h, nfft)
    hist = np.zeros((L - 1, channels), dtype=np.float64)
    pos = 0
    while pos < frames:
        n = min(CHUNK, frames - pos)
        x = np.asarray(read(pos, n), dtype=np.float64).reshape(n, channels)
        if n:
            peak = max(peak, float(np.max(np.abs(x))))
        seg = np.concatenate((hist, x), axis=0)
        y = np.fft.irfft(np.fft.rfft(seg, nfft, axis=0) * H[:, None], nfft, axis=0)
        y = y[L - 1:L - 1 + n]
        hist = seg[-(L - 1):]
        first = pos // HOP
        full = (pos + n) // HOP - first
        if full > 0:
            sq = y[:full * HOP] ** 2
            out[first:first + full] = sq.reshape(full, HOP, channels).sum(axis=1)
        pos += n
    return out, peak


def integrated_from_hops(hops, channels_gain=None):
    """BS.1770-4 gated integrated loudness from per-hop energies. None when nothing passes the
    absolute gate (silence) — never −inf, which JSON cannot carry."""
    n_hops, ch = hops.shape
    if n_hops < BLOCK_HOPS:
        return None
    g = np.ones(ch) if channels_gain is None else np.asarray(channels_gain, dtype=np.float64)
    c = np.cumsum(np.vstack([np.zeros((1, ch)), hops]), axis=0)
    blk = (c[BLOCK_HOPS:] - c[:-BLOCK_HOPS]) / float(BLOCK_HOPS * HOP)   # mean square per block
    z = blk @ g
    with np.errstate(divide="ignore"):
        lj = -0.691 + 10.0 * np.log10(z)
    keep = lj > ABS_GATE
    if not keep.any():
        return None
    rel = -0.691 + 10.0 * math.log10(float(z[keep].mean())) + REL_GATE
    keep &= lj > rel
    if not keep.any():
        return None
    return -0.691 + 10.0 * math.log10(float(z[keep].mean()))


def measure(read, frames, channels=2):
    """{"lufs": float|None, "peak_dbfs": float|None, "short": bool}. `short`: shorter than one
    400 ms block — the integrated value is then the plain mean square of the whole sound (BS.1770
    leaves it undefined; a 200 ms stinger must still get a sensible gain)."""
    hops, peak = hop_energies(read, frames, channels)
    lufs = integrated_from_hops(hops)
    short = hops.shape[0] < BLOCK_HOPS
    if short and frames > 0:
        h = _k_fir()
        x = np.asarray(read(0, frames), dtype=np.float64).reshape(frames, channels)
        y = np.stack([np.convolve(x[:, i], h)[:frames] for i in range(channels)], axis=1)
        ms = float((y ** 2).mean(axis=0).sum())
        lufs = (-0.691 + 10.0 * math.log10(ms)) if ms > 0 else None
        if lufs is not None and lufs <= ABS_GATE:
            lufs = None
    return {"lufs": None if lufs is None else round(lufs, 2),
            "peak_dbfs": round(20.0 * math.log10(peak), 2) if peak > 0 else None,
            "short": bool(short)}


def normalization_gain(meas, target_lufs=-23.0, max_gain_db=12.0, peak_ceiling_db=-1.0,
                       enabled=True):
    """Gain (dB) bringing the measured sound to `target_lufs`, and why it was limited.

    A cut is never limited. A boost is capped by `max_gain_db` (raising a quiet recording by 30 dB
    raises its noise with it) and by the sample-peak ceiling (a boost must not clip). Silence and
    unmeasured media get 0 dB: nothing to normalise."""
    if not enabled or not meas or meas.get("lufs") is None:
        return 0.0, ("disabled" if not enabled else "unmeasured")
    g = float(target_lufs) - float(meas["lufs"])
    why = None
    if g > 0:
        if g > max_gain_db:
            g, why = float(max_gain_db), "max_gain"
        pk = meas.get("peak_dbfs")
        if pk is not None and pk + g > peak_ceiling_db:
            g, why = max(0.0, float(peak_ceiling_db) - pk), "peak"
    return round(g, 2), why


def waveform(read, frames, points=400, channels=2):
    """Peak envelope for display: `points` integers 0..100 (max |L|,|R| per bucket), streamed
    by chunk. Small on purpose: it travels in /waveform, polled by every open console."""
    if frames <= 0:
        return []
    points = max(1, min(int(points), frames))
    edges = np.linspace(0, frames, points + 1).astype(np.int64)
    out = np.zeros(points, dtype=np.float32)
    pos = 0
    b = 0
    step = HOP * 50
    while pos < frames and b < points:
        n = min(step, frames - pos)
        x = np.abs(np.asarray(read(pos, n), dtype=np.float32)).reshape(n, channels).max(axis=1)
        while b < points and edges[b] < pos + n:
            lo, hi = max(edges[b], pos), min(edges[b + 1], pos + n)
            if hi > lo:
                out[b] = max(out[b], float(x[lo - pos:hi - pos].max()))
            if edges[b + 1] <= pos + n:
                b += 1
            else:
                break
        pos += n
    return [int(round(min(1.0, v) * 100)) for v in out]
