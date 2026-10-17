# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Cart engine: players, keys, grid pages, playlist with A/B. Pure logic.

No MXL, no ffmpeg, no HTTP, no file I/O: everything that touches the outside world is handed in
(`source_for`, `norm_db_for`). That is what lets tests/verif_plugin_cartouchier.py drive every
behaviour — exclusivity, loop, chaining, second press, fades, A/B, assigned player, free player —
with numpy arrays, and what keeps this module free of `str.format` brace doubling (it is shipped
next to the script through the manifest's `files`, never rendered).

Vocabulary
  doc      the CART: pages of keys, the playlist, shared settings. Persisted by the orchestrator
           (`control.persist`, param `cart`), so it survives a redeploy. One cart is shared by
           everybody: two independent positions deploy two carts (operator decision 2026-10-17).
  player   one stereo MXL flow. N of them, fixed at deploy time. State: idle | cued | playing |
           paused | fading.
  key      a grid button (media + behaviour). A playlist ITEM carries the same media fields.
  defaults the behaviour a key INHERITS (doc 2, 0.1.2): stop fade, second press, loop, exclusive
           group, assigned player. Three levels — the key's own value, else its page's default,
           else the cart's default. In a key, `None` means "inherited"; any other value (an empty
           group or player 0 included) is an explicit override. A playlist item inherits only its
           stop fade, from the cart.

Time is counted in SAMPLES at `sample_rate`; every value published to the outside is in ms.
The render is called by ONE audio thread, the commands by the HTTP threads: one RLock covers
both, and nothing slow (decoding, measuring) ever runs under it.
"""
import copy
import math
import threading

import numpy as np

try:
    from . import montage as MT
except ImportError:                       # imported flat (tests, or sys.path = the package dir)
    import montage as MT

STATES = ("idle", "cued", "playing", "paused", "fading")
SECOND_PRESS = ("stop", "pause", "restart")
MODES = ("grid", "playlist")
COLORS = ("", "red", "orange", "yellow", "green", "blue", "purple", "grey")

DECLICK_MS = 5          # every cut, pause and replacement is ramped over this: never a click
MAX_COLS = MAX_ROWS = 12
MAX_PAGES = 64
MAX_ITEMS = 500
MAX_FADE_MS = 30000
GAIN_MIN_DB, GAIN_MAX_DB = -40.0, 20.0
DOC_VERSION = 2

# What a key inherits, and the cart defaults of a new cart. These are ALSO the implicit values every
# key carried in doc version 1 — which is what the migration relies on (see normalize_item).
# player: 0 = any free player, n = player n.
DEFAULT_FIELDS = {"fade_out_ms": 0, "second_press": "stop", "loop": False, "group": "", "player": 0}
KEY_INHERITED = tuple(DEFAULT_FIELDS)
ITEM_INHERITED = ("fade_out_ms",)


class EngineError(Exception):
    """A refusal the operator must read (HTTP 4xx). `code` is a stable machine code: the UI
    translates it, a JSON sentence never travels to the screen."""

    def __init__(self, code, status=409, **params):
        super().__init__(code)
        self.code = code
        self.status = status
        self.params = params


class ArraySource:
    """A decoded media held in memory: float32 (frames, 2). The runtime uses a memmap with the
    same two members; tests use plain arrays."""

    def __init__(self, pcm):
        a = np.asarray(pcm, dtype=np.float32)
        if a.ndim == 1:
            a = np.repeat(a[:, None], 2, axis=1)
        self.pcm = a
        self.frames = int(a.shape[0])

    def read(self, pos, n):
        return self.pcm[pos:pos + n]


# ─── Document ────────────────────────────────────────────────────────────────────────────────
def default_doc():
    return {
        "version": DOC_VERSION,
        "mode": "grid",
        "grid": {"cols": 4, "rows": 3},
        "page": "p1",
        "pages": [{"id": "p1", "name": "", "keys": []}],
        "playlist": {"items": [], "ab": True, "ab_players": [1, 2], "auto": False, "loop": False},
        "settings": {"target_lufs": -23.0, "normalize": True, "max_gain_db": 12.0,
                     "peak_ceiling_db": -1.0, "alert_s": 10},
        "defaults": dict(DEFAULT_FIELDS),
        "seq": 1,
    }


def _num(v, lo, hi, default, cast=float):
    try:
        x = cast(v)
    except (TypeError, ValueError):
        return default
    if isinstance(x, float) and not math.isfinite(x):
        return default
    return max(lo, min(hi, x))


def _text(v, n):
    return str(v if v is not None else "").strip()[:n]


def normalize_media(m):
    """{asset, file}: the media IDENTITY first (media base), the path as a hint — same rule as the
    player's playlist entries. A path is relative to the media volume; `..` is refused."""
    m = m if isinstance(m, dict) else {}
    asset = _text(m.get("asset"), 64) or None
    f = _text(m.get("file"), 512)
    if ".." in f.replace("\\", "/").split("/"):
        f = ""
    return {"asset": asset, "file": f}


def _media_set(m):
    return bool(m.get("asset") or m.get("file"))


def norm_field(name, v, n_players):
    """One inheritable value, bounded. None in = None out (inherited); an unreadable value is
    None too: it falls back on the default instead of becoming a surprising override."""
    if v is None:
        return None
    if name == "fade_out_ms":
        return _num(v, 0, MAX_FADE_MS, None, int)
    if name == "second_press":
        return v if v in SECOND_PRESS else None
    if name == "loop":
        return bool(v)
    if name == "group":
        return _text(v, 32)
    if name == "player":
        if v in ("", 0, "0", False):
            return 0
        return _num(v, 1, n_players, None, int)
    return None


def normalize_defaults(src, n_players, partial=False):
    """Cart defaults (`partial=False`: every field, missing ones at DEFAULT_FIELDS) or page
    defaults (`partial=True`: only the fields the page overrides)."""
    src = src if isinstance(src, dict) else {}
    out = {}
    for f, dflt in DEFAULT_FIELDS.items():
        v = norm_field(f, src.get(f), n_players)
        if v is None and not partial:
            v = dflt
        if v is not None:
            out[f] = v
    return out


def resolve(item, page_defaults, cart_defaults, fields=KEY_INHERITED):
    """Effective value of each inherited field: the item's own, else the page's, else the cart's."""
    out = {}
    for f in fields:
        v = item.get(f)
        if v is None:
            v = (page_defaults or {}).get(f)
        if v is None:
            v = (cart_defaults or {}).get(f)
        if v is None:
            v = DEFAULT_FIELDS[f]
        out[f] = v
    return out


def normalize_item(k, n_players, is_key=True, legacy=False):
    """A key (grid) or a playlist item. Unknown fields are dropped; every value is bounded.

    Inherited fields stay None. `legacy` (a doc older than version 2, where every key carried
    every value): a value EQUAL to the old implicit default becomes inherited again — there is no
    way to tell an operator's choice of "0 ms" from the default it was given, and keeping them all
    as overrides would make the new defaults act on nothing. A different value stays an override."""
    k = k if isinstance(k, dict) else {}
    out = {
        "id": _text(k.get("id"), 32),
        "title": _text(k.get("title"), 80),
        "media": normalize_media(k.get("media")),
        "gain_db": round(_num(k.get("gain_db", 0.0), GAIN_MIN_DB, GAIN_MAX_DB, 0.0), 2),
        "intro_ms": _num(k.get("intro_ms", 0), 0, 24 * 3600 * 1000, 0, int),
        "outro_ms": _num(k.get("outro_ms", 0), 0, 24 * 3600 * 1000, 0, int),
        "color": k.get("color") if k.get("color") in COLORS else "",
        "edit": MT.normalize_edit(k.get("edit")),      # clip editor (0.1.3): None = the media whole
    }
    fields = KEY_INHERITED if is_key else ITEM_INHERITED
    for f in fields:
        raw = k.get(f)
        if legacy and f == "player" and raw in (None, "", 0, "0"):
            raw = None                    # version 1: no player = any free player = the default
        v = norm_field(f, raw, n_players)
        if legacy and v == DEFAULT_FIELDS[f]:
            v = None
        out[f] = v
    if not is_key:
        out["loop"] = bool(k.get("loop", False))
    else:
        out.update({
            "slot": _num(k.get("slot", 0), 0, MAX_COLS * MAX_ROWS - 1, 0, int),
            "next": _text(k.get("next"), 32),
        })
    return out


def _next_id(d, prefix):
    """A new key / page / item id, never one already in the document. `seq` alone is not enough: a
    document written elsewhere (imported, hand-edited) may hold ids above it, and a reused id made
    the NEW element take the old one's place (the old one was renamed by normalize_doc)."""
    used = {p["id"] for p in d["pages"]} | {k["id"] for p in d["pages"] for k in p["keys"]} \
        | {it["id"] for it in d["playlist"]["items"]}
    while True:
        d["seq"] += 1
        c = "%s%d" % (prefix, d["seq"])
        if c not in used:
            return c


def normalize_doc(doc, n_players):
    """Canonical cart document. Never raises: an unreadable field falls back to its default, so a
    damaged persisted doc degrades instead of preventing the container from starting."""
    d = default_doc()
    src = doc if isinstance(doc, dict) else {}
    # A document without a version, or of version 1, predates the defaults: migrated (see
    # normalize_item). An unreadable version is treated as the oldest, never a crash.
    legacy = _num(src.get("version", 1), 1, 10 ** 6, 1, int) < 2
    d["mode"] = src.get("mode") if src.get("mode") in MODES else "grid"
    g = src.get("grid") if isinstance(src.get("grid"), dict) else {}
    d["grid"] = {"cols": _num(g.get("cols", 4), 1, MAX_COLS, 4, int),
                 "rows": _num(g.get("rows", 3), 1, MAX_ROWS, 3, int)}
    seq = _num(src.get("seq", 1), 1, 10 ** 9, 1, int)
    used = set()

    def _uid(prefix, cur):
        nonlocal seq
        if cur and cur not in used:
            used.add(cur)
            return cur
        while True:
            seq += 1
            c = "%s%d" % (prefix, seq)
            if c not in used:
                used.add(c)
                return c

    pages = []
    for p in (src.get("pages") or [])[:MAX_PAGES]:
        if not isinstance(p, dict):
            continue
        keys, slots = [], set()
        for k in p.get("keys") or []:
            nk = normalize_item(k, n_players, True, legacy)
            if nk["slot"] in slots:
                continue                      # two keys on one slot: the first wins
            slots.add(nk["slot"])
            nk["id"] = _uid("k", nk["id"])
            keys.append(nk)
        keys.sort(key=lambda x: x["slot"])
        pages.append({"id": _uid("p", _text(p.get("id"), 32)), "name": _text(p.get("name"), 40),
                      "keys": keys,
                      "defaults": normalize_defaults(p.get("defaults"), n_players, partial=True)})
    if not pages:
        pages = [{"id": _uid("p", "p1"), "name": "", "keys": [], "defaults": {}}]
    d["pages"] = pages
    ids = {p["id"] for p in pages}
    d["page"] = src.get("page") if src.get("page") in ids else pages[0]["id"]

    pl = src.get("playlist") if isinstance(src.get("playlist"), dict) else {}
    items = []
    for it in (pl.get("items") or [])[:MAX_ITEMS]:
        ni = normalize_item(it, n_players, False, legacy)
        ni["id"] = _uid("i", ni["id"])
        items.append(ni)
    abp = pl.get("ab_players") if isinstance(pl.get("ab_players"), list) else [1, 2]
    abp = [_num(x, 1, n_players, 1, int) for x in abp[:2]]
    if len(abp) < 2 or abp[0] == abp[1]:
        abp = [1, 2 if n_players >= 2 else 1]
    d["playlist"] = {"items": items, "ab": bool(pl.get("ab", True)) and n_players >= 2,
                     "ab_players": abp, "auto": bool(pl.get("auto", False)),
                     "loop": bool(pl.get("loop", False))}
    s = src.get("settings") if isinstance(src.get("settings"), dict) else {}
    d["settings"] = {
        "target_lufs": round(_num(s.get("target_lufs", -23.0), -40.0, -5.0, -23.0), 1),
        "normalize": bool(s.get("normalize", True)),
        "max_gain_db": round(_num(s.get("max_gain_db", 12.0), 0.0, 30.0, 12.0), 1),
        "peak_ceiling_db": round(_num(s.get("peak_ceiling_db", -1.0), -20.0, 0.0, -1.0), 1),
        "alert_s": _num(s.get("alert_s", 10), 0, 120, 10, int),
    }
    d["defaults"] = normalize_defaults(src.get("defaults"), n_players)
    # References that point nowhere are cleared, not kept dangling.
    key_ids = {k["id"] for p in pages for k in p["keys"]}
    for p in pages:
        for k in p["keys"]:
            if k["next"] not in key_ids:
                k["next"] = ""
    d["seq"] = seq
    return d


# ─── Players ─────────────────────────────────────────────────────────────────────────────────
class Player:
    def __init__(self, n):
        self.n = n                  # 1-based, as on the screen and in the flow name
        self.reset()
        self.tails = []             # playbacks being de-clicked out after a cut
        self.pending_cue = None     # playlist item id to cue as soon as this player is idle
        self.preview = False        # the PRE-LISTEN player (0.1.3): off air, no flow, no chaining

    def reset(self):
        self.state = "idle"
        self.item = None            # key or playlist item (a COPY: an edit never moves a playing sound)
        self.origin = None          # "grid" | "playlist"
        self.source = None
        self.pos = 0
        self.gain = 1.0             # linear: normalisation + key gain
        self.gain_db = 0.0
        self.env = 1.0              # fade envelope (amplitude, before the square law)
        self.env_step = 0.0         # per sample; < 0 = fading out
        self.after_fade = None      # "stop" | "pause" when the envelope reaches 0
        self.stop_at = None         # pre-listen only: frame where the playback ends (a selection)

    def busy(self):
        return self.state != "idle"


class CartEngine:
    def __init__(self, n_players=2, sample_rate=48000, doc=None, source_for=None,
                 norm_db_for=None):
        self.n_players = max(1, min(8, int(n_players)))
        self.sr = int(sample_rate)
        self.lock = threading.RLock()
        self.players = [Player(i + 1) for i in range(self.n_players)]
        self.source_for = source_for or (lambda media: None)
        self.norm_db_for = norm_db_for or (lambda media: 0.0)
        self.doc = normalize_doc(doc, self.n_players)
        self._key_page = {}
        self._index()
        self.pl_on_air = None       # playlist item id currently on air
        self.pl_ready = None        # playlist item id that the next GO plays
        self.events = []            # last events, for /state (bounded)
        self.counters = {"fired": 0, "refused": 0, "chained": 0, "looped": 0}
        self.rev = 0                # bumped at every document change: consoles refetch /doc on it
        self._declick = max(1, int(self.sr * DECLICK_MS / 1000))
        self.pfl = Player(0)        # pre-listen (0.1.3): rendered after the players, to no flow
        self.pfl.preview = True
        self.pfl_out = None
        self.pfl_what = None        # {"key"|"item": id, "start_ms", "end_ms"} of the current pre-listen
        self._reset_ready()
        self.recue()

    # ── helpers ──
    def _ms(self, frames):
        return int(round(frames * 1000.0 / self.sr))

    def _event(self, kind, **kw):
        self.events.append(dict(kind=kind, **kw))
        del self.events[:-20]

    def _index(self):
        self._key_page = {k["id"]: p["id"] for p in self.doc["pages"] for k in p["keys"]}

    def effective(self, item, origin="grid"):
        """Resolved inherited values of a key (or a player's copy of it: `_page` is set at load)
        or of a playlist item. Read from the CURRENT doc: a default changed now applies now."""
        d = self.doc
        if origin != "grid":
            return resolve(item, None, d["defaults"], ITEM_INHERITED)
        pid = item.get("_page") or self._key_page.get(item.get("id"))
        pg = next((p for p in d["pages"] if p["id"] == pid), None)
        return resolve(item, pg.get("defaults") if pg else None, d["defaults"])

    def _fade_for(self, item, origin="grid"):
        return self.effective(item or {}, origin)["fade_out_ms"]

    def find_key(self, key_id):
        for p in self.doc["pages"]:
            for k in p["keys"]:
                if k["id"] == key_id:
                    return k
        return None

    def _items(self):
        return self.doc["playlist"]["items"]

    def _item_index(self, item_id):
        for i, it in enumerate(self._items()):
            if it["id"] == item_id:
                return i
        return None

    def _player(self, n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise EngineError("bad_player", 400)
        if not 1 <= n <= self.n_players:
            raise EngineError("bad_player", 400, n=n)
        return self.players[n - 1]

    def holder(self, key_id, origin="grid"):
        for p in self.players:
            if p.busy() and p.origin == origin and p.item and p.item["id"] == key_id:
                return p
        return None

    def _gain_for(self, item):
        if item.get("edit"):        # an edit list normalises each of its segments itself
            g = float(item.get("gain_db") or 0.0)
            return g, 10.0 ** (g / 20.0)
        g = float(self.norm_db_for(item["media"]) or 0.0) + float(item.get("gain_db") or 0.0)
        return g, 10.0 ** (g / 20.0)

    # ── low-level transitions ──
    def _load(self, p, item, origin, state):
        """Put `item` on player `p` (whatever it held is cut, de-clicked). Raises if the media is
        not ready: a key whose sound is still being decoded must say so, not play silence."""
        src = self.source_of(item)
        self._cut(p)
        it = copy.deepcopy(item)
        if origin == "grid":
            # What the sound DOES is fixed at launch (loop, group, second press, player): an edit
            # never moves a playing sound. The stop fade is left as the key's own value (None =
            # inherited) and resolved when the stop comes — a default changed during playback
            # applies to the fade, which is harmless.
            eff = self.effective(item, "grid")
            it["_page"] = self._key_page.get(item.get("id"))
            for f in ("loop", "group", "second_press", "player"):
                it[f] = eff[f]
        p.item = it
        p.origin = origin
        p.source = src
        p.pos = 0
        p.gain_db, p.gain = self._gain_for(item)
        p.env, p.env_step, p.after_fade = 1.0, 0.0, None
        p.state = state

    def _cut(self, p):
        """Stop NOW without a click: the current playback continues as a tail, ramped to zero over
        DECLICK_MS, while the player itself is already free."""
        if p.state in ("playing", "fading") and p.source is not None:
            p.tails.append({"src": p.source, "pos": p.pos, "gain": p.gain, "env": p.env * p.env,
                            "step": 1.0 / self._declick})
        p.reset()

    def _release(self, p, reason):
        if p.preview:
            p.reset()
            return
        self._event("released", player=p.n, reason=reason,
                    key=(p.item or {}).get("id"))
        p.reset()
        self._apply_pending_cue(p)

    def _apply_pending_cue(self, p):
        iid = p.pending_cue
        p.pending_cue = None
        if iid is None or p.busy():
            return
        idx = self._item_index(iid)
        if idx is None:
            return
        try:
            self._load(p, self._items()[idx], "playlist", "cued")
        except EngineError:
            pass                      # not decoded yet: cued at the next GO, not lost

    def _fade_out(self, p, fade_ms, then="stop"):
        """Fade `p` out over fade_ms then stop (or pause). 0 ms = de-clicked cut."""
        if p.state == "paused" or p.state == "cued":
            if then == "stop":
                self._release(p, "stop")
            return
        if p.state not in ("playing", "fading"):
            return
        n = int(self.sr * max(0, int(fade_ms)) / 1000)
        if n <= 0:
            if then == "pause":
                self._pause_now(p)
            else:
                self._cut(p)
                self._release_after_cut(p)
            return
        p.env_step = -max(p.env, 1e-6) / n
        p.after_fade = then
        p.state = "fading"

    def _release_after_cut(self, p):
        self._event("released", player=p.n, reason="cut", key=None)
        self._apply_pending_cue(p)

    def _pause_now(self, p):
        """Pause at the current sample; the next 5 ms continue as a tail so the stop is clean."""
        p.tails.append({"src": p.source, "pos": p.pos, "gain": p.gain, "env": p.env * p.env,
                        "step": 1.0 / self._declick})
        p.state = "paused"
        p.env, p.env_step, p.after_fade = 1.0, 0.0, None

    def _start(self, p, from_pos=None):
        if from_pos is not None:
            p.pos = int(from_pos)
        if p.pos > 0:                 # resuming mid-sound: ramp in, never a step
            p.env, p.env_step = 0.0, 1.0 / self._declick
        else:
            p.env, p.env_step = 1.0, 0.0
        p.after_fade = None
        p.state = "playing"

    # ── grid ──
    def fire(self, key_id):
        """A key is PRESSED. First press: launch. Press while it plays: its `second_press`."""
        with self.lock:
            k = self.find_key(key_id)
            if k is None:
                self.counters["refused"] += 1
                raise EngineError("unknown_key", 404, key=key_id)
            h = self.holder(key_id)
            if h is not None:
                return self._second_press(h, k)
            return self._launch(k)

    def _second_press(self, p, k):
        if p.state == "cued":
            self._start(p)
            return {"player": p.n, "action": "start"}
        if p.state == "paused":
            self._start(p)
            return {"player": p.n, "action": "resume"}
        if p.state == "fading":
            # Pressing during a fade-out cuts it: the operator wants it gone NOW.
            self._cut(p)
            self._release_after_cut(p)
            return {"player": p.n, "action": "cut"}
        sp = self.effective(k)["second_press"]
        if sp == "pause":
            self._pause_now(p)
            return {"player": p.n, "action": "pause"}
        if sp == "restart":
            src, item = p.source, p.item
            self._cut(p)
            p.item, p.origin, p.source = item, "grid", src
            p.gain_db, p.gain = self._gain_for(item)
            self._start(p, 0)
            return {"player": p.n, "action": "restart"}
        self._fade_out(p, self._fade_for(k))
        return {"player": p.n, "action": "stop"}

    def _exclusive(self, k):
        g = self.effective(k)["group"]
        if not g:
            return
        for p in self.players:
            if (p.busy() and p.origin == "grid" and p.item and p.item.get("group") == g
                    and p.item["id"] != k["id"] and p.state != "fading"):
                self._fade_out(p, self._fade_for(p.item, p.origin))

    def _alloc(self, k):
        pl = self.effective(k)["player"]
        if pl and 1 <= pl <= self.n_players:
            return self.players[pl - 1]
        for p in self.players:
            if not p.busy() and p.pending_cue is None:
                return p
        for p in self.players:
            if not p.busy():
                return p
        return None

    def _launch(self, k, on=None):
        self._exclusive(k)
        p = on or self._alloc(k)
        if p is None:
            self.counters["refused"] += 1
            raise EngineError("no_free_player", 409, key=k["id"])
        self._load(p, k, "grid", "playing")
        self._start(p, 0)
        self.counters["fired"] += 1
        self._event("fired", player=p.n, key=k["id"])
        return {"player": p.n, "action": "start"}

    def stop_key(self, key_id, cut=False):
        with self.lock:
            p = self.holder(key_id)
            if p is None:
                return {"stopped": False}
            if cut:
                self._cut(p)
                self._release_after_cut(p)
            else:
                self._fade_out(p, self._fade_for(p.item, p.origin))
            return {"stopped": True, "player": p.n}

    def stop_all(self, cut=False):
        with self.lock:
            n = 0
            for p in self.players:
                if p.state in ("playing", "fading", "paused"):
                    n += 1
                    if cut or p.state == "paused":
                        self._cut(p)
                        self._release_after_cut(p)
                    elif p.state == "playing":
                        self._fade_out(p, self._fade_for(p.item, p.origin))
            return {"stopped": n}

    # ── players (direct) ──
    def player_play(self, n):
        with self.lock:
            p = self._player(n)
            if p.state in ("cued", "paused"):
                if p.origin == "playlist" and p.state == "cued":
                    return self.pl_go()
                self._start(p)
                return {"player": p.n, "action": "start"}
            return {"player": p.n, "action": "none", "state": p.state}

    def player_pause(self, n):
        with self.lock:
            p = self._player(n)
            if p.state == "playing":
                self._pause_now(p)
                return {"player": p.n, "action": "pause"}
            if p.state == "paused":
                self._start(p)
                return {"player": p.n, "action": "resume"}
            return {"player": p.n, "action": "none", "state": p.state}

    def player_stop(self, n, cut=False):
        with self.lock:
            p = self._player(n)
            if not p.busy():
                return {"player": p.n, "action": "none"}
            if cut or p.state in ("paused", "cued"):
                was_cued = p.state == "cued"
                self._cut(p)
                if not was_cued:
                    self._release_after_cut(p)
            else:
                self._fade_out(p, self._fade_for(p.item, p.origin))
            return {"player": p.n, "action": "stop"}

    # ── playlist ──
    def _ab(self):
        pl = self.doc["playlist"]
        return [self.players[i - 1] for i in pl["ab_players"]] if pl["ab"] else None

    def _reset_ready(self):
        items = self._items()
        if self.pl_ready is None or self._item_index(self.pl_ready) is None:
            self.pl_ready = items[0]["id"] if items else None
        if self.pl_on_air is not None and self._item_index(self.pl_on_air) is None:
            self.pl_on_air = None

    def _following(self, item_id):
        items = self._items()
        i = self._item_index(item_id)
        if i is None:
            return items[0]["id"] if items else None
        if i + 1 < len(items):
            return items[i + 1]["id"]
        return items[0]["id"] if (items and self.doc["playlist"]["loop"]) else None

    def _cued_playlist_player(self):
        for p in self.players:
            if p.state == "cued" and p.origin == "playlist":
                return p
        return None

    def recue(self):
        """Make the player that HOLDS the next item agree with `pl_ready` (A/B only)."""
        with self.lock:
            ab = self._ab()
            if not ab or self.doc["mode"] != "playlist":
                # Outside A/B playlist mode nothing may sit cued: it would hold a player that a
                # grid key could not take.
                for p in self.players:
                    p.pending_cue = None
                    if p.state == "cued" and p.origin == "playlist":
                        p.reset()
                return
            c = self._cued_playlist_player()
            if self.pl_ready is None:
                if c is not None:
                    c.reset()
                return
            idx = self._item_index(self.pl_ready)
            if c is not None and c.item and c.item["id"] == self.pl_ready:
                return
            target = c
            if target is None:
                for p in ab:
                    if not p.busy():
                        target = p
                        break
            if target is None:
                # both A and B busy (one on air, the other fading out): cue when it frees up
                for p in ab:
                    if p.state == "fading":
                        p.pending_cue = self.pl_ready
                return
            if c is not None:
                c.reset()
            try:
                self._load(target, self._items()[idx], "playlist", "cued")
            except EngineError:
                target.pending_cue = self.pl_ready

    def pl_go(self):
        """GO: the ready item goes on air; what was on air fades with its own fade."""
        with self.lock:
            items = self._items()
            if not items:
                raise EngineError("playlist_empty", 409)
            self._reset_ready()
            if self.pl_ready is None:
                raise EngineError("playlist_end", 409)
            ab = self._ab()
            on_air_players = [p for p in self.players
                              if p.origin == "playlist" and p.state in ("playing", "paused")]
            target = None
            if ab:
                c = self._cued_playlist_player()
                if c is not None and c.item["id"] == self.pl_ready:
                    target = c
                else:
                    for p in ab:
                        if not p.busy() and p not in on_air_players:
                            target = p
                            break
                    if target is None:
                        for p in ab:
                            if not p.busy():
                                target = p
                                break
            else:
                for p in self.players:
                    if not p.busy():
                        target = p
                        break
            if target is None:
                self.counters["refused"] += 1
                raise EngineError("no_free_player", 409)
            if target.state != "cued":
                self._load(target, items[self._item_index(self.pl_ready)], "playlist", "cued")
            for p in on_air_players:
                if p is not target:
                    self._fade_out(p, self._fade_for(p.item, p.origin))
            self._start(target, 0)
            self.pl_on_air = target.item["id"]
            self.pl_ready = self._following(self.pl_on_air)
            self.counters["fired"] += 1
            self._event("go", player=target.n, item=self.pl_on_air)
            if ab and self.pl_ready is not None:
                # B holds the following item ready: now if it is free, when its fade ends if not.
                other = ab[1] if target is ab[0] else ab[0]
                other.pending_cue = self.pl_ready
                if not other.busy():
                    self._apply_pending_cue(other)
            return {"player": target.n, "item": self.pl_on_air, "ready": self.pl_ready}

    def pl_step(self, delta):
        """Move the READY pointer (next / previous) without touching what is on air."""
        with self.lock:
            items = self._items()
            if not items:
                return {"ready": None}
            i = self._item_index(self.pl_ready)
            if i is None:
                i = -1 if delta > 0 else len(items)
            i = max(0, min(len(items) - 1, i + int(delta)))
            self.pl_ready = items[i]["id"]
            for p in self.players:
                if p.pending_cue is not None:
                    p.pending_cue = self.pl_ready
            self.recue()
            return {"ready": self.pl_ready, "index": i}

    def pl_goto(self, index, play=False):
        with self.lock:
            items = self._items()
            try:
                i = int(index)
            except (TypeError, ValueError):
                raise EngineError("bad_index", 400)
            if not 0 <= i < len(items):
                raise EngineError("bad_index", 400, index=i)
            self.pl_ready = items[i]["id"]
            self.recue()
            if play:
                return self.pl_go()
            return {"ready": self.pl_ready, "index": i}

    # ── render ──
    def render(self, n):
        """n frames for every player: list of float32 (n, 2). Called by the audio thread only."""
        with self.lock:
            out = [self._render_player(p, n) for p in self.players]
            self.pfl_out = self._render_player(self.pfl, n) if self.pfl.busy() or self.pfl.tails else None
            return out

    def _render_player(self, p, n):
        out = np.zeros((n, 2), dtype=np.float32)
        filled = 0
        guard = 0
        while filled < n and p.state in ("playing", "fading") and guard < 8:
            guard += 1
            src = p.source
            end = src.frames if p.stop_at is None else min(src.frames, p.stop_at)
            avail = end - p.pos
            take = min(n - filled, avail)
            if take > 0:
                env, used, done = self._envelope(p, take)
                if used > 0:
                    chunk = src.read(p.pos, used)
                    out[filled:filled + used] = chunk * (p.gain * env)[:, None]
                    p.pos += used
                    filled += used
                if done:
                    action = p.after_fade
                    if action == "pause":
                        p.state = "paused"
                        p.env, p.env_step, p.after_fade = 1.0, 0.0, None
                    else:
                        self._release(p, "faded")
                    break
            if p.state in ("playing", "fading") and p.pos >= end:
                self._on_end(p)
        if p.tails:
            self._render_tails(p, out)
        return out

    def _envelope(self, p, take):
        """(gain array for up to `take` samples, samples used, fade finished)."""
        if p.env_step == 0.0:
            g = p.env * p.env
            return np.full(take, g, dtype=np.float32), take, False
        k = np.arange(1, take + 1, dtype=np.float64)
        e = p.env + p.env_step * k
        if p.env_step > 0:
            e = np.minimum(e, 1.0)
            p.env = float(e[-1])
            if p.env >= 1.0:
                p.env, p.env_step = 1.0, 0.0
            return (e * e).astype(np.float32), take, False
        hit = np.nonzero(e <= 0.0)[0]
        if hit.size:
            used = int(hit[0]) + 1
            e = np.maximum(e[:used], 0.0)
            p.env = 0.0
            return (e * e).astype(np.float32), used, True
        p.env = float(e[-1])
        return (e * e).astype(np.float32), take, False

    def _render_tails(self, p, out):
        keep = []
        n = out.shape[0]
        for t in p.tails:
            k = np.arange(1, n + 1, dtype=np.float64)
            e = t["env"] - t["step"] * k
            m = int(np.count_nonzero(e > 0.0))
            m = min(m, t["src"].frames - t["pos"])
            if m > 0:
                out[:m] += t["src"].read(t["pos"], m) * (t["gain"] * e[:m]).astype(np.float32)[:, None]
                t["pos"] += m
                t["env"] = float(e[m - 1])
            if m == n and t["env"] > 0.0 and t["pos"] < t["src"].frames:
                keep.append(t)
        p.tails = keep

    def _on_end(self, p):
        item = p.item or {}
        if p.preview:               # pre-listen: plays its range once, chains nothing
            if p.stop_at is not None and p.pos < p.source.frames:
                p.stop_at = None    # end of a selection: faded out over the next 5 ms, never a click
                self._fade_out(p, DECLICK_MS)
            else:
                p.reset()
            return
        if item.get("loop"):
            p.pos = 0
            self.counters["looped"] += 1
            return
        if p.state == "fading":
            self._release(p, "end")
            return
        if p.origin == "grid" and item.get("next"):
            nxt = self.find_key(item["next"])
            if nxt is not None:
                # Chained on the SAME player when the next key has none of its own: gapless (the
                # render loop goes on filling the very same block from the next sound).
                npl = self.effective(nxt)["player"]
                on = None if npl and npl != p.n else p
                if on is p:
                    p.reset()
                else:
                    self._release(p, "end")
                try:
                    self._launch(nxt, on=on)
                    self.counters["chained"] += 1
                except EngineError as e:
                    self._event("chain_failed", player=p.n, key=nxt["id"], code=e.code)
                    if on is p:
                        self._apply_pending_cue(p)
                return
        was_playlist = p.origin == "playlist"
        self._release(p, "end")
        if was_playlist and self.doc["mode"] == "playlist" and self.doc["playlist"]["auto"]:
            try:
                self.pl_go()
            except EngineError:
                pass

    # ── clip edit + pre-listen (0.1.3) ──
    def source_of(self, item, edit=False):
        """What a player reads for `item`: its decoded media, or its EDIT LIST compiled into a
        montage (montage.py) — same `frames` / `read`, so everything downstream counts the edited
        duration. `edit` overrides the item's own list (the editor's unsaved draft; None = whole
        media). Raises if a media is not ready: a key whose sound is still being decoded must say
        so, not play silence."""
        e = item.get("edit") if edit is False else MT.normalize_edit(edit)
        if not _media_set(item.get("media") or {}) and not e:
            raise EngineError("no_media", 409, key=item.get("id"))
        if e:
            try:
                return MT.compile_edit(e, item.get("media"), self.source_for, self.norm_db_for, self.sr)
            except MT.MontageError as ex:
                raise EngineError(ex.code, 409, key=item.get("id"), **ex.params)
        if not _media_set(item["media"]):
            raise EngineError("no_media", 409, key=item.get("id"))
        src = self.source_for(item["media"])
        if src is None or not getattr(src, "frames", 0):
            raise EngineError("media_not_ready", 409, key=item.get("id"))
        return src

    def find_target(self, body):
        """{key} | {item} -> (item, origin). The editor and the pre-listen address one or the other."""
        if body.get("key"):
            k = self.find_key(body.get("key"))
            if k is None:
                raise EngineError("unknown_key", 404, key=body.get("key"))
            return k, "grid"
        if body.get("item"):
            i = self._item_index(body.get("item"))
            if i is None:
                raise EngineError("unknown_item", 404, item=body.get("item"))
            return self._items()[i], "playlist"
        raise EngineError("no_target", 400)

    def edited_ms(self, item):
        """Duration of the item as it PLAYS (edit list applied), or None if a media is not ready."""
        if not item.get("edit"):
            return None
        try:
            return self._ms(self.source_of(item).frames)
        except EngineError:
            return None

    def preview(self, body):
        """PRE-LISTEN: play a key or an item (or the editor's draft: `edit`, `gain_db`) on the
        off-air player, from `start_ms` to `end_ms` (0 / absent = to the end). Touches no player and
        no flow: heard only by the browsers listening to `pfl`. A new pre-listen replaces the
        current one (de-clicked)."""
        with self.lock:
            item, origin = self.find_target(body or {})
            it = copy.deepcopy(item)
            if "edit" in (body or {}):
                it["edit"] = MT.normalize_edit(body.get("edit"))
            if body.get("gain_db") is not None:
                it["gain_db"] = _num(body.get("gain_db"), GAIN_MIN_DB, GAIN_MAX_DB, 0.0)
            src = self.source_of(it)
            p = self.pfl
            self._cut(p)
            a = _num(body.get("start_ms", 0), 0, 24 * 3600 * 1000, 0, int)
            b = _num(body.get("end_ms", 0), 0, 24 * 3600 * 1000, 0, int)
            start = min(src.frames, int(round(a * self.sr / 1000.0)))
            stop = min(src.frames, int(round(b * self.sr / 1000.0))) if b else None
            if stop is not None and stop <= start:
                raise EngineError("bad_range", 400)
            p.item, p.origin, p.source = it, origin, src
            p.gain_db, p.gain = self._gain_for(it)
            p.stop_at = stop
            self._start(p, start)
            self.pfl_what = {"key" if origin == "grid" else "item": item["id"],
                             "start_ms": a, "end_ms": b, "draft": "edit" in (body or {})}
            self.counters["previewed"] = self.counters.get("previewed", 0) + 1
            return {"preview": True, "duration_ms": self._ms(src.frames)}

    def preview_stop(self):
        with self.lock:
            was = self.pfl.busy()
            self._cut(self.pfl)
            self.pfl_what = None
            return {"stopped": was}

    def preview_view(self):
        p = self.pfl
        it = p.item or {}
        return {"state": p.state, "title": it.get("title") or "",
                "key": (self.pfl_what or {}).get("key") if p.busy() else None,
                "item": (self.pfl_what or {}).get("item") if p.busy() else None,
                "elapsed_ms": self._ms(p.pos) if p.busy() else 0,
                "duration_ms": self._ms(p.source.frames) if p.busy() and p.source is not None else 0,
                "end_ms": self._ms(p.stop_at) if p.busy() and p.stop_at else None}

    # ── views ──
    def player_view(self, p, flow=None):
        it = p.item or {}
        dur = p.source.frames if p.source is not None else 0
        el = self._ms(p.pos) if p.busy() else 0
        d = self._ms(dur)
        rem = max(0, d - el) if p.busy() else 0
        intro, outro = int(it.get("intro_ms") or 0), int(it.get("outro_ms") or 0)
        v = {
            "player": p.n, "state": p.state, "origin": p.origin,
            "key": it.get("id"), "title": it.get("title") or "",
            "elapsed_ms": el, "remaining_ms": rem, "duration_ms": d,
            "intro_ms": intro, "outro_ms": outro,
            "intro_remaining_ms": (intro - el) if (p.busy() and intro and el < intro) else None,
            "outro_remaining_ms": (outro - el) if (p.busy() and outro and el < outro) else None,
            "loop": bool(it.get("loop")), "gain_db": round(p.gain_db, 2),
            "fade_remaining_ms": (self._ms(p.env / -p.env_step) if p.env_step < 0 else 0),
            "cue_pending": p.pending_cue,
        }
        if flow:
            v["flow"] = flow
        return v

    def players_view(self, flows=None):
        with self.lock:
            return [self.player_view(p, (flows[i] if flows else None))
                    for i, p in enumerate(self.players)]

    def flows_view(self, flows):
        """The per-flow publication (phase 2 contract, see help.md). One entry per player flow,
        idle ones included: a consumer must be able to tell "idle" from "not published"."""
        out = {}
        alert_s = self.doc.get("settings", {}).get("alert_s", 10)
        for v in self.players_view(flows):
            out[v["flow"]] = {k: v[k] for k in (
                "title", "remaining_ms", "elapsed_ms", "duration_ms", "intro_ms", "outro_ms",
                "state", "player", "intro_remaining_ms", "outro_remaining_ms")}
            out[v["flow"]]["alert_s"] = alert_s      # the consumer's countdown turns to alert colour under it
        return out

    def playlist_view(self):
        with self.lock:
            items = self._items()
            return {"on_air": self.pl_on_air, "ready": self.pl_ready,
                    "on_air_index": self._item_index(self.pl_on_air) if self.pl_on_air else None,
                    "ready_index": self._item_index(self.pl_ready) if self.pl_ready else None,
                    "count": len(items)}

    def key_options(self):
        """Live list for the macro editor (`options_endpoint`): every key of every page."""
        with self.lock:
            out = []
            for pi, pg in enumerate(self.doc["pages"]):
                pname = pg["name"] or ("%d" % (pi + 1))
                for k in pg["keys"]:
                    lbl = k["title"] or k["media"].get("file") or k["id"]
                    out.append({"value": k["id"], "label": "%s · %s" % (pname, lbl)})
            return out

    # ── document edits (all return the canonical doc) ──
    def _commit(self, d):
        self.doc = normalize_doc(d, self.n_players)
        self._index()
        self.rev += 1
        self._reset_ready()
        self.recue()
        return self.doc

    def doc_copy(self):
        with self.lock:
            return copy.deepcopy(self.doc)

    def replace_doc(self, doc):
        with self.lock:
            return copy.deepcopy(self._commit(doc))

    def _page(self, d, page_id):
        for p in d["pages"]:
            if p["id"] == page_id:
                return p
        raise EngineError("unknown_page", 404, page=page_id)

    def set_key(self, body):
        """Upsert ({page, slot, key}) or delete ({id, delete: true}) a key. Moving a key onto an
        occupied slot of the SAME page swaps the two; onto another page's occupied slot is refused
        (a key is never overwritten as a side effect)."""
        with self.lock:
            d = copy.deepcopy(self.doc)
            if body.get("delete"):
                kid = body.get("id")
                for p in d["pages"]:
                    p["keys"] = [k for k in p["keys"] if k["id"] != kid]
                return copy.deepcopy(self._commit(d))
            pg = self._page(d, body.get("page") or d["page"])
            k = dict(body.get("key") or {})
            old, old_page = None, None
            if k.get("id"):
                for p in d["pages"]:
                    for x in p["keys"]:
                        if x["id"] == k["id"]:
                            old, old_page = x, p
            merged = dict(old or {})
            merged.update(k)
            if "slot" in body:
                merged["slot"] = body["slot"]
            slot = _num(merged.get("slot", 0), 0, MAX_COLS * MAX_ROWS - 1, 0, int)
            other = next((x for x in pg["keys"] if x["slot"] == slot
                          and x["id"] != merged.get("id")), None)
            if other is not None:
                if old is not None and old_page is pg:
                    other["slot"] = old["slot"]
                else:
                    raise EngineError("slot_taken", 409, slot=slot, key=other["id"])
            if old_page is not None:
                old_page["keys"] = [x for x in old_page["keys"] if x["id"] != old["id"]]
            if not merged.get("id"):
                merged["id"] = _next_id(d, "k")
            merged["slot"] = slot
            pg["keys"].append(merged)
            doc = self._commit(d)
            return dict(copy.deepcopy(doc), key_id=merged["id"])

    def pages_op(self, body):
        with self.lock:
            d = copy.deepcopy(self.doc)
            op = body.get("op")
            if op == "add":
                if len(d["pages"]) >= MAX_PAGES:
                    raise EngineError("too_many_pages", 409)
                pid = _next_id(d, "p")
                d["pages"].append({"id": pid, "name": _text(body.get("name"), 40), "keys": []})
                d["page"] = pid
            elif op == "rename":
                self._page(d, body.get("page"))["name"] = _text(body.get("name"), 40)
            elif op == "delete":
                if len(d["pages"]) <= 1:
                    raise EngineError("last_page", 409)
                pid = body.get("page")
                self._page(d, pid)
                d["pages"] = [p for p in d["pages"] if p["id"] != pid]
            elif op == "move":
                pid = body.get("page")
                pg = self._page(d, pid)
                i = d["pages"].index(pg)
                j = max(0, min(len(d["pages"]) - 1, i + int(body.get("delta") or 0)))
                d["pages"].insert(j, d["pages"].pop(i))
            else:
                raise EngineError("bad_op", 400, op=op)
            return copy.deepcopy(self._commit(d))

    def set_page(self, page):
        """Current page: an id, a 1-based number, or "next" / "prev" (wraps)."""
        with self.lock:
            d = copy.deepcopy(self.doc)
            ids = [p["id"] for p in d["pages"]]
            cur = ids.index(d["page"]) if d["page"] in ids else 0
            if page in ("next", "prev"):
                cur = (cur + (1 if page == "next" else -1)) % len(ids)
            elif page in ids:
                cur = ids.index(page)
            else:
                try:
                    i = int(page)
                except (TypeError, ValueError):
                    raise EngineError("unknown_page", 404, page=page)
                if not 1 <= i <= len(ids):
                    raise EngineError("unknown_page", 404, page=page)
                cur = i - 1
            d["page"] = ids[cur]
            return copy.deepcopy(self._commit(d))

    def set_grid(self, body):
        with self.lock:
            d = copy.deepcopy(self.doc)
            d["grid"] = {"cols": body.get("cols", d["grid"]["cols"]),
                         "rows": body.get("rows", d["grid"]["rows"])}
            return copy.deepcopy(self._commit(d))

    def set_mode(self, mode):
        with self.lock:
            if mode not in MODES:
                raise EngineError("bad_mode", 400, mode=mode)
            d = copy.deepcopy(self.doc)
            d["mode"] = mode
            return copy.deepcopy(self._commit(d))

    def set_settings(self, body):
        with self.lock:
            d = copy.deepcopy(self.doc)
            d["settings"].update({k: v for k, v in (body or {}).items() if k in d["settings"]})
            doc = self._commit(d)
            # a new target re-gains what is ALREADY playing: the operator hears the change
            for p in self.players + [self.pfl]:
                if p.busy() and p.item:
                    p.gain_db, p.gain = self._gain_for(p.item)
                    if isinstance(p.source, MT.Montage):
                        p.source.renorm(self.norm_db_for)
            return copy.deepcopy(doc)

    def set_defaults(self, body):
        """Cart defaults ({field: value}; null = back to the factory default) or, with `page`,
        that page's defaults ({field: value}; null = back to inheriting from the cart). Every key
        that inherits follows at once; a stop fade in progress keeps the slope it started with."""
        with self.lock:
            body = body or {}
            d = copy.deepcopy(self.doc)
            vals = {f: body[f] for f in DEFAULT_FIELDS if f in body}
            pid = body.get("page")
            if pid:
                pg = self._page(d, pid)
                cur = dict(pg.get("defaults") or {})
                for f, v in vals.items():
                    if v is None:
                        cur.pop(f, None)
                    else:
                        cur[f] = v
                pg["defaults"] = cur
            else:
                for f, v in vals.items():
                    d["defaults"][f] = DEFAULT_FIELDS[f] if v is None else v
            return copy.deepcopy(self._commit(d))

    def assign_media(self, body):
        """Put a media on the key at {page, slot} — the console's drag / tap gesture. An empty slot
        gets a NEW key (every behaviour inherited); an occupied one keeps everything but its media
        and title. Returns the doc with `key_id` and `previous` (the key as it was, None for a new
        one): what the console needs to undo the gesture."""
        with self.lock:
            media = normalize_media(body.get("media"))
            if not _media_set(media):
                raise EngineError("no_media", 400)
            slot = _num(body.get("slot"), 0, MAX_COLS * MAX_ROWS - 1, None, int)
            if slot is None:
                raise EngineError("bad_slot", 400)
            d = copy.deepcopy(self.doc)
            pg = self._page(d, body.get("page") or d["page"])
            title = _text(body.get("title"), 80)
            cur = next((k for k in pg["keys"] if k["slot"] == slot), None)
            previous = copy.deepcopy(cur)
            if cur is not None:
                cur["media"] = media
                cur["edit"] = None        # In / Out / cuts of the OLD sound mean nothing on the new one
                if title:
                    cur["title"] = title
            else:
                cur = {"id": _next_id(d, "k"), "slot": slot, "media": media, "title": title}
                pg["keys"].append(cur)
            doc = self._commit(d)
            return dict(copy.deepcopy(doc), key_id=cur["id"], previous=previous)

    def pl_options(self, body):
        with self.lock:
            d = copy.deepcopy(self.doc)
            for k in ("ab", "auto", "loop", "ab_players"):
                if k in (body or {}):
                    d["playlist"][k] = body[k]
            return copy.deepcopy(self._commit(d))

    def playlist_op(self, body):
        with self.lock:
            d = copy.deepcopy(self.doc)
            items = d["playlist"]["items"]
            op = body.get("op")
            added = []

            def _idx(iid):
                for i, it in enumerate(items):
                    if it["id"] == iid:
                        return i
                raise EngineError("unknown_item", 404, item=iid)

            if op == "add":
                new = body.get("items") if isinstance(body.get("items"), list) else [body.get("item")]
                at = body.get("at")
                at = len(items) if at is None else max(0, min(len(items), int(at)))
                for it in new:
                    if len(items) >= MAX_ITEMS:
                        raise EngineError("too_many_items", 409)
                    it = dict(it or {})
                    it["id"] = _next_id(d, "i")
                    added.append(it["id"])
                    items.insert(at, it)
                    at += 1
            elif op == "update":
                it = dict(body.get("item") or {})
                i = _idx(it.get("id"))
                items[i] = dict(items[i], **it)
            elif op == "remove":
                items.pop(_idx(body.get("id")))
            elif op == "move":
                i = _idx(body.get("id"))
                j = max(0, min(len(items) - 1, i + int(body.get("delta") or 0)))
                items.insert(j, items.pop(i))
            elif op == "clear":
                del items[:]
            else:
                raise EngineError("bad_op", 400, op=op)
            doc = copy.deepcopy(self._commit(d))
            return dict(doc, added=added) if added else doc

    # ── SAVE / RECALL (0.1.3) ────────────────────────────────────────────────────────────────
    # A save is ONE of three kinds, each saved and recalled on its own (operator decision
    # 2026-10-17): the whole GRID (grid size, every page with its keys and its page defaults),
    # the PLAYLIST (items and their options), or ONE PAGE. The library itself lives in the
    # orchestrator (plugin_store, `control.recall` of the manifest); the container only captures
    # and applies. Recalling never cuts what plays: a player holds a COPY of its key, so the
    # document can change under it. What must not happen is a NEW key or item taking the id of
    # a sound still playing (a press would then act on the old sound): those ids are renamed.
    def capture(self, kind, page=None):
        """The payload of a save of `kind` ("grid" | "playlist" | "page"), with its summary."""
        with self.lock:
            d = copy.deepcopy(self.doc)
            if kind == "grid":
                payload = {"grid": d["grid"], "page": d["page"],
                           "pages": [{f: p.get(f) for f in ("id", "name", "keys", "defaults")}
                                     for p in d["pages"]]}
                medias = [k["media"] for p in d["pages"] for k in p["keys"]]
                summary = {"pages": len(d["pages"]), "keys": len(medias)}
            elif kind == "playlist":
                pl = d["playlist"]
                payload = {f: pl[f] for f in ("items", "ab", "ab_players", "auto", "loop")}
                medias = [it["media"] for it in pl["items"]]
                summary = {"items": len(medias)}
            elif kind == "page":
                pg = self._page(d, self._page_ref(d, page))
                payload = {"grid": d["grid"],
                           "page": {f: pg.get(f) for f in ("id", "name", "keys", "defaults")}}
                medias = [k["media"] for k in pg["keys"]]
                summary = {"keys": len(medias), "page_name": pg["name"]}
            else:
                raise EngineError("bad_kind", 400, kind=kind)
            summary["media"] = len([m for m in medias if _media_set(m)])
            return {"kind": kind, "payload": payload, "summary": summary}

    def _page_ref(self, d, page):
        """A page designated by id, by 1-based number, or nothing (= the page on screen)."""
        if page in (None, ""):
            return d["page"]
        ids = [p["id"] for p in d["pages"]]
        if page in ids:
            return page
        try:
            i = int(page)
        except (TypeError, ValueError):
            raise EngineError("unknown_page", 404, page=page)
        if not 1 <= i <= len(ids):
            raise EngineError("unknown_page", 404, page=page)
        return ids[i - 1]

    def _renamer(self, d, taken, origin):
        """Id assignment for recalled elements. An id stays when nothing else holds it; else a new
        one. A key (or item) whose id is held by a PLAYING player stays only if it is the same
        sound (same media): pressing it then acts on that sound, which is what the operator sees."""
        held = {p.item["id"]: normalize_media(p.item.get("media"))
                for p in self.players if p.busy() and p.item and p.origin == origin}
        all_ids = taken | set(held)

        def fresh(prefix):
            while True:
                d["seq"] += 1
                c = "%s%d" % (prefix, d["seq"])
                if c not in all_ids:
                    all_ids.add(c)
                    return c

        def assign(prefix, cur, media=None):
            cur = _text(cur, 32)
            if cur and cur not in taken and (cur not in held or (media is not None and
                                                                 held[cur] == normalize_media(media))):
                taken.add(cur)
                all_ids.add(cur)
                return cur
            c = fresh(prefix)
            taken.add(c)
            return c
        return assign

    @staticmethod
    def _remap_slots(keys, src_grid, dst_grid):
        """A page saved on a src_cols x src_rows grid, laid on dst: each key keeps its ROW and
        COLUMN when they exist; the others take the first free slot of the grid; beyond a full
        grid they are kept (as after a grid resize) and counted as hidden."""
        sc = _num((src_grid or {}).get("cols", dst_grid["cols"]), 1, MAX_COLS, dst_grid["cols"], int)
        dc_, dr = dst_grid["cols"], dst_grid["rows"]
        placed, rest, used = [], [], set()
        for k in keys:
            r, c = divmod(_num(k.get("slot", 0), 0, MAX_COLS * MAX_ROWS - 1, 0, int), sc)
            s = r * dc_ + c
            if c < dc_ and r < dr and s not in used:
                k["slot"] = s
                used.add(s)
                placed.append(k)
            else:
                rest.append(k)
        relocated = hidden = 0
        for k in rest:
            s = next((i for i in range(MAX_COLS * MAX_ROWS) if i not in used), None)
            if s is None:
                break
            used.add(s)
            k["slot"] = s
            placed.append(k)
            if s < dc_ * dr:
                relocated += 1
            else:
                hidden += 1
        return placed, relocated, hidden

    def recall(self, kind, payload, page=None, add=False):
        """Apply a save. `page` (kind "page"): the page REPLACED (id, 1-based number, or the page
        on screen); `add`: appended as a new page instead. Returns the canonical doc and a report
        {kind, page, renamed, relocated, hidden}. Nothing that plays is touched."""
        with self.lock:
            payload = payload if isinstance(payload, dict) else {}
            d = copy.deepcopy(self.doc)
            report = {"kind": kind, "renamed": 0, "relocated": 0, "hidden": 0}

            def keys_of(src_keys, assign, kmap):
                out = []
                for k in src_keys if isinstance(src_keys, list) else []:
                    if not isinstance(k, dict):
                        continue
                    k = copy.deepcopy(k)
                    nid = assign("k", k.get("id"), k.get("media"))
                    if nid != k.get("id"):
                        report["renamed"] += 1
                    kmap[k.get("id")] = nid
                    k["id"] = nid
                    out.append(k)
                return out

            def relink(keys, kmap):
                for k in keys:
                    k["next"] = kmap.get(k.get("next"), "") if k.get("next") else ""

            if kind == "grid":
                src_pages = [p for p in (payload.get("pages") or []) if isinstance(p, dict)][:MAX_PAGES]
                if not src_pages:
                    raise EngineError("empty_save", 400, kind=kind)
                taken = {it["id"] for it in d["playlist"]["items"]}
                assign = self._renamer(d, taken, "grid")
                kmap, pages = {}, []
                for p in src_pages:
                    pages.append({"id": assign("p", p.get("id")), "name": p.get("name"),
                                  "defaults": p.get("defaults"), "keys": keys_of(p.get("keys"), assign, kmap)})
                for p in pages:
                    relink(p["keys"], kmap)
                g = payload.get("grid") if isinstance(payload.get("grid"), dict) else {}
                d["grid"] = {"cols": g.get("cols", d["grid"]["cols"]), "rows": g.get("rows", d["grid"]["rows"])}
                d["pages"] = pages
                first = next((p["id"] for p, s in zip(pages, src_pages) if s.get("id") == payload.get("page")),
                             pages[0]["id"])
                d["page"] = first
                report["page"] = first
            elif kind == "playlist":
                src_items = [it for it in (payload.get("items") or []) if isinstance(it, dict)][:MAX_ITEMS]
                taken = {p["id"] for p in d["pages"]} | {k["id"] for p in d["pages"] for k in p["keys"]}
                assign = self._renamer(d, taken, "playlist")
                items = []
                for it in src_items:
                    it = copy.deepcopy(it)
                    nid = assign("i", it.get("id"), it.get("media"))
                    if nid != it.get("id"):
                        report["renamed"] += 1
                    it["id"] = nid
                    items.append(it)
                pl = d["playlist"]
                pl["items"] = items
                for f in ("ab", "ab_players", "auto", "loop"):
                    if f in payload:
                        pl[f] = payload[f]
                # The new list starts at its top: a "ready" id of the old list means nothing here.
                # What is on air goes on to its end; the next GO fades it as usual.
                self.pl_ready = None
                self.pl_on_air = None
            elif kind == "page":
                src = payload.get("page") if isinstance(payload.get("page"), dict) else None
                if src is None:
                    raise EngineError("empty_save", 400, kind=kind)
                if add:
                    if len(d["pages"]) >= MAX_PAGES:
                        raise EngineError("too_many_pages", 409)
                    target = {"id": _next_id(d, "p"), "name": "", "keys": [], "defaults": {}}
                    d["pages"].append(target)
                else:
                    target = self._page(d, self._page_ref(d, page))
                taken = {p["id"] for p in d["pages"]} | {it["id"] for it in d["playlist"]["items"]} \
                    | {k["id"] for p in d["pages"] if p is not target for k in p["keys"]}
                assign = self._renamer(d, taken, "grid")
                kmap = {}
                keys = keys_of(src.get("keys"), assign, kmap)
                relink(keys, kmap)
                keys, report["relocated"], report["hidden"] = self._remap_slots(
                    keys, payload.get("grid"), d["grid"])
                target["keys"] = keys
                target["defaults"] = src.get("defaults") or {}
                if _text(src.get("name"), 40):
                    target["name"] = src.get("name")
                d["page"] = target["id"]
                report["page"] = target["id"]
            else:
                raise EngineError("bad_kind", 400, kind=kind)
            doc = self._commit(d)
            self._event("recalled", save_kind=kind)
            return dict(copy.deepcopy(doc), recall=report)
