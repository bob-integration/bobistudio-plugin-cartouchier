#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Cartouchier (cart machine) — THIN TEMPLATE.

The logic lives in the modules pushed next to this script by the manifest's `files`, under
/opt/script/cart/: `engine` (players, keys, pages, playlist A/B — pure, tested offline),
`loudness` (R128 at load time) and `media` (identity → file → decoded cache). This file only wires
them to the outside world: the MXL writers, the audio thread, the two HTTP ports.

WHY A PLUGIN OF ITS OWN, not a module of the audio console (operator decision 2026-10-17): media
handling — decoding, measuring, files — stays out of the console's real-time thread, and one cart
can feed several consoles, or none. The console integrates it through generic mechanisms only:
the per-flow publication `GET /flows` (see help.md), fader start, the plugin UI contract.

AUDIO THREAD. One thread for all players. It writes 1 ms blocks (48 samples) at an EXPLICIT,
CONTIGUOUS index anchored once on the TAI sample grid — never `write()` without an index, which
re-derives the index from the clock at every call and overwrites or skips samples (measured on
stream_in 0.1.0: ~220 discontinuities/s). One block per pass, the thread sleeping well under 1 ms:
a consumer reading 1 ms blocks injects silence whenever the head jumps by more. All players share
the index: the flows are sample-aligned with each other.

THE OUTPUT DOES NOT DEPEND ON WHAT IS LOADED: an idle player writes silence, so a console strip
fed by it never sees its source vanish between two jingles.

⚠ `str.format` template: three substitutions only, every literal brace is DOUBLED.
"""
import json
import os
import signal
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CONFIG = {config}
HOSTNAME = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

SR = 48000
BLOCK = SR // 1000                 # 48 samples = 1 ms, the bus's audio granularity
LATE_REANCHOR = 200                # more than 200 ms behind (suspension, CPU starvation): re-anchor


def _int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


N_PLAYERS = _int(CONFIG.get("players"), 1, 8, 2)
# Flow names: the SAME rule as `wiring.produces` in plugin.json ("{{hostname}}_player_{{i1}}").
FLOWS = ["%s_player_%d" % (HOSTNAME, i + 1) for i in range(N_PLAYERS)]
MEDIA_ROOT = CONFIG.get("media_root") or "/mnt/media"
LOG_LEVEL = CONFIG.get("log_level") or "info"


def log(msg, level="info"):
    order = ("debug", "info", "warning", "error")
    if order.index(level if level in order else "info") >= order.index(
            LOG_LEVEL if LOG_LEVEL in order else "info"):
        print("cartouchier: %s" % msg, flush=True)


from cart import engine as E          # noqa: E402
from cart import loudness             # noqa: E402
from cart import media as M           # noqa: E402
from cart import montage as MT        # noqa: E402
from cart import listen as LI         # noqa: E402

metrics = {{
    "fps": 0.0, "blocks_per_s": 0.0, "render_ms_max": 0.0, "late_blocks": 0, "reanchors": 0,
    "write_errors": 0, "writer_reopens": 0, "players": N_PLAYERS, "flows": FLOWS,
    "playing": 0, "plugin_version": PLUGIN_VERSION, "own_latency_ms": 0.0, "source": "cart",
    "media_ready": 0, "media_pending": 0, "media_missing": 0, "last_error": None,
}}
metrics_lock = threading.Lock()

library = M.Library(MEDIA_ROOT, cache_dir=CONFIG.get("cache_dir") or None,
                    max_cache_mb=_int(CONFIG.get("cache_max_mb"), 256, 65536, 4096),
                    max_media_s=_int(CONFIG.get("max_media_s"), 10, 7200, 1200),
                    log=lambda m: log(m, "warning"))


def norm_db(media):
    """Normalisation gain (dB) of a media under the cart's CURRENT settings — re-evaluated at
    each start, so changing the target applies to the next launch (and, via set_settings, to
    what is already playing)."""
    k = library.cache_key(media)
    with library.lock:
        meas = library.meta.get(k)
    s = engine.doc["settings"]
    g, _why = loudness.normalization_gain(meas, s["target_lufs"], s["max_gain_db"],
                                          s["peak_ceiling_db"], s["normalize"])
    return g


engine = E.CartEngine(N_PLAYERS, SR, CONFIG.get("cart"), source_for=library.source,
                      norm_db_for=norm_db)
library.on_ready = lambda m: engine.recue()
# Browser listening (0.1.3): a player's output or the off-air pre-listen. When the last browser
# listening to the pre-listen leaves (page closed, source changed), the pre-listen stops.
listen_hub = LI.ListenHub(N_PLAYERS, on_last_pfl=engine.preview_stop)


def referenced_medias():
    d = engine.doc
    out = [m for p in d["pages"] for k in p["keys"] for m in MT.edit_medias(k)]
    out += [m for it in d["playlist"]["items"] for m in MT.edit_medias(it)]
    return [m for m in out if m.get("asset") or m.get("file")]


def keep_keys():
    return {{library.cache_key(m) for m in referenced_medias()}}


def media_view(item):
    st = library.info(item["media"]) if (item["media"].get("asset") or item["media"].get("file")) \
        else {{"state": "empty"}}
    s = engine.doc["settings"]
    with library.lock:
        meas = library.meta.get(library.cache_key(item["media"]))
    g, why = loudness.normalization_gain(meas, s["target_lufs"], s["max_gain_db"],
                                         s["peak_ceiling_db"], s["normalize"])
    if item.get("edit"):               # the EDITED duration, not the file's
        st["duration_ms"] = edited_ms(item)
    return {{"state": st.get("state"), "error": st.get("error"), "duration_ms": st.get("duration_ms"),
            "edited": bool(item.get("edit")), "file_ms": library.info(item["media"]).get("duration_ms")
            if (item["media"].get("asset") or item["media"].get("file")) else None,
            "lufs": st.get("lufs"), "peak_dbfs": st.get("peak_dbfs"), "norm_db": g,
            "norm_limited": why if why in ("max_gain", "peak") else None}}


def state_view():
    # The engine lock is held only for the SNAPSHOT of the players and the playlist: the audio
    # thread renders under the same lock, and a 500-item playlist's media views must not delay a
    # 1 ms block. The doc itself is never mutated in place (every edit commits a new one), so it
    # is read without the lock.
    with engine.lock:
        d = engine.doc
        players = engine.players_view(FLOWS)
        on_air = engine.pl_on_air
        plv = engine.playlist_view()
        rev = engine.rev
        counters = dict(engine.counters)
        events = list(engine.events[-8:])
    ids = [p["id"] for p in d["pages"]]
    pi = ids.index(d["page"]) if d["page"] in ids else 0
    page = d["pages"][pi]
    cur = next((p for p in players if p["state"] in ("playing", "fading")), None)
    out = {{
        "type": "cartouchier", "version": PLUGIN_VERSION, "hostname": HOSTNAME,
        "n_players": N_PLAYERS, "flows": FLOWS, "mode": d["mode"], "doc_rev": rev,
        "page": d["page"], "page_index": pi + 1, "page_name": page["name"],
        "pages_count": len(ids), "grid": d["grid"], "settings": d["settings"],
        "defaults": d["defaults"], "page_defaults": page.get("defaults") or {{}},
        "players": players,
        "playing_count": sum(1 for p in players if p["state"] in ("playing", "fading")),
        "current_title": cur["title"] if cur else "",
        "current_remaining_ms": cur["remaining_ms"] if cur else 0,
        "playlist": dict(plv, ab=d["playlist"]["ab"], auto=d["playlist"]["auto"],
                         loop=d["playlist"]["loop"], ab_players=d["playlist"]["ab_players"]),
        "on_air_title": next((it["title"] for it in d["playlist"]["items"] if it["id"] == on_air), ""),
        "key_media": {{k["id"]: media_view(k) for k in page["keys"]}},
        "item_media": {{it["id"]: media_view(it) for it in d["playlist"]["items"]}},
        "counters": counters, "events": events,
        "preview": engine.preview_view(), "listen": listen_hub.stats(),
    }}
    with metrics_lock:
        out["audio"] = {{k: metrics[k] for k in ("blocks_per_s", "render_ms_max", "late_blocks",
                                                "reanchors", "write_errors", "last_error")}}
    out["cache"] = dict(library.stats, dir=library.cache)
    return out


# ─── Audio thread ────────────────────────────────────────────────────────────────────────────
def audio_thread():
    import bobimxl
    inst = bobimxl.Instance(os.environ.get("MXL_DOMAIN") or "/dev/shm/mxl")
    writers = None
    next_idx = None
    t0 = time.monotonic()
    written = 0
    count = 0
    rmax = 0.0
    tick = time.monotonic()
    while True:
        try:
            if writers is None:
                writers = [bobimxl.AudioWriter(inst, f, channels=2, sample_rate=SR, index_mode="tai")
                           for f in FLOWS]
                next_idx = None
                t0 = time.monotonic()
                written = 0
            due = int((time.monotonic() - t0) * 1000.0)
            if due - written > LATE_REANCHOR:
                with metrics_lock:
                    metrics["reanchors"] += 1
                    metrics["late_blocks"] += due - written
                t0 = time.monotonic()
                written = 0
                next_idx = None
                due = 0
            if written < due:
                r0 = time.perf_counter()
                blocks = engine.render(BLOCK)
                rmax = max(rmax, (time.perf_counter() - r0) * 1000.0)
                listen_hub.push(blocks, engine.pfl_out)               # browsers only; no client = no cost
                if next_idx is None:
                    next_idx = writers[0].write(blocks[0])        # anchor on the TAI grid, once
                    for w, b in zip(writers[1:], blocks[1:]):
                        w.write(b, index=next_idx)
                else:
                    for w, b in zip(writers, blocks):
                        w.write(b, index=next_idx)
                next_idx += BLOCK
                written += 1
                count += 1
                if written < due:
                    continue                 # catching up: no sleep, one block per pass anyway
            now = time.monotonic()
            if now - tick >= 1.0:
                with metrics_lock:
                    metrics["blocks_per_s"] = round(count / (now - tick), 1)
                    metrics["fps"] = metrics["blocks_per_s"]
                    metrics["render_ms_max"] = round(rmax, 3)
                    metrics["own_latency_ms"] = round(rmax, 3)
                    metrics["playing"] = sum(1 for p in engine.players
                                             if p.state in ("playing", "fading"))
                count, rmax, tick = 0, 0.0, now
            time.sleep(0.0004)
        except Exception as e:
            # Never silent: a feeder that dies quietly is an output that goes off air unnoticed.
            log("audio thread: %s: %s — writers reopened" % (type(e).__name__, e), "error")
            with metrics_lock:
                metrics["write_errors"] += 1
                metrics["writer_reopens"] += 1
                metrics["last_error"] = "%s: %s" % (type(e).__name__, e)
            for w in writers or []:
                try:
                    w.close()
                except Exception:
                    pass
            writers = None
            time.sleep(0.5)


def media_watch():
    """Keep every referenced media decoded and measured (loader runs in its own thread)."""
    while True:
        try:
            library.want(referenced_medias())
            counts = {{"ready": 0, "pending": 0, "missing": 0}}
            for m in referenced_medias():
                st = library.info(m).get("state")
                if st == "ready":
                    counts["ready"] += 1
                elif st in ("missing", "error"):
                    counts["missing"] += 1
                else:
                    counts["pending"] += 1
            with metrics_lock:
                metrics["media_ready"] = counts["ready"]
                metrics["media_pending"] = counts["pending"]
                metrics["media_missing"] = counts["missing"]
        except Exception as e:
            log("media watch: %s" % e, "warning")
        time.sleep(2.0)


# ─── SIGBUS ──────────────────────────────────────────────────────────────────────────────────
# A writer whose flow is re-created underneath it, or a cache file truncated by hand under a
# memmap, raises SIGBUS. Without a handler the process dies on a signal and Docker restarts it in
# a loop with nobody knowing why; with it, the audio thread logs, reopens and goes on.
def _sigbus(signum, frame):
    log("SIGBUS: MXL flow or cache mapping invalidated — reopening", "warning")
    raise IOError("SIGBUS")


signal.signal(signal.SIGBUS, _sigbus)


# ─── HTTP ────────────────────────────────────────────────────────────────────────────────────
def _reply(h, code, obj):
    body = json.dumps(obj).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


class Metrics(BaseHTTPRequestHandler):
    def do_GET(self):
        with metrics_lock:
            m = dict(metrics)
        m["players_state"] = [p.state for p in engine.players]
        _reply(self, 200, m)

    def do_POST(self):
        # :8080 also receives the TSL distributor's pushes; a GET-only server would answer 501
        # and nobody would know. Nothing to show here: acknowledged and ignored.
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        _reply(self, 200, {{"ok": True}})

    def log_message(self, *a):
        pass


def _waveform(qs):
    for kind in ("key", "item", "player"):
        v = (qs.get(kind) or [""])[0]
        if not v:
            continue
        if kind == "key":
            it = engine.find_key(v)
        elif kind == "item":
            it = next((x for x in engine.doc["playlist"]["items"] if x["id"] == v), None)
        else:
            try:
                it = engine.players[int(v) - 1].item
            except (ValueError, IndexError):
                it = None
        if not it:
            return 404, {{"error": "unknown"}}
        if it.get("edit"):
            pts, dur = edited_waveform(it)
            return 200, {{"points": pts, "duration_ms": dur, "edited": True,
                         "intro_ms": it.get("intro_ms", 0), "outro_ms": it.get("outro_ms", 0)}}
        info = library.info(it["media"])
        return 200, {{"points": library.waveform(it["media"]), "duration_ms": info.get("duration_ms"),
                     "intro_ms": it.get("intro_ms", 0), "outro_ms": it.get("outro_ms", 0)}}
    return 400, {{"error": "key, item or player required"}}


# ─── Clip editor (0.1.3): waveform windows, montage render ──────────────────────────────────
_wave_cache = {{}}                     # signature of an edited item -> (points, duration_ms)


def edited_ms(item):
    try:
        return engine.edited_ms(item)
    except Exception:
        return None


def edited_waveform(item):
    """400-point waveform of an EDITED item (rendered, cached by its edit list and settings)."""
    sig = json.dumps([item.get("media"), item.get("edit"), engine.doc["settings"]], sort_keys=True)
    hit = _wave_cache.get(sig)
    if hit:
        return hit
    try:
        m = engine.source_of(item)
    except E.EngineError:
        return [], None
    res = (MT.montage_window(m, 0, m.frames, M.WAVE_POINTS), engine._ms(m.frames))
    if len(_wave_cache) > 64:
        _wave_cache.clear()
    _wave_cache[sig] = res
    return res


def _want_target(b):
    """Decode what the editor's draft needs (a segment just added is referenced by no saved key)."""
    try:
        it, _o = engine.find_target(b)
    except E.EngineError:
        return
    it = dict(it)
    if "edit" in b:
        it["edit"] = MT.normalize_edit(b.get("edit"))
    library.want(MT.edit_medias(it))


def _q(qs, k, default=None):
    v = qs.get(k)
    return (v[0] if isinstance(v, list) else v) if v not in (None, [], "") else default


def _window_args(src_frames, q):
    a = int(round(float(_q(q, "from_ms", 0) or 0) * SR / 1000.0))
    b_ms = float(_q(q, "to_ms", 0) or 0)
    b = int(round(b_ms * SR / 1000.0)) if b_ms > 0 else src_frames
    return a, min(src_frames, b), int(float(_q(q, "points", 1000) or 1000))


def wave_source(qs):
    """GET /wave?key=|item=[&seg=i]|asset=&file=  &from_ms&to_ms&points — a window of a SOURCE
    file's waveform (the clip editor zooms in it): coarse level for wide windows, samples for
    narrow ones (montage.window). Never computed in the browser on the whole file."""
    media = None
    if _q(qs, "key") or _q(qs, "item"):
        try:
            it, _o = engine.find_target({{"key": _q(qs, "key"), "item": _q(qs, "item")}})
        except E.EngineError as ex:
            return ex.status, {{"ok": False, "error": ex.code}}
        media = it["media"]
        seg = _q(qs, "seg")
        segs = (it.get("edit") or {{}}).get("segments") or []
        if seg is not None and str(seg).isdigit() and int(seg) < len(segs) and segs[int(seg)].get("media"):
            media = segs[int(seg)]["media"]
    elif _q(qs, "asset") or _q(qs, "file"):
        media = E.normalize_media({{"asset": _q(qs, "asset"), "file": _q(qs, "file")}})
    if not media or not (media.get("asset") or media.get("file")):
        return 400, {{"ok": False, "error": "no_media"}}
    library.want([media])
    src = library.source(media)
    if src is None:
        return 409, {{"ok": False, "error": "media_not_ready"}}
    a, b, n = _window_args(src.frames, qs)
    lv = M.peaks(library, media)
    return 200, {{"points": MT.window(src.read, src.frames, a, b, n, lv), "from_ms": a * 1000.0 / SR,
                 "to_ms": b * 1000.0 / SR, "duration_ms": src.frames * 1000.0 / SR}}


def wave_montage(b):
    """POST /montage/wave {{key|item, edit?, from_ms, to_ms, points}} — a window of the EDITED
    timeline (the editor's draft when `edit` is given), plus its layout (pieces in ms)."""
    _want_target(b)
    it, _o = engine.find_target(b)
    it = dict(it)
    if "edit" in b:
        it["edit"] = MT.normalize_edit(b.get("edit"))
    src = engine.source_of(it)
    a, z, n = _window_args(src.frames, b)
    out = {{"points": MT.montage_window(src, a, z, n) if isinstance(src, MT.Montage)
            else MT.window(src.read, src.frames, a, z, n, M.peaks(library, it["media"])),
            "from_ms": a * 1000.0 / SR, "to_ms": z * 1000.0 / SR, "duration_ms": src.frames * 1000.0 / SR,
            "layout": src.layout(SR) if isinstance(src, MT.Montage) else []}}
    return out


_jobs = {{}}
_jobs_lock = threading.Lock()


def montage_render(b):
    """POST /montage/render {{key|item, edit?, gain_db?, name}} — "save as new media": the edited
    sound rendered into a WAV of the media volume, in a worker thread (never the audio one)."""
    import uuid
    _want_target(b)
    it, _o = engine.find_target(b)
    it = dict(it)
    if "edit" in b:
        it["edit"] = MT.normalize_edit(b.get("edit"))
    if b.get("gain_db") is not None:
        it["gain_db"] = b.get("gain_db")
    engine.source_of(it)                          # refuses now if a media is not ready
    # Always through a montage of its own (own buffers: the audio thread keeps reading the players'
    # ones), normalised per segment; the clip gain on top — exactly what the key plays.
    whole = {{"segments": [{{"media": None, "in_ms": 0, "out_ms": 0, "cuts": [], "xfade_ms": 0,
                            "xfade_shape": "eq"}}], "fade_in_ms": 0, "fade_in_shape": "lin",
             "fade_out_ms": 0, "fade_out_shape": "lin", "envelope": []}}
    src = MT.compile_edit(it.get("edit") or whole, it["media"], engine.source_for, engine.norm_db_for, SR)
    gain = 10.0 ** (float(it.get("gain_db") or 0.0) / 20.0)
    name = str(b.get("name") or it.get("title") or "montage").strip()[:80]
    segs = (it.get("edit") or {{}}).get("segments") or []
    first = (segs[0].get("media") if segs and segs[0].get("media") else None) or it["media"]
    jid = uuid.uuid4().hex[:12]
    with _jobs_lock:
        for k in [k for k, j in _jobs.items() if time.time() - j["t"] > 3600]:
            _jobs.pop(k, None)
        _jobs[jid] = {{"state": "running", "pct": 0, "t": time.time()}}

    def run():
        def prog(done, total):
            _jobs[jid]["pct"] = int(100 * done / max(1, total))
        try:
            r = M.save_montage(library, src, name, first, HOSTNAME, gain=gain, progress=prog)
            _jobs[jid].update(r, state="done", pct=100)
            log("new media %s (%s, %d ms, %d clipped samples)" % (r["file"], r["asset"], r["duration_ms"],
                                                                r["clipped"]))
        except Exception as ex:
            log("montage render: %s" % traceback.format_exc(), "error")
            _jobs[jid].update(state="failed", error="render_failed", detail="%s: %s" % (type(ex).__name__, ex))
    threading.Thread(target=run, daemon=True, name="montage-render").start()
    return {{"job": jid}}


def montage_job(jid):
    with _jobs_lock:
        j = _jobs.get(jid)
    if not j:
        return 404, {{"ok": False, "error": "unknown_job"}}
    return 200, {{k: v for k, v in j.items() if k != "t"}}
# ─── SAVE / RECALL (0.1.3) ─────────────────────────────────────────────────────────────────
# The library of saves lives in the ORCHESTRATOR (plugin_store, manifest `control.recall`): it
# captures here (GET /capture) and applies here (POST /recall, declared in control.persist.settings
# so the recalled cart survives a redeploy). Medias are referenced by identity, as keys are: a save
# imported from another installation may name sounds this node does not have — they are REPORTED
# (the key is kept, its sound shows as missing), never a refusal.
def _recalled_items(kind, doc, report):
    if kind == "playlist":
        return doc["playlist"]["items"]
    if kind == "page":
        return next((p["keys"] for p in doc["pages"] if p["id"] == report.get("page")), [])
    return [k for p in doc["pages"] for k in p["keys"]]


def recall(b):
    kind = b.get("kind")
    r = engine.recall(kind, b.get("payload"), page=b.get("page"), add=bool(b.get("add")))
    report = r.pop("recall")
    missing = []
    for it in _recalled_items(kind, r, report):
        m = it.get("media") or {{}}
        if (m.get("asset") or m.get("file")) and not library.resolve(m):
            missing.append({{"id": it["id"], "title": it.get("title") or "", "asset": m.get("asset"),
                            "file": m.get("file") or ""}})
    library.want(referenced_medias())
    report["missing"] = missing[:100]
    report["missing_count"] = len(missing)
    report["name"] = _text_name(b.get("name"))
    log("recall %s %r: %d renamed, %d media missing" % (kind, report["name"], report["renamed"], len(missing)))
    return {{"doc": r, "recall": report}}


def _text_name(v):
    return str(v or "").strip()[:80]


def _get(path, qs):
    if path == "/state":
        return 200, state_view()
    if path == "/doc":
        return 200, engine.doc_copy()
    if path == "/flows":
        return 200, engine.flows_view(FLOWS)
    if path == "/keys":
        return 200, {{"items": engine.key_options()}}
    if path == "/pages":
        d = engine.doc
        return 200, {{"items": [{{"value": p["id"], "label": "%d · %s" % (i + 1, p["name"] or p["id"])}}
                               for i, p in enumerate(d["pages"])]}}
    if path == "/players":
        return 200, {{"items": [{{"value": i + 1, "label": "%d · %s" % (i + 1, f)}}
                               for i, f in enumerate(FLOWS)]}}
    if path == "/items":
        d = engine.doc
        return 200, {{"items": [{{"value": i, "label": "%d · %s" % (i + 1, it["title"] or it["media"]["file"])}}
                               for i, it in enumerate(d["playlist"]["items"])]}}
    if path == "/waveform":
        return _waveform(qs)
    if path == "/wave":
        return wave_source(qs)
    if path == "/montage/job":
        return montage_job((qs.get("id") or [""])[0])
    if path == "/capture":
        try:
            return 200, engine.capture((qs.get("kind") or [""])[0], (qs.get("page") or [""])[0] or None)
        except E.EngineError as ex:
            return ex.status, {{"ok": False, "error": ex.code, "params": ex.params}}
    return 404, {{"error": "unknown"}}


def _post(path, b):
    e = engine
    if path == "/fire":
        return e.fire(b.get("key"))
    if path == "/stop":
        return e.stop_key(b.get("key"), cut=bool(b.get("cut")))
    if path == "/stop_all":
        return e.stop_all(cut=bool(b.get("cut")))
    if path == "/page":
        e.set_page(b.get("page"))
        return {{"page": e.doc["page"]}}
    if path == "/pl/go":
        return e.pl_go()
    if path == "/pl/next":
        return e.pl_step(+1)
    if path == "/pl/prev":
        return e.pl_step(-1)
    if path == "/pl/goto":
        return e.pl_goto(b.get("index"), play=bool(b.get("play")))
    if path == "/pl/options":
        return e.pl_options(b)
    if path == "/player/play":
        return e.player_play(b.get("player"))
    if path == "/player/pause":
        return e.player_pause(b.get("player"))
    if path == "/player/stop":
        return e.player_stop(b.get("player"), cut=bool(b.get("cut")), if_played=bool(b.get("if_played")))
    if path == "/key":
        r = e.set_key(b)
        library.want(referenced_medias())
        return r
    if path == "/assign":
        r = e.assign_media(b)
        library.want(referenced_medias())
        return r
    if path == "/defaults":
        return e.set_defaults(b)
    if path == "/pages":
        return e.pages_op(b)
    if path == "/grid":
        return e.set_grid(b)
    if path == "/mode":
        return e.set_mode(b.get("mode"))
    if path == "/playlist":
        r = e.playlist_op(b)
        library.want(referenced_medias())
        return r
    if path == "/settings":
        return e.set_settings(b)
    if path == "/preview":
        _want_target(b)
        return e.preview(b)
    if path == "/preview/stop":
        return e.preview_stop()
    if path == "/montage/wave":
        return wave_montage(b)
    if path == "/montage/render":
        return montage_render(b)
    if path == "/recall":
        return recall(b)
    if path == "/media/rescan":
        with library.lock:
            library.asset_scan_t = 0.0
            for k, st in list(library.status.items()):
                if st.get("state") in ("missing", "error"):
                    library.status.pop(k, None)
        library.want(referenced_medias())
        return {{"ok": True}}
    return None


class Control(BaseHTTPRequestHandler):
    def _stream(self, qs):
        """GET /listen.pcm — declared in `control.streams`, relayed as is by the orchestrator. HTTP/1.0,
        body delimited by the close; a dead client is detected at write time (5 s)."""
        src = LI.ListenHub.parse_src((qs.get("src") or ["pfl"])[0], N_PLAYERS)
        if src is None:
            return _reply(self, 400, {{"ok": False, "error": "bad_source"}})
        cl = listen_hub.open(src)
        if cl is None:
            return _reply(self, 503, {{"ok": False, "error": "too_many_listeners", "params": {{"max": listen_hub.max_clients}}}})
        try:
            self.connection.settimeout(5.0)
            self.send_response(200)
            self.send_header("Content-Type", "audio/L16;rate=48000;channels=2")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                data = cl.get(1.0)
                if data is not None:
                    self.wfile.write(data)
        except OSError:
            pass
        finally:
            cl.close()
            self.close_connection = True

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/listen.pcm":
            return self._stream(parse_qs(u.query))
        try:
            code, obj = _get(u.path, parse_qs(u.query))
        except Exception as ex:
            log("GET %s: %s" % (u.path, traceback.format_exc()), "error")
            code, obj = 500, {{"error": "%s: %s" % (type(ex).__name__, ex)}}
        _reply(self, code, obj)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{{}}") if n else {{}}
        except Exception:
            return _reply(self, 400, {{"ok": False, "error": "bad_json"}})
        if not isinstance(body, dict):
            return _reply(self, 400, {{"ok": False, "error": "bad_json"}})
        try:
            r = _post(u.path, body)
        except E.EngineError as ex:
            return _reply(self, ex.status, {{"ok": False, "error": ex.code, "params": ex.params}})
        except Exception as ex:
            log("POST %s: %s" % (u.path, traceback.format_exc()), "error")
            return _reply(self, 500, {{"ok": False, "error": "internal",
                                      "detail": "%s: %s" % (type(ex).__name__, ex)}})
        if r is None:
            return _reply(self, 404, {{"ok": False, "error": "unknown"}})
        out = {{"ok": True}}
        if isinstance(r, dict):
            out.update(r if "pages" not in r else {{"doc": r}})
        _reply(self, 200, out)

    def log_message(self, *a):
        pass


def main():
    threading.Thread(target=library.worker, args=(keep_keys,), daemon=True, name="loader").start()
    threading.Thread(target=media_watch, daemon=True, name="media").start()
    threading.Thread(target=audio_thread, daemon=True, name="audio").start()
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8080), Metrics).serve_forever(),
                     daemon=True, name="metrics").start()
    log("%s: %d player(s) — %s; media %s; cache %s" % (PLUGIN_VERSION, N_PLAYERS, ", ".join(FLOWS),
                                                       MEDIA_ROOT, library.cache))
    ThreadingHTTPServer(("0.0.0.0", 8082), Control).serve_forever()


if __name__ == "__main__":
    main()
