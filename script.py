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
LATE_REANCHOR = 200                # more than 200 blocks off the TAI grid (suspension, clock step): re-anchor
LEAD = 3 * BLOCK                   # samples written AHEAD of the TAI instant. With SCHED_FIFO and no request in flight
                                   # the worst write lag is 91 samples; under 20 requests/s a request thread holding the
                                   # GIL gets preempted and the audio thread waits for it: 255 once in 3 min (measured
                                   # 2026-10-19). 3 ms ahead keeps that under a console's 144-sample read margin; it
                                   # delays a player's reaction to Play by 3 ms, never the audio path.


def _int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


N_PLAYERS = _int(CONFIG.get("players"), 1, 8, 2)
RT_PRIORITY = _int(CONFIG.get("rt_priority"), 0, 20, 10)   # SCHED_FIFO of the audio thread, 0 = normal
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
    "write_late_max_ms": 0.0, "rt": None,
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
    out = [m for z in d["zones"] for p in z["pages"] for k in p["keys"] for m in MT.edit_medias(k)]
    out += [m for z in d["zones"] for it in z["playlist"]["items"] for m in MT.edit_medias(it)]
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
    #
    # Tabs and zones (0.1.8): `tabs` / `zones` describe every zone, ALL tabs included (a hidden
    # tab's dot lights when one of its players plays). The top-level `mode`, `page*`, `grid` and
    # `playlist` are those of the DEFAULT zone — what /state said before 0.1.8, so the declared
    # macro states and older readers keep their meaning. key_media covers the page on screen of
    # EVERY grid zone (a few dozen keys), item_media every playlist item, as before.
    with engine.lock:
        d = engine.doc
        players = engine.players_view(FLOWS)
        zv = engine.zones_view()
        gz = engine.default_zone("grid")
        pz = engine.default_zone("playlist")
        plv = next(z["playlist"] for z in zv["zones"] if z["id"] == pz["id"])
        rev = engine.rev
        counters = dict(engine.counters)
        events = list(engine.events[-8:])
    ids = [p["id"] for p in gz["pages"]]
    pi = ids.index(gz["page"]) if gz["page"] in ids else 0
    page = gz["pages"][pi]
    dz = engine.default_zone()
    cur = next((p for p in players if p["state"] in ("playing", "fading")), None)
    shown = [next(p for p in z["pages"] if p["id"] == z["page"]) for z in d["zones"]]
    out = {{
        "type": "cartouchier", "version": PLUGIN_VERSION, "hostname": HOSTNAME,
        "n_players": N_PLAYERS, "flows": FLOWS, "mode": dz["mode"], "doc_rev": rev,
        "page": gz["page"], "page_index": pi + 1, "page_name": page["name"],
        "pages_count": len(ids), "grid": gz["grid"], "settings": d["settings"],
        "defaults": d["defaults"], "page_defaults": page.get("defaults") or {{}},
        "tab": zv["tab"], "tab_index": zv["tab_index"], "tabs": zv["tabs"], "zones": zv["zones"],
        "players": players,
        "playing_count": sum(1 for p in players if p["state"] in ("playing", "fading")),
        "current_title": cur["title"] if cur else "",
        "current_remaining_ms": cur["remaining_ms"] if cur else 0,
        "playlist": plv,
        "on_air_title": next((it["title"] for it in pz["playlist"]["items"] if it["id"] == plv["on_air"]), ""),
        "key_media": {{k["id"]: media_view(k) for pg in shown for k in pg["keys"]}},
        "item_media": {{it["id"]: media_view(it) for z in d["zones"] for it in z["playlist"]["items"]}},
        "counters": counters, "events": events,
        "preview": engine.preview_view(), "listen": listen_hub.stats(),
    }}
    with metrics_lock:
        out["audio"] = {{k: metrics[k] for k in ("blocks_per_s", "render_ms_max", "late_blocks",
                                                "reanchors", "write_errors", "last_error", "write_late_max_ms", "rt")}}
    out["cache"] = dict(library.stats, dir=library.cache)
    return out


# ─── Audio thread ────────────────────────────────────────────────────────────────────────────
def _apply_rt():
    """SCHED_FIFO for the CALLING thread (Linux: pid 0 = this thread). The container shares its core with
    unpinned processes: under CFS the audio thread waited up to 6 ms to run (measured 2026-10-19, still
    331-sample spikes with the GIL at 0.5 ms and no request in flight). Needs SYS_NICE (manifest
    resources.cap_add); a refusal is published in metrics["rt"], never fatal."""
    rt = {{"requested": RT_PRIORITY, "ok": False, "error": None}}
    if RT_PRIORITY:
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(RT_PRIORITY))
            rt["ok"] = True
        except OSError as e:
            rt["error"] = "rt_not_permitted" if e.errno == 1 else "rt_failed"
            log("audio thread: real-time scheduling refused (%s)" % e, "warning")
    with metrics_lock:
        metrics["rt"] = rt


def audio_thread():
    import bobimxl
    _apply_rt()
    inst = bobimxl.Instance(os.environ.get("MXL_DOMAIN") or "/dev/shm/mxl")
    writers = None
    next_idx = None
    count = 0
    rmax = 0.0
    lag_max = 0
    tick = time.monotonic()
    while True:
        try:
            if writers is None:
                writers = [bobimxl.AudioWriter(inst, f, channels=2, sample_rate=SR, index_mode="tai")
                           for f in FLOWS]
                next_idx = None
            # Paced on the TAI sample index itself, never on time.monotonic(): the index is TAI, and a
            # monotonic pace drifts away from it (measured 2026-10-19 on 1176: 121 ms behind TAI after
            # 10 h, ~4 ppm). A block [i, i+BLOCK) is due once its END is no later than now + LEAD.
            now_idx = bobimxl.current_index(SR)
            if next_idx is None:
                next_idx = now_idx - BLOCK
            late = now_idx - (next_idx + BLOCK)
            if late > LATE_REANCHOR * BLOCK or late < -LATE_REANCHOR * BLOCK:
                with metrics_lock:
                    metrics["reanchors"] += 1
                    metrics["late_blocks"] += max(0, late // BLOCK)
                log("audio thread: %+d samples off the TAI grid — re-anchored" % late, "warning")
                next_idx = now_idx - BLOCK
                late = 0
            if next_idx + BLOCK <= now_idx + LEAD:
                lag_max = max(lag_max, late)
                r0 = time.perf_counter()
                blocks = engine.render(BLOCK)
                rmax = max(rmax, (time.perf_counter() - r0) * 1000.0)
                listen_hub.push(blocks, engine.pfl_out)               # browsers only; no client = no cost
                for w, b in zip(writers, blocks):
                    w.write(b, index=next_idx)
                next_idx += BLOCK
                count += 1
                if next_idx + BLOCK <= now_idx + LEAD:
                    continue                 # catching up: no sleep, one block per pass anyway
            now = time.monotonic()
            if now - tick >= 1.0:
                with metrics_lock:
                    metrics["blocks_per_s"] = round(count / (now - tick), 1)
                    metrics["fps"] = metrics["blocks_per_s"]
                    metrics["render_ms_max"] = round(rmax, 3)
                    metrics["own_latency_ms"] = round(rmax, 3)
                    metrics["write_late_max_ms"] = round(max(0, lag_max) * 1000.0 / SR, 2)
                    metrics["playing"] = sum(1 for p in engine.players
                                             if p.state in ("playing", "fading"))
                count, rmax, lag_max, tick = 0, 0.0, 0, now
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
            it = engine.find_item(v)[1]
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
    z = next((x for x in doc["zones"] if x["id"] == report.get("zone")), None) or {{"pages": [], "playlist": {{"items": []}}}}
    if kind == "playlist":
        return z["playlist"]["items"]
    if kind == "page":
        return next((p["keys"] for p in z["pages"] if p["id"] == report.get("page")), [])
    return [k for p in z["pages"] for k in p["keys"]]


def recall(b):
    kind = b.get("kind")
    r = engine.recall(kind, b.get("payload"), page=b.get("page"), add=bool(b.get("add")), zone=b.get("zone") or None)
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


def _get(path, qs, tab=None):
    if tab is not None:
        return scoped_get(path, qs, tab)
    if path == "/state":
        return 200, state_view()
    if path == "/doc":
        return 200, engine.doc_copy()
    if path == "/flows":
        return 200, engine.flows_view(FLOWS)
    if path == "/keys":
        return 200, {{"items": engine.key_options(_q(qs, "zone"))}}
    if path == "/pages":
        # every page of every zone (a page id names its zone), or those of ?zone=
        d = engine.doc
        zf = _q(qs, "zone")
        many = len(d["zones"]) > 1 and not zf
        return 200, {{"items": [{{"value": p["id"], "label": ("%s · " % engine.zone_label(z, zi) if many else "")
                                + "%d · %s" % (i + 1, p["name"] or p["id"])}}
                               for zi, z in enumerate(d["zones"]) if not zf or z["id"] == zf
                               for i, p in enumerate(z["pages"])]}}
    if path == "/zones":
        d = engine.doc
        tabs = {{zid: (ti, t) for ti, t in enumerate(d["tabs"]) for zid in t["zones"]}}
        out = []
        for zi, z in enumerate(d["zones"]):
            ti, t = tabs.get(z["id"], (0, {{}}))
            out.append({{"value": z["id"], "label": "%s · %s (%s)" % (t.get("name") or "%d" % (ti + 1),
                                                                    engine.zone_label(z, zi), E.zone_kind(z))}})
        return 200, {{"items": out}}
    if path == "/tabs":
        d = engine.doc
        return 200, {{"items": [{{"value": t["id"], "label": "%d · %s" % (i + 1, t["name"] or t["id"])}}
                               for i, t in enumerate(d["tabs"])]}}
    if path == "/players":
        return 200, {{"items": [{{"value": i + 1, "label": "%d · %s" % (i + 1, f)}}
                               for i, f in enumerate(FLOWS)]}}
    if path == "/items":
        try:
            z = engine.zone(_q(qs, "zone"), "playlist")
        except E.EngineError as ex:
            return ex.status, {{"ok": False, "error": ex.code, "params": ex.params}}
        return 200, {{"items": [{{"value": i, "label": "%d · %s" % (i + 1, it["title"] or it["media"]["file"])}}
                               for i, it in enumerate(z["playlist"]["items"])]}}
    if path == "/waveform":
        return _waveform(qs)
    if path == "/wave":
        return wave_source(qs)
    if path == "/montage/job":
        return montage_job((qs.get("id") or [""])[0])
    if path == "/capture":
        try:
            return 200, engine.capture(_q(qs, "kind", ""), _q(qs, "page"), _q(qs, "zone"))
        except E.EngineError as ex:
            return ex.status, {{"ok": False, "error": ex.code, "params": ex.params}}
    return 404, {{"error": "unknown"}}


def _post(path, b):
    e = engine
    if path == "/fire":
        return e.fire(b.get("key"))
    if path == "/stop":
        return e.stop_key(b.get("key"), cut=bool(b.get("cut")))
    # `zone` (0.1.8) is optional everywhere: absent or empty = the default zone of the kind (see
    # engine.default_zone) — so a macro or a console link written before the tabs acts as before.
    z = b.get("zone") or None
    if path == "/stop_all":
        return e.stop_all(cut=bool(b.get("cut")), zone=z)
    if path == "/page":
        return e.set_page(b.get("page"), zone=z)
    if path == "/pl/go":
        return e.pl_go(zone=z)
    if path == "/pl/next":
        return e.pl_step(+1, zone=z)
    if path == "/pl/prev":
        return e.pl_step(-1, zone=z)
    if path == "/pl/goto":
        return e.pl_goto(b.get("index"), play=bool(b.get("play")), zone=z)
    if path == "/tab":
        return e.set_tab(b.get("tab"))
    if path == "/tabs":
        return e.tabs_op(b)
    if path == "/layout":
        return e.set_layout(b)
    if path == "/zone":
        return e.set_zone(b)
    if path == "/set":
        return e.set_path(b.get("path"), b.get("value"))
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
        return e.set_mode(b.get("mode"), zone=z)
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


# ─── Public tab (0.1.8) ──────────────────────────────────────────────────────────────────────
# A public link (/p/<token>) may be allowed to PLAY one tab, without an account (opt-in per link).
# The orchestrator's relay forwards such a POST only for a path of `control.public_actions` and sets
# the link's scope in X-Bobi-Public-Scope ITSELF (a client-sent one is stripped there). This side is
# the second lock, and the only one that knows what a key, a zone or a player belongs to:
#   ⚠ a request carrying the scope may only press / stop a key, GO, play or stop a player of a zone
#   of THE SCOPED TAB. Anything else is 403 — another tab's key, a player of another zone, an edit.
# PUBLIC_ACTIONS must equal the manifest's control.public_actions (checked by the test): the relay
# filters on the manifest, this server on this list, and neither may be wider than the other.
PUBLIC_ACTIONS = ("/fire", "/stop", "/pl/go", "/player/play", "/player/stop")
SCOPE_HEADER = "X-Bobi-Public-Scope"


def public_scope_refusal(path, b, raw):
    """None if the scoped request may run, else the refusal code."""
    try:
        scope = json.loads(raw)
    except ValueError:
        return "bad_scope"
    if not isinstance(scope, dict) or not scope.get("play"):
        return "read_only"
    if path not in PUBLIC_ACTIONS:
        return "not_public"
    tab = scope.get("tab")
    if not tab:
        return "no_tab"
    with engine.lock:
        if path in ("/fire", "/stop"):
            zid = engine._key_zone.get(b.get("key"))
        elif path == "/pl/go":
            try:
                zid = engine.zone(b.get("zone") or None, "playlist")["id"]
            except E.EngineError:
                zid = None
        else:
            try:
                zid = engine._player_zone.get(int(b.get("player")))
            except (TypeError, ValueError):
                zid = None
        return None if zid and engine.tab_of_zone(zid) == tab else "out_of_scope"


# ─── Public tab: what a SCOPED link may READ (0.1.10) ─────────────────────────────────────────
# Through a link scoped to a tab, the read endpoints answer for THAT TAB ONLY: its zones, their
# players, pages, keys, playlist items and states. Nothing about another tab, zone, player or medium
# (title, file name, id) goes out — the page only SHOWED one tab, but /doc and /state used to carry
# all of them to anyone holding the link. Filtered HERE because only the cart knows what belongs to
# a tab. The relay always sets the header for a public link (built from scratch, a visitor's own is
# never forwarded); a public link without a tab (`{{}}`) reads as before, and so does a request
# without the header (the console's own interface). An unreadable header reads as a tab that does
# not exist: nothing at all, never everything.
_NO_TAB = "\0"


def scoped_tab(raw):
    """None = read as before; else the id of the tab the request may read (_NO_TAB if unreadable)."""
    if raw is None:
        return None
    try:
        sc = json.loads(raw)
    except ValueError:
        return _NO_TAB
    if not isinstance(sc, dict):
        return _NO_TAB
    tab = sc.get("tab")
    if tab is None:
        return None
    return tab if isinstance(tab, str) and tab else _NO_TAB


def scoped_doc(doc, tab):
    """The document reduced to the tab `tab`: its zones in full, the global settings, nothing else."""
    t = next((x for x in doc["tabs"] if x["id"] == tab), None)
    zids = set(t["zones"]) if t else set()
    return json.loads(json.dumps({{
        "version": doc.get("version"), "tab": t["id"] if t else "", "tabs": [t] if t else [],
        "zones": [z for z in doc["zones"] if z["id"] in zids],
        "settings": doc["settings"], "defaults": doc["defaults"]}}))


def _scope_zones(tab):
    t = next((x for x in engine.doc["tabs"] if x["id"] == tab), None)
    return t, (list(t["zones"]) if t else [])


def scoped_state(tab):
    """/state for a scoped link, cut to the tab. The top-level fields that describe the DEFAULT zone
    (which may sit in another tab) are re-read from the tab's own zones; counters, events, pre-listen
    and listening (the whole cart's) are left out."""
    full = state_view()
    t, zl = _scope_zones(tab)
    zids = set(zl)
    zones = [z for z in engine.doc["zones"] if z["id"] in zids]
    players = []
    for p in full["players"]:
        if p.get("zone") not in zids:
            continue
        if p.get("from_zone") and p["from_zone"] not in zids:
            p = dict(p, key=None, title="", from_zone=None)   # moved by the layout while playing another tab's sound
        players.append(p)
    gz = next((z for z in zones if z["mode"] == "grid"), None)
    pz = next((z for z in zones if z["mode"] == "playlist"), None)
    zv = [z for z in full["zones"] if z["id"] in zids]
    cur = next((p for p in players if p["state"] in ("playing", "fading")), None)
    out = {{"type": full["type"], "version": full["version"], "hostname": full["hostname"],
           "n_players": full["n_players"], "flows": [p["flow"] for p in players if p.get("flow")],
           "doc_rev": full["doc_rev"], "mode": zones[0]["mode"] if zones else "grid",
           "settings": full["settings"], "defaults": full["defaults"],
           "tab": t["id"] if t else "", "tab_index": 1, "tabs": [x for x in full["tabs"] if x["id"] == tab],
           "zones": zv, "players": players,
           "playing_count": sum(1 for p in players if p["state"] in ("playing", "fading")),
           "current_title": cur["title"] if cur else "", "current_remaining_ms": cur["remaining_ms"] if cur else 0,
           "playlist": None, "on_air_title": "", "page": None, "page_index": None, "page_name": "",
           "pages_count": 0, "grid": None, "page_defaults": {{}}, "key_media": {{}}, "item_media": {{}},
           "counters": {{}}, "events": [], "preview": None, "listen": None, "audio": full.get("audio")}}
    if gz:
        ids = [p["id"] for p in gz["pages"]]
        pi = ids.index(gz["page"]) if gz["page"] in ids else 0
        out.update(page=gz["page"], page_index=pi + 1, page_name=gz["pages"][pi]["name"], pages_count=len(ids),
                   grid=gz["grid"], page_defaults=gz["pages"][pi].get("defaults") or {{}})
    if pz:
        plv = next(z["playlist"] for z in zv if z["id"] == pz["id"])
        out["playlist"] = plv
        out["on_air_title"] = next((it["title"] for it in pz["playlist"]["items"] if it["id"] == plv["on_air"]), "")
    shown = [next(p for p in z["pages"] if p["id"] == z["page"]) for z in zones]
    out["key_media"] = {{k["id"]: full["key_media"].get(k["id"]) for pg in shown for k in pg["keys"]}}
    out["item_media"] = {{it["id"]: full["item_media"].get(it["id"]) for z in zones for it in z["playlist"]["items"]}}
    return out


def scoped_get(path, qs, tab):
    """GET through a link scoped to `tab`: every read endpoint, cut to the tab (see above)."""
    if path == "/state":
        return 200, scoped_state(tab)
    if path == "/doc":
        return 200, scoped_doc(engine.doc, tab)
    t, zl = _scope_zones(tab)
    zids = set(zl)
    unknown = (404, {{"error": "unknown"}})            # outside the tab = not there (no oracle on what exists)
    with engine.lock:
        players = [n for n in range(1, N_PLAYERS + 1) if engine._player_zone.get(n) in zids]
        keys = {{k for k, z in engine._key_zone.items() if z in zids}}
    zones = [z for z in engine.doc["zones"] if z["id"] in zids]
    zf = _q(qs, "zone")
    if path == "/flows":
        keep = {{FLOWS[n - 1] for n in players if n - 1 < len(FLOWS)}}
        return 200, {{f: v for f, v in engine.flows_view(FLOWS).items() if f in keep}}
    if path == "/keys":
        if zf and zf not in zids:
            return 200, {{"items": []}}
        return 200, {{"items": [o for o in engine.key_options(zf) if o["value"] in keys]}}
    if path == "/pages":
        zs = [z for z in zones if not zf or z["id"] == zf]
        many = len(zs) > 1
        return 200, {{"items": [{{"value": p["id"], "label": ("%s · " % engine.zone_label(z, zl.index(z["id"])) if many else "")
                                + "%d · %s" % (i + 1, p["name"] or p["id"])}}
                               for z in zs for i, p in enumerate(z["pages"])]}}
    if path == "/zones":
        tn = (t or {{}}).get("name") or "1"
        return 200, {{"items": [{{"value": z["id"], "label": "%s · %s (%s)" % (
            tn, engine.zone_label(z, zl.index(z["id"])), E.zone_kind(z))}} for z in zones]}}
    if path == "/tabs":
        return 200, {{"items": [{{"value": t["id"], "label": "1 · %s" % (t["name"] or t["id"])}}] if t else []}}
    if path == "/players":
        return 200, {{"items": [{{"value": n, "label": "%d · %s" % (n, FLOWS[n - 1])}} for n in players]}}
    if path == "/items":
        if zf:
            z = next((x for x in zones if x["id"] == zf), None)
            if z is None:
                return unknown
        else:
            z = next((x for x in zones if x["mode"] == "playlist"), None)
            if z is None:
                return 200, {{"items": []}}
        return 200, {{"items": [{{"value": i, "label": "%d · %s" % (i + 1, it["title"] or it["media"]["file"])}}
                               for i, it in enumerate(z["playlist"]["items"])]}}
    if path == "/waveform":
        k, i, p = _q(qs, "key"), _q(qs, "item"), _q(qs, "player")
        if k:
            allowed = k in keys
        elif i:
            allowed = (engine.find_item(i)[0] or {{}}).get("id") in zids
        elif p:
            try:
                allowed = int(p) in players
            except ValueError:
                allowed = False
        else:
            allowed = True                              # nothing named: the 400 of _waveform
        return _waveform(qs) if allowed else unknown
    # the clip editor's source windows, montage jobs, captures for a save: not part of a public tab
    if path in ("/wave", "/montage/job", "/capture"):
        return 403, {{"ok": False, "error": "out_of_scope"}}
    return 404, {{"error": "unknown"}}


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
            code, obj = _get(u.path, parse_qs(u.query), scoped_tab(self.headers.get(SCOPE_HEADER)))
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
        scope = self.headers.get(SCOPE_HEADER)
        if scope is not None:
            why = public_scope_refusal(u.path, body, scope)
            if why:
                log("public action refused: %s %s (%s)" % (u.path, json.dumps(body)[:200], why), "warning")
                return _reply(self, 403, {{"ok": False, "error": why}})
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
            out.update(r if "zones" not in r else {{"doc": r}})
        _reply(self, 200, out)

    def log_message(self, *a):
        pass


def main():
    # The audio thread shares the GIL with the decoder, the waveform and the HTTP threads: at the default
    # 5 ms switch interval it waited up to 6 ms for it (measured 2026-10-19: 302-sample spikes, enough to
    # dry a console reading 144 samples behind). 0.5 ms, as in the audio console.
    sys.setswitchinterval(0.0005)
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
