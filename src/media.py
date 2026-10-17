# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Media of the cart: identity → file on this node → decoded PCM cache → measured.

A key designates its media by IDENTITY (`asset`, the media base id written in the file's
sidecar `<file>.xml`, `<media id="…">`) with the path as a hint — the player's rule (0.39.0). The
container resolves the identity ITSELF on its volume: it has no access to the orchestrator, and a
cart addressed by identity keeps working after a file move or a project import elsewhere. A
missing copy is fetched by the console (the shared media library does it before assigning).

Decoding: ffmpeg → float32 stereo 48 kHz, written ONCE to the state volume as a raw `.f32`
(`/var/lib/bobi/cart_cache`, `state_volume` in the manifest) and played through np.memmap:
starting a key never decodes, and a cart of a hundred jingles does not sit in RAM — the page
cache holds what is played. The R128 measurement and the waveform are computed in the same pass
and kept in a `.json` next to it: measured once per media, not once per start or per restart.

Nothing here runs in the audio thread: `Library.worker` decodes and measures in the background,
the engine only ever sees `source(media)` → a ready memmap source or None.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time

import numpy as np

try:
    from . import loudness
except ImportError:                       # imported flat (tests, or sys.path = the package dir)
    import loudness

SR = 48000
CH = 2
MEDIA_EXTS = (".wav", ".mp3", ".aac", ".flac", ".m4a", ".ogg", ".opus", ".aif", ".aiff",
              ".mp4", ".mov", ".mxf", ".mkv")
_SIDE_ID_RE = re.compile(r'<media\b[^>]*?\bid="([^"]+)"')
WAVE_POINTS = 400


def _now():
    return time.time()


class MemmapSource:
    """Decoded media served from the cache file; same two members as engine.ArraySource."""

    def __init__(self, path):
        size = os.path.getsize(path)
        self.frames = size // (4 * CH)
        self.pcm = (np.memmap(path, dtype="<f4", mode="r", shape=(self.frames, CH))
                    if self.frames else np.zeros((0, CH), np.float32))

    def read(self, pos, n):
        return self.pcm[pos:pos + n]


class Library:
    def __init__(self, media_root="/mnt/media", cache_dir=None, max_cache_mb=4096,
                 max_media_s=1200, log=print, on_ready=None):
        self.root = media_root
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        self.cache = cache_dir or self._pick_cache_dir()
        self.max_bytes = int(max_cache_mb) << 20
        self.max_media_s = int(max_media_s)
        self.log = log
        self.on_ready = on_ready
        self.lock = threading.Lock()
        self.status = {}        # cache key -> {"state", "error", "file", ...}
        self.meta = {}          # cache key -> analysis json (r128, waveform, duration)
        self.sources = {}       # cache key -> MemmapSource
        self.queue = []
        self.wake = threading.Event()
        self.asset_map = {}
        self.asset_scan_t = 0.0
        self.stats = {"decoded": 0, "errors": 0, "decode_ms_last": 0, "evicted": 0}

    def _pick_cache_dir(self):
        for d in ("/var/lib/bobi/cart_cache", "/tmp/cart_cache"):
            try:
                os.makedirs(d, exist_ok=True)
                t = os.path.join(d, ".w")
                with open(t, "w") as f:
                    f.write("1")
                os.remove(t)
                return d
            except OSError:
                continue
        return "/tmp"

    # ── identity → path ──
    def _scan_assets(self):
        out = {}
        for root, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            names = set(files)
            for fn in files:
                if not fn.endswith(".xml"):
                    continue
                media = fn[:-4]
                if media not in names or not media.lower().endswith(MEDIA_EXTS):
                    continue
                try:
                    with open(os.path.join(root, fn), "r", encoding="utf-8", errors="replace") as f:
                        m = _SIDE_ID_RE.search(f.read(1 << 16))
                except OSError:
                    continue
                if m and m.group(1) not in out:
                    out[m.group(1)] = os.path.join(root, media)
        return out

    def resolve(self, media, rescan=False):
        """Absolute path of the media on this node, or "" (absent)."""
        aid = (media or {}).get("asset")
        f = (media or {}).get("file") or ""
        if f:
            p = f if f.startswith("/") else os.path.join(self.root, f)
            if (p.startswith(self.root + "/") or p.startswith("/mnt/ext/")) and os.path.isfile(p):
                if not aid:
                    return p
                if self._sidecar_id(p) in (aid, None):
                    return p
        if not aid:
            return ""
        with self.lock:
            p = self.asset_map.get(aid)
        if p and os.path.isfile(p):
            return p
        if rescan or _now() - self.asset_scan_t > 10:
            m = self._scan_assets()
            with self.lock:
                self.asset_map, self.asset_scan_t = m, _now()
            p = m.get(aid)
            if p and os.path.isfile(p):
                return p
        return ""

    @staticmethod
    def _sidecar_id(p):
        try:
            with open(p + ".xml", "r", encoding="utf-8", errors="replace") as f:
                m = _SIDE_ID_RE.search(f.read(1 << 16))
            return m.group(1) if m else None
        except OSError:
            return None

    @staticmethod
    def cache_key(media):
        """Stable key of a media in the cache: its identity when it has one (the same content on
        another path is the same sound), a hash of its path otherwise."""
        aid = (media or {}).get("asset")
        if aid:
            return "a_" + re.sub(r"[^A-Za-z0-9_-]", "_", aid)[:64]
        f = (media or {}).get("file") or ""
        return "f_" + hashlib.sha1(f.encode("utf-8")).hexdigest()[:20] if f else ""

    # ── public API ──
    def source(self, media):
        k = self.cache_key(media)
        with self.lock:
            return self.sources.get(k)

    def info(self, media):
        """Status + analysis of a media, for /state and the key editor."""
        k = self.cache_key(media)
        with self.lock:
            st = dict(self.status.get(k) or {"state": "unknown"})
            m = self.meta.get(k) or {}
        st.update({x: m.get(x) for x in ("duration_ms", "lufs", "peak_dbfs", "short")})
        return st

    def waveform(self, media):
        k = self.cache_key(media)
        with self.lock:
            return list((self.meta.get(k) or {}).get("waveform") or [])

    def want(self, medias):
        """Make sure every media in `medias` is (or becomes) ready. Cheap: queues what is unknown."""
        added = False
        with self.lock:
            for m in medias:
                k = self.cache_key(m)
                if not k or k in self.sources:
                    continue
                st = self.status.get(k, {}).get("state")
                if st in ("queued", "decoding"):
                    continue
                if st in ("missing", "error") and _now() - self.status[k].get("t", 0) < 15:
                    continue
                self.status[k] = {"state": "queued", "t": _now()}
                self.queue.append(dict(m))
                added = True
        if added:
            self.wake.set()

    def worker(self, keep=lambda: set()):
        """Loader thread: decode + measure, one media at a time, lowest priority work there is."""
        try:
            os.nice(5)
        except OSError:
            pass
        while True:
            self.wake.wait(5.0)
            self.wake.clear()
            while True:
                with self.lock:
                    m = self.queue.pop(0) if self.queue else None
                if m is None:
                    break
                try:
                    self._prepare(m)
                except Exception as e:                       # never let the loader die silently
                    k = self.cache_key(m)
                    with self.lock:
                        self.status[k] = {"state": "error", "error": "%s: %s" % (type(e).__name__, e),
                                          "t": _now()}
                        self.stats["errors"] += 1
                    self.log("media %s: %s" % (m, e))
            try:
                self._evict(keep())
            except Exception as e:
                self.log("cache eviction: %s" % e)

    def _prepare(self, m):
        k = self.cache_key(m)
        path = self.resolve(m, rescan=True)
        if not path:
            with self.lock:
                self.status[k] = {"state": "missing", "t": _now()}
            return
        st = os.stat(path)
        f32 = os.path.join(self.cache, k + ".f32")
        js = os.path.join(self.cache, k + ".json")
        meta = None
        try:
            with open(js) as f:
                meta = json.load(f)
            if (meta.get("size") != st.st_size or int(meta.get("mtime", 0)) != int(st.st_mtime)
                    or not os.path.isfile(f32)):
                meta = None
        except (OSError, ValueError):
            meta = None
        if meta is None:
            with self.lock:
                self.status[k] = {"state": "decoding", "t": _now(), "file": path}
            t0 = _now()
            self._decode(path, f32)
            src = MemmapSource(f32)
            meas = loudness.measure(src.read, src.frames, CH)
            meta = dict(meas, size=st.st_size, mtime=int(st.st_mtime), file=path,
                        duration_ms=int(src.frames * 1000 // SR),
                        waveform=loudness.waveform(src.read, src.frames, WAVE_POINTS, CH),
                        measured_at=int(_now()))
            tmp = js + ".part"
            with open(tmp, "w") as f:
                json.dump(meta, f)
            os.replace(tmp, js)
            self.stats["decoded"] += 1
            self.stats["decode_ms_last"] = int((_now() - t0) * 1000)
        else:
            src = MemmapSource(f32)
        with self.lock:
            self.sources[k] = src
            self.meta[k] = meta
            self.status[k] = {"state": "ready", "t": _now(), "file": path}
        try:
            os.utime(f32)                    # LRU stamp
        except OSError:
            pass
        if self.on_ready:
            self.on_ready(m)

    @staticmethod
    def _channels(path):
        try:
            r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                                "stream=channels", "-of", "csv=p=0", path],
                               capture_output=True, text=True, timeout=30)
            return int((r.stdout or "").strip().splitlines()[0])
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return 0

    def _decode(self, path, f32):
        tmp = f32 + ".part"
        # A MONO file is played as dual mono, full level on both sides — what a cart operator
        # expects. ffmpeg's default upmix (`-ac 2`) puts it at −3 dB per side (centre mix level),
        # which normalisation would hide but a cart with normalisation off would not.
        mix = (["-af", "pan=stereo|c0=c0|c1=c0"] if self._channels(path) == 1
               else ["-ac", str(CH)])
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path, "-vn", "-sn", "-dn",
               "-t", str(self.max_media_s)] + mix + ["-ar", str(SR), "-f", "f32le", tmp]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=max(60, self.max_media_s))
        if r.returncode != 0 or not os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise RuntimeError("ffmpeg: " + (r.stderr or "").strip()[-300:])
        os.replace(tmp, f32)                 # a memmap open on the old file stays valid

    def _evict(self, keep_keys):
        """Bound the cache: least recently used first, never a media the cart references."""
        try:
            entries = [e for e in os.scandir(self.cache) if e.name.endswith(".f32")]
        except OSError:
            return
        total = sum(e.stat().st_size for e in entries)
        if total <= self.max_bytes:
            return
        for e in sorted(entries, key=lambda x: x.stat().st_mtime):
            k = e.name[:-4]
            if k in keep_keys:
                continue
            sz = e.stat().st_size
            for ext in (".f32", ".json"):
                try:
                    os.remove(os.path.join(self.cache, k + ext))
                except OSError:
                    pass
            with self.lock:
                self.sources.pop(k, None)
                self.meta.pop(k, None)
                self.status.pop(k, None)
            self.stats["evicted"] += 1
            total -= sz
            if total <= self.max_bytes:
                break
