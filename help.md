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

Edit mode: tap a key (or an empty slot) to edit it; ← → move it. Media come from the **media
base** (shared library; a media absent from this node is fetched first, verified copy) or from a
**file sent from your computer**: the "Send a file…" button, or drop a desktop file on a key — the
key is assigned when the upload ends.

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

Values are a snapshot: a consumer polling at a few hertz interpolates between two reads while
`state` is `playing` or `fading`, and never while `paused`. Unknown fields must be ignored.

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
