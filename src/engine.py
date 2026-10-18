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
DOC_VERSION = 3

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


# A/B handover (0.1.7): what becomes of the item still playing on the OTHER A/B player when the
# next one is launched. "keep" (default) = it goes on playing, the operator stops it (fader start on
# fader close, mute, manual stop); "cut" = de-clicked cut now; "fade" = faded over ab_handover_ms.
AB_HANDOVER_MODES = ("keep", "cut", "fade")
AB_HANDOVER_DEFAULT = "keep"
AB_HANDOVER_MS_DEFAULT = 1000
AB_HANDOVER_MS_MAX = 30000
# Fields of the playlist that are SETTINGS (not items): set by /pl/options, carried by a save.
PL_OPTION_FIELDS = ("ab", "ab_players", "auto", "loop", "ab_handover", "ab_handover_ms")


# ─── Document ────────────────────────────────────────────────────────────────────────────────
# ─── Tabs and zones (0.1.8) ─────────────────────────────────────────────────────────────────
# The cart is a set of TABS the operator creates, names, orders and deletes. Each tab has a LAYOUT
# of 1 to 3 slots; each slot holds a ZONE. A zone is what a whole cart was up to 0.1.7: a grid
# (cols/rows, pages, keys) and a playlist (items, A/B options), its `mode` saying which one is shown
# and played, plus the PLAYERS it owns. A player belongs to at most one zone (exclusivity enforced
# by normalize_doc, in tab order: the first zone that claims it keeps it). Players of a hidden tab
# go on playing: a tab is a view, the engine never looks at which one is shown.
#
# Kind (what the layout editor offers) = mode + playlist.ab: "grid", "playlist" (one player at a
# time, the first free one of the zone), "playlist_ab" (two players of the zone, `ab_players`).
LAYOUTS = {"1": 1, "2h": 2, "2v": 2, "1+2": 3}      # 2h = side by side, 2v = stacked, 1+2 = one above two
KINDS = ("grid", "playlist", "playlist_ab")
MAX_TABS = 16
MAX_ZONES = 48


def zone_kind(z):
    if z.get("mode") != "playlist":
        return "grid"
    return "playlist_ab" if (z.get("playlist") or {}).get("ab") else "playlist"


def default_zone(zid, players=None):
    return {
        "id": zid, "name": "", "mode": "grid", "players": list(players or []),
        "grid": {"cols": 4, "rows": 3},
        "page": "",
        "pages": [],
        "playlist": {"items": [], "ab": True, "ab_players": [], "auto": False, "loop": False,
                     "ab_handover": AB_HANDOVER_DEFAULT, "ab_handover_ms": AB_HANDOVER_MS_DEFAULT},
    }


def default_doc():
    return {
        "version": DOC_VERSION,
        "tab": "t1",
        "tabs": [{"id": "t1", "name": "", "layout": "1", "zones": ["z1"]}],
        "zones": [dict(default_zone("z1"), page="p1", pages=[{"id": "p1", "name": "", "keys": []}])],
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


def doc_ids(d):
    """Every id of the document (tabs, zones, pages, keys, items): ids are unique ACROSS zones, so a
    key, a page or an item designates its zone by itself."""
    used = {t["id"] for t in d.get("tabs") or []}
    for z in d.get("zones") or []:
        used.add(z["id"])
        used |= {p["id"] for p in z["pages"]} | {k["id"] for p in z["pages"] for k in p["keys"]} \
            | {it["id"] for it in z["playlist"]["items"]}
    return used


def _next_id(d, prefix):
    """A new key / page / item / zone / tab id, never one already in the document. `seq` alone is not
    enough: a document written elsewhere (imported, hand-edited) may hold ids above it, and a reused id
    made the NEW element take the old one's place (the old one was renamed by normalize_doc)."""
    used = doc_ids(d)
    while True:
        d["seq"] += 1
        c = "%s%d" % (prefix, d["seq"])
        if c not in used:
            return c


def _norm_players(v, n_players):
    out = []
    for x in v if isinstance(v, list) else []:
        n = _num(x, 1, n_players, None, int)
        if n is not None and n not in out:
            out.append(n)
    return out


def normalize_doc(doc, n_players):
    """Canonical cart document. Never raises: an unreadable field falls back to its default, so a
    damaged persisted doc degrades instead of preventing the container from starting. Idempotent.

    MIGRATION (0.1.8). A document without `zones` (every cart up to 0.1.7: one `mode`, one grid, one
    playlist) becomes ONE tab holding ONE zone that owns EVERY player and carries that grid and that
    playlist unchanged — the same behaviour, read without a resave."""
    src = doc if isinstance(doc, dict) else {}
    # A document without a version, or of version 1, predates the defaults: migrated (see
    # normalize_item). An unreadable version is treated as the oldest, never a crash.
    legacy = _num(src.get("version", 1), 1, 10 ** 6, 1, int) < 2
    seq = _num(src.get("seq", 1), 1, 10 ** 9, 1, int)
    used = set()

    def _uid(prefix, cur):
        nonlocal seq
        cur = _text(cur, 32)
        if cur and cur not in used:
            used.add(cur)
            return cur
        while True:
            seq += 1
            c = "%s%d" % (prefix, seq)
            if c not in used:
                used.add(c)
                return c

    zsrc = src.get("zones") if isinstance(src.get("zones"), list) else None
    if not zsrc:
        # Pre-tabs document: its grid and playlist ARE the single zone, which owns every player.
        zsrc = [{"id": "z1", "name": "", "mode": src.get("mode"), "players": list(range(1, n_players + 1)),
                 "grid": src.get("grid"), "page": src.get("page"), "pages": src.get("pages"),
                 "playlist": src.get("playlist")}]
        tsrc = [{"id": "t1", "name": "", "layout": "1", "zones": ["z1"]}]
        tab_src = "t1"
    else:
        tsrc = src.get("tabs") if isinstance(src.get("tabs"), list) else []
        tab_src = src.get("tab")
    # Tab and zone ids are reserved first: a page or a key of an old document can never be named
    # "z1" or "t1" (prefixes k, p, i), but a hand-written one could.
    zones, zone_order = {}, []
    for z in zsrc[:MAX_ZONES]:
        if isinstance(z, dict):
            zid = _uid("z", z.get("id"))
            zones[zid] = z
            zone_order.append(zid)
    tabs = []
    for t in tsrc[:MAX_TABS]:
        if isinstance(t, dict):
            tabs.append((_uid("t", t.get("id")), t))
    if not tabs:
        # zones without tabs (damaged document): one tab per zone, nothing lost
        tabs = [(_uid("t", ""), {"layout": "1", "zones": [zid]}) for zid in zone_order[:MAX_TABS]] \
            or [(_uid("t", "t1"), {"layout": "1", "zones": []})]

    def _norm_zone(zid, z):
        z = z if isinstance(z, dict) else {}
        g = z.get("grid") if isinstance(z.get("grid"), dict) else {}
        out = {"id": zid, "name": _text(z.get("name"), 40),
               "mode": z.get("mode") if z.get("mode") in MODES else "grid",
               "players": _norm_players(z.get("players"), n_players),
               "grid": {"cols": _num(g.get("cols", 4), 1, MAX_COLS, 4, int),
                        "rows": _num(g.get("rows", 3), 1, MAX_ROWS, 3, int)}}
        pages = []
        for p in (z.get("pages") or [])[:MAX_PAGES] if isinstance(z.get("pages"), list) else []:
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
            pages.append({"id": _uid("p", p.get("id")), "name": _text(p.get("name"), 40),
                          "keys": keys,
                          "defaults": normalize_defaults(p.get("defaults"), n_players, partial=True)})
        if not pages:
            pages = [{"id": _uid("p", "p1"), "name": "", "keys": [], "defaults": {}}]
        out["pages"] = pages
        ids = [p["id"] for p in pages]
        out["page"] = z.get("page") if z.get("page") in ids else ids[0]
        pl = z.get("playlist") if isinstance(z.get("playlist"), dict) else {}
        items = []
        for it in (pl.get("items") or [])[:MAX_ITEMS] if isinstance(pl.get("items"), list) else []:
            ni = normalize_item(it, n_players, False, legacy)
            ni["id"] = _uid("i", ni["id"])
            items.append(ni)
        out["playlist"] = {"items": items, "ab": bool(pl.get("ab", True)),
                           "ab_players": pl.get("ab_players") if isinstance(pl.get("ab_players"), list) else [1, 2],
                           "auto": bool(pl.get("auto", False)),
                           "loop": bool(pl.get("loop", False)),
                           "ab_handover": (pl.get("ab_handover") if pl.get("ab_handover") in AB_HANDOVER_MODES
                                           else AB_HANDOVER_DEFAULT),
                           "ab_handover_ms": _num(pl.get("ab_handover_ms", AB_HANDOVER_MS_DEFAULT), 0,
                                                  AB_HANDOVER_MS_MAX, AB_HANDOVER_MS_DEFAULT, int)}
        return out

    # Tabs: each slot of the layout holds a zone; a zone sits in ONE slot. A slot without a zone (or
    # pointing at one already placed) gets a new empty grid zone; a zone that no slot holds is dropped.
    out_tabs, placed, out_zones = [], set(), []
    for tid, t in tabs:
        layout = t.get("layout") if t.get("layout") in LAYOUTS else "1"
        refs = t.get("zones") if isinstance(t.get("zones"), list) else []
        slots = []
        for i in range(LAYOUTS[layout]):
            ref = refs[i] if i < len(refs) else None
            ref = ref if isinstance(ref, str) else None
            if ref in zones and ref not in placed:
                zid = ref
                zsrc_ = zones[ref]
            else:
                zid = _uid("z", "")
                zsrc_ = {}
            placed.add(zid)
            nz = _norm_zone(zid, zsrc_)
            out_zones.append(nz)
            slots.append(zid)
        out_tabs.append({"id": tid, "name": _text(t.get("name"), 40), "layout": layout, "zones": slots})

    # Player exclusivity, in tab order: the first zone that claims a player keeps it.
    claimed = set()
    for z in out_zones:
        z["players"] = [n for n in z["players"] if n not in claimed]
        claimed |= set(z["players"])
        pl = z["playlist"]
        abp = []
        for x in pl["ab_players"]:
            n = _num(x, 1, n_players, None, int)
            if n in z["players"] and n not in abp:
                abp.append(n)
        for n in z["players"]:
            if len(abp) >= 2:
                break
            if n not in abp:
                abp.append(n)
        pl["ab_players"] = abp[:2]
        if len(pl["ab_players"]) < 2:
            pl["ab"] = False                  # A/B needs two players of the zone
    # References that point nowhere — or into ANOTHER zone — are cleared, not kept dangling: a key
    # chains on a key of its own zone, played by that zone's players.
    for z in out_zones:
        key_ids = {k["id"] for p in z["pages"] for k in p["keys"]}
        for p in z["pages"]:
            for k in p["keys"]:
                if k["next"] not in key_ids:
                    k["next"] = ""
    tab_ids = [t["id"] for t in out_tabs]
    s = src.get("settings") if isinstance(src.get("settings"), dict) else {}
    return {
        "version": DOC_VERSION,
        "tab": tab_src if tab_src in tab_ids else tab_ids[0],
        "tabs": out_tabs,
        "zones": out_zones,
        "settings": {
            "target_lufs": round(_num(s.get("target_lufs", -23.0), -40.0, -5.0, -23.0), 1),
            "normalize": bool(s.get("normalize", True)),
            "max_gain_db": round(_num(s.get("max_gain_db", 12.0), 0.0, 30.0, 12.0), 1),
            "peak_ceiling_db": round(_num(s.get("peak_ceiling_db", -1.0), -20.0, 0.0, -1.0), 1),
            "alert_s": _num(s.get("alert_s", 10), 0, 120, 10, int),
        },
        "defaults": normalize_defaults(src.get("defaults"), n_players),
        "seq": seq,
    }


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
        self.zone = None            # zone id the playing item was launched from (0.1.8)

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
        self.pl = {}                # zone id -> {"on_air": item id on air, "ready": item id the next GO plays}
        self._index()
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
        d = self.doc
        self._zones = {z["id"]: z for z in d["zones"]}
        self._key_page, self._key_zone, self._page_zone, self._item_zone = {}, {}, {}, {}
        self._player_zone = {}
        self._zone_tab = {zid: t["id"] for t in d["tabs"] for zid in t["zones"]}
        for z in d["zones"]:
            for p in z["pages"]:
                self._page_zone[p["id"]] = z["id"]
                for k in p["keys"]:
                    self._key_page[k["id"]] = p["id"]
                    self._key_zone[k["id"]] = z["id"]
            for it in z["playlist"]["items"]:
                self._item_zone[it["id"]] = z["id"]
            for n in z["players"]:
                self._player_zone[n] = z["id"]
        for zid in list(self.pl):
            if zid not in self._zones:
                del self.pl[zid]
        for zid in self._zones:
            self.pl.setdefault(zid, {"on_air": None, "ready": None})

    # ── zones ──
    def default_zone(self, kind=None, d=None):
        """The zone an order without `zone` acts on (macros and console links written before 0.1.8):
        the first zone, in tab order, whose mode is `kind` ("grid" | "playlist"), else the first zone
        of the first tab. A migrated cart has one zone: it is that one, whatever the mode."""
        d = d or self.doc
        if kind:
            for t in d["tabs"]:
                for zid in t["zones"]:
                    z = next(x for x in d["zones"] if x["id"] == zid)
                    if z["mode"] == kind:
                        return z
        first = d["tabs"][0]["zones"][0]
        return next(x for x in d["zones"] if x["id"] == first)

    def zone(self, zid=None, kind=None, d=None):
        """A zone by id; None / "" = the default zone (see default_zone). An unknown id is refused:
        acting on another zone than the one named would be a silent failure."""
        d = d or self.doc
        if zid in (None, ""):
            return self.default_zone(kind, d)
        for z in d["zones"]:
            if z["id"] == zid:
                return z
        raise EngineError("unknown_zone", 404, zone=zid)

    def zone_of_player(self, n):
        zid = self._player_zone.get(n)
        return self._zones.get(zid) if zid else None

    def tab_of_zone(self, zid):
        return self._zone_tab.get(zid)

    def _zone_players(self, z):
        return [self.players[n - 1] for n in z["players"] if 1 <= n <= self.n_players]

    def effective(self, item, origin="grid"):
        """Resolved inherited values of a key (or a player's copy of it: `_page` is set at load)
        or of a playlist item. Read from the CURRENT doc: a default changed now applies now."""
        d = self.doc
        if origin != "grid":
            return resolve(item, None, d["defaults"], ITEM_INHERITED)
        pid = item.get("_page") or self._key_page.get(item.get("id"))
        z = self._zones.get(self._page_zone.get(pid))
        pg = next((p for p in z["pages"] if p["id"] == pid), None) if z else None
        return resolve(item, pg.get("defaults") if pg else None, d["defaults"])

    def _fade_for(self, item, origin="grid"):
        return self.effective(item or {}, origin)["fade_out_ms"]

    def find_key(self, key_id):
        z = self._zones.get(self._key_zone.get(key_id))
        if z is None:
            return None
        for p in z["pages"]:
            for k in p["keys"]:
                if k["id"] == key_id:
                    return k
        return None

    def _items(self, z):
        return z["playlist"]["items"]

    def _item_index(self, z, item_id):
        for i, it in enumerate(self._items(z)):
            if it["id"] == item_id:
                return i
        return None

    def find_item(self, item_id):
        """(zone, item) of a playlist item id, or (None, None)."""
        z = self._zones.get(self._item_zone.get(item_id))
        if z is None:
            return None, None
        i = self._item_index(z, item_id)
        return (z, self._items(z)[i]) if i is not None else (None, None)

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

    # ── compatibility: the playlist pointers of the DEFAULT playlist zone (0.1.7 attributes) ──
    @property
    def pl_on_air(self):
        return self.pl[self.default_zone("playlist")["id"]]["on_air"]

    @pl_on_air.setter
    def pl_on_air(self, v):
        self.pl[self.default_zone("playlist")["id"]]["on_air"] = v

    @property
    def pl_ready(self):
        return self.pl[self.default_zone("playlist")["id"]]["ready"]

    @pl_ready.setter
    def pl_ready(self, v):
        self.pl[self.default_zone("playlist")["id"]]["ready"] = v

    # ── low-level transitions ──
    def _load(self, p, item, origin, state, zid=None):
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
        p.zone = zid or (self._key_zone if origin == "grid" else self._item_zone).get(item.get("id"))
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
        z, it = self.find_item(iid)
        if it is None:
            return
        try:
            self._load(p, it, "playlist", "cued", z["id"])
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
            src, item, zid = p.source, p.item, p.zone
            self._cut(p)
            p.item, p.origin, p.source, p.zone = item, "grid", src, zid
            p.gain_db, p.gain = self._gain_for(item)
            self._start(p, 0)
            return {"player": p.n, "action": "restart"}
        self._fade_out(p, self._fade_for(k))
        return {"player": p.n, "action": "stop"}

    def _exclusive(self, k, zid):
        """Exclusive groups act inside the key's ZONE: two zones are two independent carts."""
        g = self.effective(k)["group"]
        if not g:
            return
        for p in self.players:
            if (p.busy() and p.origin == "grid" and p.zone == zid and p.item and p.item.get("group") == g
                    and p.item["id"] != k["id"] and p.state != "fading"):
                self._fade_out(p, self._fade_for(p.item, p.origin))

    def _alloc(self, k, z):
        """A player of the key's zone: the one assigned to the key (it must belong to the zone —
        playing on another zone's player would break that zone's exclusivity), else a free one."""
        pl = self.effective(k)["player"]
        if pl:
            if pl not in z["players"] or not 1 <= pl <= self.n_players:
                self.counters["refused"] += 1
                raise EngineError("player_outside_zone", 409, key=k["id"], player=pl, zone=z["id"])
            return self.players[pl - 1]
        own = self._zone_players(z)
        for p in own:
            if not p.busy() and p.pending_cue is None:
                return p
        for p in own:
            if not p.busy():
                return p
        return None

    def _launch(self, k, on=None):
        zid = self._key_zone.get(k["id"])
        z = self._zones.get(zid)
        if z is None:
            self.counters["refused"] += 1
            raise EngineError("unknown_key", 404, key=k["id"])
        self._exclusive(k, zid)
        p = on or self._alloc(k, z)
        if p is None:
            self.counters["refused"] += 1
            raise EngineError("no_free_player", 409, key=k["id"], zone=zid)
        self._load(p, k, "grid", "playing", zid)
        self._start(p, 0)
        self.counters["fired"] += 1
        self._event("fired", player=p.n, key=k["id"], zone=zid)
        return {"player": p.n, "action": "start", "zone": zid}

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

    def stop_all(self, cut=False, zone=None):
        """Every player — or, with `zone`, the players of that zone (and whatever it launched)."""
        with self.lock:
            z = self.zone(zone) if zone else None
            n = 0
            for p in self.players:
                if z is not None and p.n not in z["players"] and p.zone != z["id"]:
                    continue
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
                    return self.pl_go(only=p, zone=p.zone)  # CE lecteur et ce qu'il tient, jamais l'autre (2026-10-18)
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

    def player_stop(self, n, cut=False, if_played=False):
        """`if_played` (fader start, 2026-10-18) : un élément chargé mais pas encore lu reste chargé — fermer le fader pour
        le relancer ne doit pas le décharger ; l'arrêt ne vaut que si la lecture a eu lieu."""
        with self.lock:
            p = self._player(n)
            if not p.busy():
                return {"player": p.n, "action": "none"}
            if if_played and p.state == "cued":
                return {"player": p.n, "action": "none", "reason": "not_played"}
            if cut or p.state in ("paused", "cued"):
                was_cued = p.state == "cued"
                self._cut(p)
                if not was_cued:
                    self._release_after_cut(p)
            else:
                self._fade_out(p, self._fade_for(p.item, p.origin))
            return {"player": p.n, "action": "stop"}

    # ── playlist (per zone) ──
    def _ab(self, z):
        pl = z["playlist"]
        if not pl["ab"] or len(pl["ab_players"]) < 2:
            return None
        return [self.players[i - 1] for i in pl["ab_players"]]

    def _reset_ready(self):
        for z in self.doc["zones"]:
            st = self.pl[z["id"]]
            items = self._items(z)
            if st["ready"] is None or self._item_index(z, st["ready"]) is None:
                st["ready"] = items[0]["id"] if items else None
            if st["on_air"] is not None and self._item_index(z, st["on_air"]) is None:
                st["on_air"] = None

    def _following(self, z, item_id):
        items = self._items(z)
        i = self._item_index(z, item_id)
        if i is None:
            return items[0]["id"] if items else None
        if i + 1 < len(items):
            return items[i + 1]["id"]
        return items[0]["id"] if (items and z["playlist"]["loop"]) else None

    def _cued_playlist_player(self, z):
        for p in self.players:
            if p.state == "cued" and p.origin == "playlist" and p.zone == z["id"]:
                return p
        return None

    def recue(self):
        """Make the player that HOLDS the next item agree with the zone's ready pointer (A/B zones
        in playlist mode only). A cued item of a zone that is no longer an A/B playlist — or no
        longer exists — is unloaded: it would hold a player that a key could not take."""
        with self.lock:
            for p in self.players:
                if p.state == "cued" and p.origin == "playlist":
                    z = self._zones.get(p.zone)
                    if z is None or not self._ab(z) or z["mode"] != "playlist" or p not in self._ab(z):
                        p.reset()
            for z in self.doc["zones"]:
                self._recue_zone(z)

    def _recue_zone(self, z):
        ab = self._ab(z)
        st = self.pl[z["id"]]
        if not ab or z["mode"] != "playlist":
            for p in self._zone_players(z):
                p.pending_cue = None
            return
        c = self._cued_playlist_player(z)
        if st["ready"] is None:
            if c is not None:
                c.reset()
            return
        idx = self._item_index(z, st["ready"])
        if c is not None and c.item and c.item["id"] == st["ready"]:
            return
        target = c
        if target is None:
            for p in ab:
                if not p.busy():
                    target = p
                    break
        if target is None:
            # both A and B busy (one on air, the other fading out, or still playing in "keep"
            # handover): cue on the one that is NOT on air, when it frees up
            for p in ab:
                if p.state == "fading" or (p.item or {}).get("id") != st["on_air"]:
                    p.pending_cue = st["ready"]
            return
        if c is not None:
            c.reset()
        try:
            self._load(target, self._items(z)[idx], "playlist", "cued", z["id"])
        except EngineError:
            target.pending_cue = st["ready"]

    def pl_go(self, only=None, zone=None):
        """GO: the ready item of the zone goes on air. What was on air: in A/B, the playlist's
        `ab_handover` (keep playing — default —, cut, or fade over ab_handover_ms, 0.1.7); outside A/B,
        its own fade. `only` (2026-10-18) : lancer CE lecteur et l'élément qu'il tient prêt — le fader
        start d'un lecteur qui tient « La Femme » ne doit pas lancer l'élément tenu par l'autre lecteur A/B."""
        with self.lock:
            z = self.zone(zone, "playlist")
            st = self.pl[z["id"]]
            items = self._items(z)
            if not items:
                raise EngineError("playlist_empty", 409, zone=z["id"])
            self._reset_ready()
            if only is not None and only.state == "cued" and only.item:
                st["ready"] = only.item["id"]
            if st["ready"] is None:
                raise EngineError("playlist_end", 409, zone=z["id"])
            ab = self._ab(z)
            on_air_players = [p for p in self.players
                              if p.origin == "playlist" and p.zone == z["id"] and p.state in ("playing", "paused")]
            target = None
            if only is not None and only.state == "cued":
                target = only
            elif ab:
                c = self._cued_playlist_player(z)
                if c is not None and c.item["id"] == st["ready"]:
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
                    if target is None:
                        # Both A/B players busy (the previous item left playing in "keep", or still
                        # fading): GO takes the one that is NOT on air — fading first — and its sound
                        # goes on as a tail faded with its own fade, the player itself free at once.
                        cands = [p for p in ab if (p.item or {}).get("id") != st["on_air"]] or list(ab)
                        cands.sort(key=lambda p: p.state != "fading")
                        target = cands[0]
                        self._free_to_tail(target, self._fade_for(target.item, target.origin))
            else:
                for p in self._zone_players(z):
                    if not p.busy():
                        target = p
                        break
            if target is None:
                self.counters["refused"] += 1
                raise EngineError("no_free_player", 409, zone=z["id"])
            if target.state != "cued":
                self._load(target, items[self._item_index(z, st["ready"])], "playlist", "cued", z["id"])
            for p in on_air_players:
                if p is not target:
                    self._handover(p, z, bool(ab))
            self._start(target, 0)
            st["on_air"] = target.item["id"]
            st["ready"] = self._following(z, st["on_air"])
            self.counters["fired"] += 1
            self._event("go", player=target.n, item=st["on_air"], zone=z["id"])
            if ab and st["ready"] is not None:
                # B holds the following item ready: now if it is free, when its fade ends if not.
                other = ab[1] if target is ab[0] else ab[0]
                # l'autre lecteur tenait un élément qui n'est plus le suivant (lancé hors ordre) : il reprend le suivant
                if other.state == "cued" and other.item and other.item["id"] != st["ready"]:
                    self._cut(other)
                other.pending_cue = st["ready"]
                if not other.busy():
                    self._apply_pending_cue(other)
            return {"player": target.n, "item": st["on_air"], "ready": st["ready"], "zone": z["id"]}

    def _handover(self, p, z, ab):
        """What becomes of `p`, on air, when another player is launched by GO. Outside A/B: its own
        fade, as always. In A/B: the zone's `ab_handover` (0.1.7)."""
        pl = z["playlist"]
        mode = pl.get("ab_handover", AB_HANDOVER_DEFAULT) if ab else None
        if mode == "keep":
            return
        if mode == "cut":
            self._fade_out(p, 0)
        elif mode == "fade":
            self._fade_out(p, pl.get("ab_handover_ms", AB_HANDOVER_MS_DEFAULT))
        else:
            self._fade_out(p, self._fade_for(p.item, p.origin))

    def _free_to_tail(self, p, fade_ms):
        """Free `p` NOW for another item: what it plays goes on as a tail faded over fade_ms (a fade
        already under way keeps its remaining time; paused or cued sounds just go)."""
        if p.state in ("playing", "fading") and p.source is not None:
            g = p.env * p.env
            if p.env_step < 0:
                n = max(1.0, p.env / -p.env_step)
            else:
                n = max(float(self._declick), self.sr * max(0, int(fade_ms)) / 1000.0)
            p.tails.append({"src": p.source, "pos": p.pos, "gain": p.gain, "env": g, "step": g / n})
        self._event("released", player=p.n, reason="reclaimed", key=(p.item or {}).get("id"))
        p.pending_cue = None
        p.reset()

    def pl_step(self, delta, zone=None):
        """Move the READY pointer (next / previous) without touching what is on air."""
        with self.lock:
            z = self.zone(zone, "playlist")
            st = self.pl[z["id"]]
            items = self._items(z)
            if not items:
                return {"ready": None, "zone": z["id"]}
            i = self._item_index(z, st["ready"])
            if i is None:
                i = -1 if delta > 0 else len(items)
            i = max(0, min(len(items) - 1, i + int(delta)))
            st["ready"] = items[i]["id"]
            for p in self._zone_players(z):
                if p.pending_cue is not None:
                    p.pending_cue = st["ready"]
            self.recue()
            return {"ready": st["ready"], "index": i, "zone": z["id"]}

    def pl_goto(self, index, play=False, zone=None):
        with self.lock:
            z = self.zone(zone, "playlist")
            items = self._items(z)
            try:
                i = int(index)
            except (TypeError, ValueError):
                raise EngineError("bad_index", 400)
            if not 0 <= i < len(items):
                raise EngineError("bad_index", 400, index=i)
            self.pl[z["id"]]["ready"] = items[i]["id"]
            self.recue()
            if play:
                return self.pl_go(zone=z["id"])
            return {"ready": items[i]["id"], "index": i, "zone": z["id"]}

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
        z = self._zones.get(p.zone)
        # An item left playing by the "keep" A/B handover (0.1.7) is no longer on air: its end must
        # not chain a GO while the item on air still plays. (on_air None = list just recalled.)
        on_air = self.pl[z["id"]]["on_air"] if z else None
        was_on_air = on_air is None or item.get("id") == on_air
        self._release(p, "end")
        # The ZONE the item was launched from chains (a zone deleted meanwhile chains nothing).
        if was_playlist and was_on_air and z is not None and z["mode"] == "playlist" and z["playlist"]["auto"]:
            try:
                self.pl_go(zone=z["id"])
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
            _z, it = self.find_item(body.get("item"))
            if it is None:
                raise EngineError("unknown_item", 404, item=body.get("item"))
            return it, "playlist"
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
            # zone = the zone that OWNS the player (status strip); from_zone = the one that launched
            # what it plays (they differ only after the layout moved a playing player)
            "zone": self._player_zone.get(p.n), "from_zone": p.zone if p.busy() else None,
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

    def playlist_view(self, zone=None):
        with self.lock:
            z = self.zone(zone, "playlist")
            st = self.pl[z["id"]]
            return {"on_air": st["on_air"], "ready": st["ready"],
                    "on_air_index": self._item_index(z, st["on_air"]) if st["on_air"] else None,
                    "ready_index": self._item_index(z, st["ready"]) if st["ready"] else None,
                    "count": len(self._items(z))}

    def zone_label(self, z, i=None):
        if z.get("name"):
            return z["name"]
        m = "".join(c for c in z["id"] if c.isdigit())
        return "Zone %s" % (m or ((i or 0) + 1))

    def key_options(self, zone=None):
        """Live list for the macro editor (`options_endpoint`): every key of every page (of `zone`)."""
        with self.lock:
            out = []
            many = len(self.doc["zones"]) > 1
            for zi, z in enumerate(self.doc["zones"]):
                if zone and z["id"] != zone:
                    continue
                for pi, pg in enumerate(z["pages"]):
                    pname = pg["name"] or ("%d" % (pi + 1))
                    if many:
                        pname = "%s · %s" % (self.zone_label(z, zi), pname)
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
        """(zone, page) of a page id, in document `d`."""
        for z in d["zones"]:
            for p in z["pages"]:
                if p["id"] == page_id:
                    return p
        raise EngineError("unknown_page", 404, page=page_id)

    def _page_zone_in(self, d, page_id):
        for z in d["zones"]:
            if any(p["id"] == page_id for p in z["pages"]):
                return z
        raise EngineError("unknown_page", 404, page=page_id)

    def _target_page(self, d, body):
        """The page an edit addresses: `page` (an id: it names its zone), else the page on screen of
        `zone` (default: the default grid zone)."""
        if body.get("page"):
            return self._page_zone_in(d, body["page"]), self._page(d, body["page"])
        z = self.zone(body.get("zone"), "grid", d)
        return z, next(p for p in z["pages"] if p["id"] == z["page"])

    def set_key(self, body):
        """Upsert ({page, slot, key}) or delete ({id, delete: true}) a key. Moving a key onto an
        occupied slot of the SAME page swaps the two; onto another page's occupied slot is refused
        (a key is never overwritten as a side effect)."""
        with self.lock:
            d = copy.deepcopy(self.doc)
            if body.get("delete"):
                kid = body.get("id")
                for z in d["zones"]:
                    for p in z["pages"]:
                        p["keys"] = [k for k in p["keys"] if k["id"] != kid]
                return copy.deepcopy(self._commit(d))
            _z, pg = self._target_page(d, body)
            k = dict(body.get("key") or {})
            old, old_page = None, None
            if k.get("id"):
                for z in d["zones"]:
                    for p in z["pages"]:
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
                z = self.zone(body.get("zone"), "grid", d)
                if len(z["pages"]) >= MAX_PAGES:
                    raise EngineError("too_many_pages", 409)
                pid = _next_id(d, "p")
                z["pages"].append({"id": pid, "name": _text(body.get("name"), 40), "keys": []})
                z["page"] = pid
            elif op == "rename":
                self._page(d, body.get("page"))["name"] = _text(body.get("name"), 40)
            elif op == "delete":
                pid = body.get("page")
                z = self._page_zone_in(d, pid)
                if len(z["pages"]) <= 1:
                    raise EngineError("last_page", 409)
                z["pages"] = [p for p in z["pages"] if p["id"] != pid]
            elif op == "move":
                pid = body.get("page")
                z = self._page_zone_in(d, pid)
                pg = self._page(d, pid)
                i = z["pages"].index(pg)
                j = max(0, min(len(z["pages"]) - 1, i + int(body.get("delta") or 0)))
                z["pages"].insert(j, z["pages"].pop(i))
            else:
                raise EngineError("bad_op", 400, op=op)
            return copy.deepcopy(self._commit(d))

    def set_page(self, page, zone=None):
        """Current page of a zone: an id (it names its zone), a 1-based number, or "next" / "prev"
        (wraps) — of `zone`, default: the default grid zone."""
        with self.lock:
            d = copy.deepcopy(self.doc)
            if isinstance(page, str) and page not in ("next", "prev") and not page.isdigit():
                z = self._page_zone_in(d, page)
            else:
                z = self.zone(zone, "grid", d)
            ids = [p["id"] for p in z["pages"]]
            cur = ids.index(z["page"]) if z["page"] in ids else 0
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
            z["page"] = ids[cur]
            self._commit(d)
            return {"page": ids[cur], "zone": z["id"]}

    def set_grid(self, body):
        with self.lock:
            d = copy.deepcopy(self.doc)
            z = self.zone(body.get("zone"), "grid", d)
            z["grid"] = {"cols": body.get("cols", z["grid"]["cols"]),
                         "rows": body.get("rows", z["grid"]["rows"])}
            return copy.deepcopy(self._commit(d))

    def set_mode(self, mode, zone=None):
        with self.lock:
            if mode not in MODES:
                raise EngineError("bad_mode", 400, mode=mode)
            d = copy.deepcopy(self.doc)
            self.zone(zone, None, d)["mode"] = mode
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
            _z, pg = self._target_page(d, body)
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
            z = self.zone((body or {}).get("zone"), "playlist", d)
            for k in PL_OPTION_FIELDS:
                if k in (body or {}):
                    z["playlist"][k] = body[k]
            return copy.deepcopy(self._commit(d))

    def playlist_op(self, body):
        with self.lock:
            d = copy.deepcopy(self.doc)
            op = body.get("op")
            if op in ("update", "remove", "move"):
                iid = (body.get("item") or {}).get("id") if op == "update" else body.get("id")
                z = next((x for x in d["zones"] if any(it["id"] == iid for it in x["playlist"]["items"])), None)
                if z is None:
                    raise EngineError("unknown_item", 404, item=iid)
            else:
                z = self.zone(body.get("zone"), "playlist", d)
            items = z["playlist"]["items"]
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

    # ── TABS and ZONES (0.1.8) ───────────────────────────────────────────────────────────────
    def _tab(self, d, tab):
        """A tab by id, 1-based number, or "next" / "prev" relative to the shown one."""
        ids = [t["id"] for t in d["tabs"]]
        if tab in ids:
            return d["tabs"][ids.index(tab)]
        cur = ids.index(d["tab"]) if d["tab"] in ids else 0
        if tab in ("next", "prev"):
            return d["tabs"][(cur + (1 if tab == "next" else -1)) % len(ids)]
        try:
            i = int(tab)
        except (TypeError, ValueError):
            raise EngineError("unknown_tab", 404, tab=tab)
        if not 1 <= i <= len(ids):
            raise EngineError("unknown_tab", 404, tab=tab)
        return d["tabs"][i - 1]

    def set_tab(self, tab):
        """The tab shown on the consoles (shared, like the page: a macro can switch it). Nothing that
        plays is touched — the players of the tab left keep playing."""
        with self.lock:
            d = copy.deepcopy(self.doc)
            d["tab"] = self._tab(d, tab)["id"]
            self._commit(d)
            return {"tab": d["tab"]}

    def tabs_op(self, body):
        """{op: add | rename | delete | move, tab, name, delta}. A new tab has one empty grid zone
        (no player: the layout editor gives it some). Deleting a tab deletes its zones and frees
        their players; what they play goes on to its end."""
        with self.lock:
            d = copy.deepcopy(self.doc)
            op = body.get("op")
            if op == "add":
                if len(d["tabs"]) >= MAX_TABS:
                    raise EngineError("too_many_tabs", 409)
                if len(d["zones"]) >= MAX_ZONES:
                    raise EngineError("too_many_zones", 409)
                tid, zid = _next_id(d, "t"), _next_id(d, "z")
                d["zones"].append(default_zone(zid))
                d["tabs"].append({"id": tid, "name": _text(body.get("name"), 40), "layout": "1", "zones": [zid]})
                d["tab"] = tid
            elif op == "rename":
                self._tab(d, body.get("tab"))["name"] = _text(body.get("name"), 40)
            elif op == "delete":
                if len(d["tabs"]) <= 1:
                    raise EngineError("last_tab", 409)
                t = self._tab(d, body.get("tab"))
                d["tabs"] = [x for x in d["tabs"] if x is not t]
                d["zones"] = [z for z in d["zones"] if z["id"] not in t["zones"]]
            elif op == "move":
                t = self._tab(d, body.get("tab"))
                i = d["tabs"].index(t)
                j = max(0, min(len(d["tabs"]) - 1, i + int(body.get("delta") or 0)))
                d["tabs"].insert(j, d["tabs"].pop(i))
            else:
                raise EngineError("bad_op", 400, op=op)
            return copy.deepcopy(self._commit(d))

    def set_layout(self, body):
        """The tab's layout editor: {tab, layout, slots: [{zone?, name, kind, players}]}, one entry
        per slot of the layout. `zone` = an existing zone KEPT with its grid and playlist (moved here
        if it sat elsewhere); absent = a new zone. Refused, never half-applied:
          player_taken          a player already belongs to a zone of ANOTHER tab (free it there first);
          player_twice          the same player in two slots of this tab;
          ab_needs_two_players  a playlist A/B zone with fewer than two players;
          bad_layout / bad_kind / too_many_zones.
        The zones the tab held that no slot keeps are deleted (their players freed; what plays goes
        on to its end)."""
        with self.lock:
            d = copy.deepcopy(self.doc)
            t = self._tab(d, body.get("tab") or d["tab"])
            layout = body.get("layout")
            if layout not in LAYOUTS:
                raise EngineError("bad_layout", 400, layout=layout)
            slots = body.get("slots") if isinstance(body.get("slots"), list) else []
            if len(slots) != LAYOUTS[layout]:
                raise EngineError("bad_layout", 400, layout=layout, slots=len(slots))
            zmap = {z["id"]: z for z in d["zones"]}
            others = {}                                   # player -> zone of ANOTHER tab
            kept = {s.get("zone") for s in slots if isinstance(s, dict) and s.get("zone")}
            for tt in d["tabs"]:
                if tt is t:
                    continue
                for zid in tt["zones"]:
                    if zid in kept:
                        continue                          # moved into this tab: its players come along
                    for n in zmap[zid]["players"]:
                        others[n] = zid
            seen, new_zones = set(), []
            for s in slots:
                s = s if isinstance(s, dict) else {}
                kind = s.get("kind", "grid")
                if kind not in KINDS:
                    raise EngineError("bad_kind", 400, kind=kind)
                players = _norm_players(s.get("players"), self.n_players)
                for n in players:
                    if n in seen:
                        raise EngineError("player_twice", 409, player=n)
                    if n in others:
                        raise EngineError("player_taken", 409, player=n, zone=others[n],
                                          zone_name=self.zone_label(zmap[others[n]]))
                    seen.add(n)
                if kind == "playlist_ab" and len(players) < 2:
                    raise EngineError("ab_needs_two_players", 409)
                zid = s.get("zone")
                if zid and zid in zmap and zid not in [z["id"] for z in new_zones]:
                    z = zmap[zid]
                else:
                    z = default_zone(_next_id(d, "z"))
                    d["zones"].append(z)
                    zmap[z["id"]] = z
                z["name"] = _text(s.get("name", z.get("name")), 40)
                z["players"] = players
                z["mode"] = "grid" if kind == "grid" else "playlist"
                if kind != "grid":
                    z["playlist"]["ab"] = kind == "playlist_ab"
                    if kind == "playlist_ab":
                        keep = [n for n in z["playlist"].get("ab_players") or [] if n in players]
                        z["playlist"]["ab_players"] = (keep + [n for n in players if n not in keep])[:2]
                new_zones.append(z)
            new_ids = [z["id"] for z in new_zones]
            dropped = [zid for zid in t["zones"] if zid not in new_ids]
            for tt in d["tabs"]:                          # a zone moved here leaves its old slot
                if tt is not t:
                    tt["zones"] = [("" if zid in new_ids else zid) for zid in tt["zones"]]
            t["layout"] = layout
            t["zones"] = new_ids
            d["zones"] = [z for z in d["zones"] if z["id"] not in dropped]
            if len(d["zones"]) > MAX_ZONES:
                raise EngineError("too_many_zones", 409)
            return copy.deepcopy(self._commit(d))

    def set_zone(self, body):
        """{zone, name?, kind?}: rename a zone or change its kind (the content of the other kind is
        kept, as the mode switch always did). Players are given by the layout editor (set_layout)."""
        with self.lock:
            d = copy.deepcopy(self.doc)
            z = self.zone(body.get("zone"), None, d)
            if "name" in body:
                z["name"] = _text(body.get("name"), 40)
            if "kind" in body:
                kind = body.get("kind")
                if kind not in KINDS:
                    raise EngineError("bad_kind", 400, kind=kind)
                if kind == "playlist_ab" and len(z["players"]) < 2:
                    raise EngineError("ab_needs_two_players", 409)
                z["mode"] = "grid" if kind == "grid" else "playlist"
                if kind != "grid":
                    z["playlist"]["ab"] = kind == "playlist_ab"
            return copy.deepcopy(self._commit(d))

    def set_path(self, path, value):
        """Macro parameter tree by id (`param_tree.by_id`): `zones.<zone id>.playlist.<option>` — the
        playlist options of ONE zone, whatever its place. Nothing else is writable this way."""
        parts = str(path or "").split(".")
        if len(parts) != 4 or parts[0] != "zones" or parts[2] != "playlist" or parts[3] not in PL_OPTION_FIELDS:
            raise EngineError("bad_path", 400, path=path)
        return self.pl_options({"zone": parts[1], parts[3]: value})

    def zones_view(self):
        """The tabs and zones as /state publishes them: cheap (no media), every poll."""
        with self.lock:
            d = self.doc
            busy = {}
            for p in self.players:
                if p.state in ("playing", "fading"):
                    for zid in {self._player_zone.get(p.n), p.zone}:
                        if zid:
                            busy[zid] = True
            zones = []
            for i, z in enumerate(d["zones"]):
                ids = [p["id"] for p in z["pages"]]
                pi = ids.index(z["page"]) if z["page"] in ids else 0
                v = {"id": z["id"], "name": z["name"], "label": self.zone_label(z, i), "kind": zone_kind(z),
                     "mode": z["mode"], "players": list(z["players"]), "tab": self._zone_tab.get(z["id"]),
                     "page": z["page"], "page_index": pi + 1, "pages_count": len(ids),
                     "playing": bool(busy.get(z["id"]))}
                pl = z["playlist"]
                v["playlist"] = dict(self.playlist_view(z["id"]), ab=pl["ab"], auto=pl["auto"], loop=pl["loop"],
                                     ab_players=pl["ab_players"], ab_handover=pl["ab_handover"],
                                     ab_handover_ms=pl["ab_handover_ms"])
                zones.append(v)
            tabs = [{"id": t["id"], "name": t["name"], "layout": t["layout"], "zones": list(t["zones"]),
                     "playing": any(busy.get(zid) for zid in t["zones"])} for t in d["tabs"]]
            ids = [t["id"] for t in d["tabs"]]
            return {"tab": d["tab"], "tab_index": (ids.index(d["tab"]) + 1) if d["tab"] in ids else 1,
                    "tabs": tabs, "zones": zones}

    # ── SAVE / RECALL (0.1.3) ────────────────────────────────────────────────────────────────
    # A save is ONE of three kinds, each saved and recalled on its own (operator decision
    # 2026-10-17): the whole GRID (grid size, every page with its keys and its page defaults),
    # the PLAYLIST (items and their options), or ONE PAGE. Since 0.1.8 each acts on ONE ZONE (`zone`,
    # or the zone of `page`; default: the default zone of that kind). The library itself lives in the
    # orchestrator (plugin_store, `control.recall` of the manifest); the container only captures
    # and applies. Recalling never cuts what plays: a player holds a COPY of its key, so the
    # document can change under it. What must not happen is a NEW key or item taking the id of
    # a sound still playing (a press would then act on the old sound): those ids are renamed.
    def _save_zone(self, d, kind, page=None, zone=None):
        if zone:
            return self.zone(zone, None, d)
        if kind in ("grid", "page") and page not in (None, "") and not str(page).isdigit():
            return self._page_zone_in(d, page)
        return self.zone(None, "playlist" if kind == "playlist" else "grid", d)

    def capture(self, kind, page=None, zone=None):
        """The payload of a save of `kind` ("grid" | "playlist" | "page"), with its summary."""
        with self.lock:
            d = copy.deepcopy(self.doc)
            if kind not in ("grid", "playlist", "page"):
                raise EngineError("bad_kind", 400, kind=kind)
            z = self._save_zone(d, kind, page, zone)
            if kind == "grid":
                payload = {"grid": z["grid"], "page": z["page"],
                           "pages": [{f: p.get(f) for f in ("id", "name", "keys", "defaults")}
                                     for p in z["pages"]]}
                medias = [k["media"] for p in z["pages"] for k in p["keys"]]
                summary = {"pages": len(z["pages"]), "keys": len(medias)}
            elif kind == "playlist":
                pl = z["playlist"]
                payload = {f: pl[f] for f in ("items",) + PL_OPTION_FIELDS}
                medias = [it["media"] for it in pl["items"]]
                summary = {"items": len(medias)}
            else:
                pg = self._page(d, self._page_ref(z, page))
                payload = {"grid": z["grid"],
                           "page": {f: pg.get(f) for f in ("id", "name", "keys", "defaults")}}
                medias = [k["media"] for k in pg["keys"]]
                summary = {"keys": len(medias), "page_name": pg["name"]}
            summary["media"] = len([m for m in medias if _media_set(m)])
            summary["zone"] = z["id"]
            return {"kind": kind, "payload": payload, "summary": summary}

    def _page_ref(self, z, page):
        """A page of zone `z` designated by id, by 1-based number, or nothing (= its page on screen)."""
        if page in (None, ""):
            return z["page"]
        ids = [p["id"] for p in z["pages"]]
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

    def recall(self, kind, payload, page=None, add=False, zone=None):
        """Apply a save to ONE zone (`zone`, or the zone of `page`, or the default zone of the kind).
        `page` (kind "page"): the page REPLACED (id, 1-based number, or the zone's page on screen);
        `add`: appended as a new page instead. Returns the canonical doc and a report
        {kind, zone, page, renamed, relocated, hidden}. Nothing that plays is touched."""
        with self.lock:
            payload = payload if isinstance(payload, dict) else {}
            d = copy.deepcopy(self.doc)
            if kind not in ("grid", "playlist", "page"):
                raise EngineError("bad_kind", 400, kind=kind)
            z = self._save_zone(d, kind, page, zone)
            report = {"kind": kind, "zone": z["id"], "renamed": 0, "relocated": 0, "hidden": 0}

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

            def ids_outside(exclude):
                """Every id of the document but those of `exclude` (what the recall replaces)."""
                zz = copy.deepcopy(d)
                exclude(next(x for x in zz["zones"] if x["id"] == z["id"]))
                return doc_ids(zz)

            if kind == "grid":
                src_pages = [p for p in (payload.get("pages") or []) if isinstance(p, dict)][:MAX_PAGES]
                if not src_pages:
                    raise EngineError("empty_save", 400, kind=kind)
                taken = ids_outside(lambda x: x.update(pages=[]))
                assign = self._renamer(d, taken, "grid")
                kmap, pages = {}, []
                for p in src_pages:
                    pages.append({"id": assign("p", p.get("id")), "name": p.get("name"),
                                  "defaults": p.get("defaults"), "keys": keys_of(p.get("keys"), assign, kmap)})
                for p in pages:
                    relink(p["keys"], kmap)
                g = payload.get("grid") if isinstance(payload.get("grid"), dict) else {}
                z["grid"] = {"cols": g.get("cols", z["grid"]["cols"]), "rows": g.get("rows", z["grid"]["rows"])}
                z["pages"] = pages
                first = next((p["id"] for p, s in zip(pages, src_pages) if s.get("id") == payload.get("page")),
                             pages[0]["id"])
                z["page"] = first
                report["page"] = first
            elif kind == "playlist":
                src_items = [it for it in (payload.get("items") or []) if isinstance(it, dict)][:MAX_ITEMS]
                taken = ids_outside(lambda x: x["playlist"].update(items=[]))
                assign = self._renamer(d, taken, "playlist")
                items = []
                for it in src_items:
                    it = copy.deepcopy(it)
                    nid = assign("i", it.get("id"), it.get("media"))
                    if nid != it.get("id"):
                        report["renamed"] += 1
                    it["id"] = nid
                    items.append(it)
                pl = z["playlist"]
                pl["items"] = items
                for f in PL_OPTION_FIELDS:
                    if f in payload:
                        pl[f] = payload[f]
                # The new list starts at its top: a "ready" id of the old list means nothing here.
                # What is on air goes on to its end; the next GO fades it as usual.
                self.pl[z["id"]] = {"on_air": None, "ready": None}
            else:
                src = payload.get("page") if isinstance(payload.get("page"), dict) else None
                if src is None:
                    raise EngineError("empty_save", 400, kind=kind)
                if add:
                    if len(z["pages"]) >= MAX_PAGES:
                        raise EngineError("too_many_pages", 409)
                    target = {"id": _next_id(d, "p"), "name": "", "keys": [], "defaults": {}}
                    z["pages"].append(target)
                else:
                    target = self._page(d, self._page_ref(z, page))
                tid = target["id"]
                taken = ids_outside(lambda x: [p.update(keys=[]) for p in x["pages"] if p["id"] == tid])
                assign = self._renamer(d, taken, "grid")
                kmap = {}
                keys = keys_of(src.get("keys"), assign, kmap)
                relink(keys, kmap)
                keys, report["relocated"], report["hidden"] = self._remap_slots(
                    keys, payload.get("grid"), z["grid"])
                target["keys"] = keys
                target["defaults"] = src.get("defaults") or {}
                if _text(src.get("name"), 40):
                    target["name"] = src.get("name")
                z["page"] = target["id"]
                report["page"] = target["id"]
            doc = self._commit(d)
            self._event("recalled", save_kind=kind, zone=z["id"])
            return dict(copy.deepcopy(doc), recall=report)
