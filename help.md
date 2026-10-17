# Cart machine

Sound keys for live production: a **paged grid** of keys, or a **playlist** with an **A/B** mode.
The sound leaves through **players**: one stereo MXL audio flow each (`<host>_player_<n>`, 2 by
default, set at deployment). It is a plugin of its own, not part of the audio console: media
handling stays out of the console's real-time thread, and one cart can feed several consoles — or
none. One cart is **shared** by everyone who opens it; two independent positions deploy two carts.

## Players

| State | Meaning |
|---|---|
| `idle` | free — the flow carries silence |
| `cued` | a sound is loaded and ready (playlist A/B) |
| `playing` | playing |
| `paused` | stopped mid-sound, resumes where it was |
| `fading` | stop fade in progress |

A fired key takes a **free** player, or the one **assigned** to it. No free player: the key is
refused, and says so. Every cut, pause and replacement is ramped over 5 ms: never a click.

## Keys

Each key carries: media, title, gain (dB), exclusive **group** (firing a key stops the others of
its group, with their fade), **loop**, **chain** (at the end, another key starts — on the same
player, without a gap, unless that key has a player of its own), **stop fade** (ms), **second
press** while playing (stop with the fade, pause/resume, or restart from the top), **intro** and
**outro** markers (ms from the start), assigned player, colour. Pressing a key during its fade cuts
it.

Edit mode: the **media manager** shows next to the grid (below it on a narrow screen). To assign a
sound: **drag** it onto a key (mouse; with a finger, long-press the media then slide), or **tap
the media** (it stays highlighted) **then the key**. It is immediate and saved; the title takes the
media's name. On a used key only the sound changes, its settings stay; a banner offers **Undo** for
a few seconds. A media absent from this node is assigned at once, then copied to the node (verified
copy). Tapping a key with no media selected opens it in the editor; ← → move it. A **desktop file**
can also be dropped on a key (or "Send a file…"): the key is assigned when ITS upload ends.

### Key defaults

Stop fade, second press, loop, exclusive group and assigned player are set **once for the whole
cart** (Settings → Key defaults), and per **page** if needed. A key **inherits** from its page, else
from the cart, except what it **customises**; the editor shows "Default (value)" or its own value,
with "Back to default". Changing a default applies to every key inheriting it; the stop fade even
to a sound already playing, the rest at the next launch. A playlist item inherits the cart's stop
fade. A cart older than 0.1.2 is migrated: a value equal to the old default becomes inherited, a
different one stays customised — nothing changes on air.

### Number of players

Settings → Players: each player is an MXL output. Changing their number **redeploys** the cart
(what plays stops, the cables of removed players drop): confirmed in the page, deploy permission
required.

## Playlist and A/B

**GO** puts the *ready* item on air and fades what was on air with its own fade. In **A/B**, two
players alternate: A plays, B holds the next item cued; at GO they swap. **Next / Previous** move
the ready item without touching what is on air. **Auto advance** chains the items by themselves.

## Levels — EBU R128

Each media is measured **once, when it is loaded** (ITU-R BS.1770-4 integrated loudness, gated;
sample peak) and the result is cached with the decoded sound. It is brought to the cart's common
**target** (−23 LUFS by default), then the key's own gain is added. A boost is capped by the
maximum gain (raising a quiet recording raises its noise) and by the peak ceiling (a boost must not
clip); a cut is never capped. Changing the target applies at once, to what is playing too.

Why measured in the plugin and not by ffmpeg's `ebur128`: the sound is decoded anyway, the
measurement runs on the samples in hand (≈ 180× real time on one core), and the same code is
checked offline on a signal of known loudness. Checked against `ffmpeg ebur128`.

## Display

Per player: title, **countdown** to the tenth (alert colour in the last seconds — threshold in
Settings), elapsed / duration, **waveform** with the play head and the intro / outro markers, and
the **intro** and **outro** countdowns while they are ahead. In the grid, a playing key shows its own
remaining time and progress bar.

## Per-flow publication — `GET /flows` (generic contract)

A producer that plays timed content publishes, **per MXL flow it produces**, what is playing on
it. A console (or any consumer) that knows which flow feeds a strip — and asks the orchestrator who
produces it — reads this endpoint and shows title and countdown on the strip. Nothing in the
contract is specific to this plugin: the player or the recorder can publish the same.

`GET /flows` (a `read_endpoint`, so reachable with the login alone through
`/api/containers/<vmid>/plugin/flows`) returns one entry per produced flow, **idle ones included**
(a consumer must tell "idle" from "not published"):

```json
{
  "studio-cart_player_1": {
    "title": "Jingle news",
    "state": "playing",
    "player": 1,
    "elapsed_ms": 4200,
    "remaining_ms": 7800,
    "duration_ms": 12000,
    "intro_ms": 3000,
    "outro_ms": 9500,
    "intro_remaining_ms": null,
    "outro_remaining_ms": 5300
  }
}
```

| Field | Meaning |
|---|---|
| key | the MXL flow name, exactly as in `wiring.produces` |
| `title` | what the operator reads; `""` when idle |
| `state` | `idle` · `cued` · `playing` · `paused` · `fading` |
| `player` | 1-based player number in this producer |
| `elapsed_ms`, `remaining_ms`, `duration_ms` | position, time left, length of the sound |
| `intro_ms`, `outro_ms` | markers, ms from the start of the sound; `0` = none |
| `intro_remaining_ms`, `outro_remaining_ms` | optional: time until the marker while it is ahead, else `null` |
| `alert_s` | optional: threshold (s) under which the countdown turns to the alert colour (producer setting) |

Values are a snapshot: a consumer polling at a few hertz interpolates between two reads while
`state` is `playing` or `fading`, and never while `paused`. Unknown fields must be ignored.

The producer DECLARES it in its manifest: `control.flow_status: {"endpoint": "/flows", "panel": true}`
(the endpoint is in `read_endpoints`; `panel`: its UI may be mounted in a consumer's panel — the
console's cart panel). The orchestrator ties flow and producer by the names of `wiring.produces`, reads
the endpoint once per producer (≈ 0.5 s cache) and serves the console
`GET /api/console-zones/<vmid>/sources`.

## Saves: save, recall, export, import

**Saves** button (top bar): the panel follows the mode on screen.

- **Grid**: the whole grid (size, pages, keys, page defaults). **Playlist**: its items and options (A/B, auto
  chaining, loop). **Page**: the page on screen alone — recalled *here* (replaces the page on screen) or *as a new
  page*, on this cart or another. Each key keeps its row and column; one that does not fit takes the first free
  slot (the report says so).
- **Shared library** of the whole installation: a save made here is recallable from any cart. List with date and
  author; **Export** gives a JSON file (one save, or a whole kind) that **Import** reads back on another
  installation. A name is unique per kind.
- **Recall without a cut**: whatever plays goes on to its end or until stopped; only the keys (or the playlist)
  change. A confirmation shows in the page before anything is replaced.
- **Media**: designated by their identity in the media base. On import an unknown media is reported; on recall a
  sound missing on the node is listed, the key stays, marked "missing".
- **Rights**: list and export with the login; save, delete, import and recall with `plugins.operate` — the presets'.
- **Macros**: "Recall a grid", "Recall a playlist", "Recall a page into a page" (target page of your choice, empty =
  page on screen, or add a page).

## Macros and external control

Actions: press / stop a key (live list of keys), stop all (fades) / cut all, go to a page, next /
previous page, playlist GO / next / previous / prepare an item, A/B on/off, auto advance on/off,
player play / pause / stop, grid or playlist mode. Readable states: players (state, time left,
title), title and time left of what is playing, playlist item on air and ready, page shown. Level
settings (normalisation, target, maximum gain, alert threshold) are in the parameter tree. Hardware
surfaces and Ember+ go through these actions and states, as for any plugin.

## Metrics (`:8080`)

`blocks_per_s` (1000 = the audio thread keeps up), `render_ms_max` (worst render of the last
second), `late_blocks` and `reanchors` (the thread fell more than 200 ms behind and re-anchored on
the TAI grid — should stay at 0), `write_errors`, `media_ready` / `media_pending` /
`media_missing`.
