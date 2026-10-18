# Cart machine

Sound keys for live production. Two ways to play: a **grid** of keys arranged in **pages**, or a
**playlist** with an **A/B** mode. The sound leaves through **players**: each one is a stereo audio
output on the MXL bus (`<host>_player_<n>`), which you cable to a console.

The cart machine is a plugin of its own, not part of the audio console. Media handling stays out of
the console's real-time thread, and one cart can feed several consoles, or none. It is **shared** by
everyone who opens it: same pages, same keys. To keep two positions apart, deploy two carts.

Everything works with a finger as well as with a mouse: buttons are at least 44 px, and every drag
gesture has a two-tap equivalent.

## The screen

The **top bar** holds, from left to right:

- **Grid** / **Playlist**: the mode on screen;
- the **page tabs** (in Grid mode): tap a tab to show that page;
- **Edit**: the switch between playing (tapping a key fires it) and preparing (tapping a key opens
  it);
- **Saves**, **Settings**;
- **🎧 Listen**, with the listened source;
- **Stop all**: stops every player, each with its fade.

Below the bar, **one card per player**; then the grid or the playlist.

On a public link the page is read only: you see the players and the keys, you fire nothing, and
listening is not available.

## Players

Each player card shows: its number (P1, P2…), its state, the title, the **countdown** to the tenth
of a second, elapsed time over duration, and the **waveform** with the play head and the intro
(orange) and outro (red) markers. While they are ahead, the **INTRO** and **OUTRO** countdowns show
too. In the last seconds the countdown turns to the alert colour (threshold in Settings → Levels,
"Countdown alert (s)"; a looping sound does not alert).

Three buttons per card: ▶ **Play** (or resume), ❚❚ **Pause** / resume, ■ **Stop** (with the fade).

| State shown | Meaning |
|---|---|
| idle | nothing to play: the output carries silence |
| cued | a sound is loaded and waiting (playlist A/B) |
| playing | playing |
| paused | stopped mid-sound, resumes where it was |
| fading | stop fade in progress |

An idle player writes silence: the console strip it feeds never loses its source. Every cut, pause
or replacement is ramped over 5 ms: never a click.

### Number of players

Settings → **Players (MXL outputs)**. Two by default, eight at most. Each player is an MXL output, so
changing their number changes the container's outputs and **redeploys the cart**:

- whatever plays stops;
- when reducing, the cables of the removed players are dropped;
- the page asks for confirmation ("Redeploy now") and says how many players are playing;
- the deploy permission is required. Without it, the number is shown read only.

The cart's content (pages, keys, playlist, settings) survives the redeploy.

## Playing the grid

Outside Edit, **tapping a key fires it**. It takes a free player, or the one assigned to it. With no
free player, the key is refused and the message says so ("No free player"). A key with an assigned
player takes it even if it is playing: the current sound is replaced.

A playing key shows its time left and a progress bar. It turns to the alert colour near the end,
like the player. Under the title: the duration and small marks — ⟲ loop, → chain, the name of the
exclusive group, P*n* for an assigned player. A sound not usable yet says so on the key: "queued",
"decoding", "not on this node", "unreadable".

The grid goes up to 12 × 12 keys, on up to 64 pages.

## What a key does

- **Loop**: the sound starts over until it is stopped.
- **Chain to**: at the end, another key starts, on the same player and without a gap, unless that
  key has a player of its own.
- **Exclusive group**: firing a key stops the other keys of the same group, each with its fade.
  Empty = no group.
- **Stop fade (ms)**: the fade length when the key is stopped. 0 = a straight stop (still ramped
  over 5 ms).
- **Second press**, when you tap a key that is playing:
    - **Stop (with the fade)**;
    - **Pause / resume**;
    - **Restart from the top**.
- Tapping a key **during its fade** cuts it at once. Tapping a **paused** key resumes it.
- **End of intro** and **Start of outro** (ms from the start): the markers, with their countdowns.
- **Assigned player**: "Free player" or a given player.
- **Key gain (dB)**: added to the normalisation.
- **Colour**: red, orange, yellow, green, blue, purple, grey, or none.

## Cart and page defaults

Five settings are set **once for the whole cart**: stop fade, second press, loop, exclusive group
and assigned player (Settings → **Key defaults**). The same section holds the playlist's auto
advance.

A **page** may depart from them: Settings → **Defaults of page "…"** (the page on screen). Each field
there says "Same as the cart (value)" until you customise it.

Inheritance reads from the bottom up:

- a key takes **its own value** if it has one;
- else its **page default**;
- else the **cart default**.

In the key editor an inherited field shows "Page default (value)" or "Cart default (value)", with
**Customise**. A customised field offers **Back to default**.

Changing a default applies to every key that inherits it. The stop fade applies even to a sound
already playing; loop, group, second press and player apply at the next launch. A playlist item
inherits the cart's stop fade.

A cart older than 0.1.2 is migrated without changing anything on air: a value equal to the old
default becomes inherited, a different one stays customised.

## Playlist and A/B

In **Playlist** mode the list is numbered. The item on air shows **ON AIR**, the next one **READY**.

- **GO** puts the ready item on air and fades what was on air, with its own fade.
- ◀ **Previous** / ▶ **Next** move the ready item without touching what is on air.
- Tapping a row makes it ready.
- **A/B**: two players alternate. A plays, B holds the next item loaded; at GO they swap. It needs at
  least two players.
- **Auto advance**: the items follow one another by themselves.
- **Loop the list**: after the last item, back to the first.

In Edit, each row gets its buttons: ↑ **Move up**, ↓ **Move down**, ✎ **Edit**, 🎧 **Pre-listen**,
✂ **Edit clip…**, ✕ **Delete**. An item carries its title, gain, stop fade, intro / outro markers
and loop. The playlist holds up to 500 items.

## Preparing: Edit mode

Turn **Edit** on. Tapping a key now opens its editor (an empty slot creates a **New key**). The ← and
→ buttons of the editor move the key by one slot. **Delete** asks for a second press. Changes made in
the editor are written by **Save**.

### Assigning a sound

In Edit, the **Media** panel shows next to the grid or the playlist (below it on a narrow screen).
It is the installation's media base. Outside Edit, it goes away.

Three gestures, your choice:

- **With a mouse**: drag a media from the panel onto a key, or between two playlist rows.
- **With a finger**: long-press the media (about a third of a second), then slide without lifting
  and release over the key. A finger that moves at once scrolls the list instead of picking the
  media.
- **Tap then tap**: tap the media (it stays selected, the panel reminds you), then tap the key or
  the playlist row. **Unselect** cancels the choice.

In the playlist the sound is inserted at the tapped position; "Drop here to add at the end" appends
it. With a mouse, a selection of several media is inserted in one go.

Assignment is **immediate and saved**: no Assign or Save button. The title takes the media's name. On
a key already in use only the sound changes; the key keeps its other settings, but loses its edit
(In / Out and cuts of the old sound mean nothing on the new one).

A banner confirms the gesture and offers **Undo** for 8 seconds: the key gets its old sound back, or
the items added to the playlist are removed.

A media of the base that is not on this node is assigned at once, then copied to the node (verified
copy). The key shows "not on this node" until the copy ends, then the sound is decoded.

The older path is still there: in the key editor, **Media base…**.

### Dropping a file

A **desktop file** can be dropped straight onto a key or an empty slot with a mouse. In the key
editor, **Send a file…** does the same (handy on a tablet). The file enters the media base, and the
key is assigned when **its** upload ends. Another upload made meanwhile does not land on the wrong
key.

### Grid and pages

Settings → **Grid and pages**:

- **Columns** and **Rows** (1 to 12);
- **Name of the shown page**; a page without a name is called "Page N" after its creation number,
  which does not change when you move it;
- **Add a page**, **← Move page**, **Move page →**, **Delete the page** (second press to confirm; the
  last page cannot be deleted);
- **Save grid**.

Shrinking the grid does not delete the keys that no longer fit: a message counts them, and they come
back when you enlarge it.

## Levels — EBU R128

Each sound is measured **once, when it is loaded**: integrated loudness per ITU-R BS.1770-4, gated,
and sample peak. It is brought to the cart's common **target**, then the key's gain is added.

Settings → **Levels (EBU R128)**:

- **Normalise on load**;
- **Target (LUFS)**: −23 by default;
- **Maximum gain (dB)**: caps a boost, because raising a quiet recording raises its noise too;
- **Peak ceiling (dBFS)**: a boost must not clip;
- **Countdown alert (s)**.

A cut is never capped. Changing the target applies at once, to what is playing too. The key editor
shows the measurement — "Measured … LUFS, peak … dBFS → normalisation … dB" — and says when the
normalisation was capped by the maximum gain or by the peak ceiling.

The measurement runs in the plugin, on the samples already decoded (about 180× real time on one
core), with code checked offline on a signal of known loudness and compared with `ffmpeg ebur128`.

## Listening

The **🎧 Listen** button of the bar plays the sound in the browser. One source at a time, chosen in
the list next to it:

- **Player N**: what really goes to the console;
- **Pre-listen**: a player of the cart's own, **off air**. It writes to no MXL output: only the
  browsers listening to the pre-listen hear it.

To pre-listen a sound before firing it:

- the 🎧 of a key, in Edit;
- **🎧 Pre-listen** in the key or item editor;
- the 🎧 of a playlist row, in Edit;
- the 🎧 buttons of the clip editor.

If listening was on a player, it switches to the pre-listen; if it was off, it starts. The bar shows
"Pre-listen: *title*" and a **■ Stop pre-listen** button. A new pre-listen replaces the previous
one. It stops by itself when the last browser listening to it leaves.

The chosen source is remembered per browser. At most eight listeners are open at once on one cart.

## Clip editor

**✂ Edit clip…**, from the key editor, the item editor or a playlist row, opens the editor almost
full screen.

Editing is **non-destructive**: the file is never changed. In, Out, cuts, fades, envelope and gain
are settings of the key (its edit list), applied at play time. The same sound can thus serve several
keys with different trims. The countdown, the duration, the markers and what the console shows all
count the edited duration.

### Moving around

- **Edit** / **Source**: the edited clip (fades, envelope, markers), or the file of the chosen sound
  (In, Out, cuts).
- Tapping the waveform places the **cursor**.
- One finger pans; two fingers pinch to zoom. With a mouse, the wheel zooms.
- Buttons **−** / **+** (zoom), **All**, ◀ **Towards the start**, ▶ **Towards the end**.

### In, Out and cuts (Source view)

- **In at cursor**, **Out at cursor**; or the fields "In (ms in the file)" and "Out (ms in the file,
  0 = end)".
- **Selection start** and **Selection end** mark a passage at the cursor.
- **Cut the selection** removes the passage. A 10 ms micro-fade smooths the join.
- Each cut is listed with **🎧 Listen to the join** and ✕ to remove it.

### Fades, envelope, markers (Edit view)

- **Fade in up to the cursor**, **Fade out from the cursor**; or the fields in ms.
- Shape of each fade: **Linear**, **Equal power**, **S-curve**, **Exponential**.
- **Volume point at cursor** adds a point to the envelope. A point is dragged with a finger or the
  mouse (time and level), can also be set in ms and dB in the list, and **Delete the point**
  removes it.
- **Intro end at cursor**, **Outro start at cursor**.
- **Key gain (dB)**.

### Assembling several sounds

- **+ Append a sound**, then pick the sound in the Media panel.
- The sounds are numbered in the bar; tap a number to select it.
- For every sound after the first: **Crossfade with the previous sound (ms)** and its shape.
- **Move earlier**, **Move later**, **Remove this sound from the edit**.
- R128 normalisation applies to each sound separately.

### Listening in the editor

The 🎧 row plays the **draft**, even unsaved: **Whole clip**, **The selection**, **Around the
cursor**, and in the Source view **Around In** and **Around Out**. **Before (s)** and **After (s)**
set how much is heard around the point. ■ stops.

### Saving

- ↶ / ↷: undo or redo a change.
- **Save** writes the edit onto the key or the item.
- **Discard** closes without saving, after a confirmation in the page.
- **Save as new media…**: the edit is rendered into a **new** 24-bit WAV file of the media base, next
  to the first sound. The original is not touched. Give a name, then **Create the media**; the
  progress shows. If samples clipped, the message says so: lower the gain. Once the media exists,
  **Use the new media here** puts it on the key, in place of the edit. The new media can serve
  elsewhere: a player, another cart.

## Saves

The **Saves** button opens a panel that follows the mode on screen.

- In Grid mode: the whole **Grid** (size, every page, their keys and their page defaults), and the
  **Page on screen** alone.
- In Playlist mode: the **Playlist** (items and options A/B, auto advance, loop).

Recalling a playlist does not touch the grid, and the other way round.

For each kind:

- **Name**, then **Save … as…**. A name is unique per kind: "Morning" can be both a grid and a
  playlist. A name already taken asks for confirmation before it is replaced.
- The list shows name, date, author and a summary (pages, keys or items).
- **Recall**. For a page: **Recall here** (replaces the page on screen) or **Add as a new page**, on
  this cart or another. Each key keeps its row and column; a key that does not fit the grid takes the
  first free slot, and the report says so.
- **Export** one save, or **Export all** of a kind, to a JSON file.
- **Import a file…** reads that file back, for instance on another installation. **Replace names
  already taken** decides what happens to duplicates; the report counts saves imported, skipped and
  refused.
- **Delete**, after confirmation.

The library is **shared by the whole installation**: a save made here can be recalled from any cart.

A **recall cuts nothing**: whatever plays goes on to its end or until stopped; only the keys or the
playlist change. A confirmation shows in the page before anything is replaced.

Sounds are designated by their identity in the media base. On import, a sound unknown to the base is
reported, and the save is kept. On recall, a sound missing on the node is listed in the report; the
key is kept and marked missing.

Rights: list and export with the login alone; save, recall, import and delete with the permission to
operate plugins.

## In the audio console

Three links with the console, without leaving its page:

- **Countdown on the strip**: a strip fed by a cart player shows by itself the title, the countdown
  and the intro / outro of what is playing. It turns to the alert colour at the same threshold as
  the cart.
- **Fader start**: opening the strip's fader plays what the player holds ready (the ready item in
  A/B) or resumes a paused sound. The link is made in the strip's configuration (Strips and buses):
  **Link to a player…**, then pick the producer and the player. The strip then carries the **FS**
  chip. The console and the cart must be in a common project, and you need the editor role on that
  project. What closing the fader does is set per link:
    - **stop** (with the fade);
    - **pause**;
    - **nothing**.
- **Cart panel**: the console's **Cart machine** button in the top bar opens the cart's interface in
  a panel. It appears as soon as a cart feeds one of the console's inputs. Console settings → **Cart
  machine** lets you offer others.

## Macros and external control

Actions:

- press a key (fire it, or its second press), stop a key;
- stop all (fades), cut all (no fade);
- go to a page, next page, previous page;
- playlist: GO, next, previous, prepare an item (and fire it at once);
- A/B on or off, auto advance on or off;
- player: play / resume, pause / resume, stop;
- grid mode, playlist mode;
- recall a grid, recall a playlist, recall a page into a page (target page of your choice, or add a
  page);
- pre-listen a key, stop the pre-listen.

The lists of keys, pages, items and saves are live.

Readable states: players playing; title and time left of what is playing; mode; page shown; A/B;
item on air (number and title) and item ready; state, time left and title of players 1 and 2; state
and title of the pre-listen.

In the parameter tree: the levels (normalisation, target, maximum gain, alert threshold) and the key
defaults. Hardware surfaces and Ember+ go through these actions and states, as for any plugin.

## For integrators

### Per-flow publication — `GET /flows` (generic contract)

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
| `elapsed_ms`, `remaining_ms`, `duration_ms` | position, time left, length of the sound (edited length when the key has an edit) |
| `intro_ms`, `outro_ms` | markers, ms from the start of the sound; `0` = none |
| `intro_remaining_ms`, `outro_remaining_ms` | optional: time until the marker while it is ahead, else `null` |
| `alert_s` | optional: threshold (s) under which the countdown turns to the alert colour (producer setting) |

Values are a snapshot: a consumer polling at a few hertz interpolates between two reads while
`state` is `playing` or `fading`, and never while `paused`. Unknown fields must be ignored.

The producer DECLARES it in its manifest: `control.flow_status: {"endpoint": "/flows", "panel": true}`
(the endpoint is in `read_endpoints`; `panel`: its UI may be mounted in a consumer's panel — the
console's cart panel). The orchestrator ties flow and producer by the names of `wiring.produces`,
reads the endpoint once per producer (≈ 0.5 s cache) and serves the console
`GET /api/console-zones/<vmid>/sources`.

### Listening — `GET /listen.pcm`

A stream declared in `control.streams` (read permission), relayed as is by the orchestrator, in the
same format as the console's listening: `audio/L16;rate=48000;channels=2`, 16-bit little-endian
interleaved integers, in 10 ms chunks from the live edge. `?src=pfl` for the pre-listen,
`?src=1`…`N` for a player. At most eight clients (`503 too_many_listeners` beyond). A slow client
loses its oldest audio; it never slows the audio thread down.

### Other endpoints added since 0.1.2

| Endpoint | Role |
|---|---|
| `POST /assign` | assign a media to a key (`page`, `slot`, `media`, `title`); persisted |
| `POST /defaults` | key defaults of the cart, or of a page with `page`; read in `/state.defaults` |
| `GET /capture?kind=&page=` | capture of a grid, a playlist or a page for the library |
| `POST /recall` | recall `{kind, payload, name, page, add}`, without a cut; persisted |
| `POST /preview`, `POST /preview/stop` | pre-listen a key or an item (`key` or `item`, `start_ms`, `end_ms`, optional draft `edit` and `gain_db`); state in `/state.preview` |
| `GET /wave` | waveform window of a source file (`key`, `item` or `asset`/`file`, `from_ms`, `to_ms`, `points`) |
| `POST /montage/wave` | window of the edited timeline, with its layout |
| `POST /montage/render`, `GET /montage/job?id=` | "Save as new media": 24-bit WAV render in the background, then its progress |

The library of saves lives in the orchestrator (kinded presets, `control.recall`, kinds `grid`,
`playlist`, `page`). The new media carries its identity in its sidecar (`origin="cartouchier"`,
`derived_from`).

### Deployment settings

- **Players (stereo flows)**: 2 by default; redeploys.
- **Maximum sound length (s)**: 1200 by default; a longer media is truncated at decoding.
- **Decoded sound cache (MB)**: 4096 by default. Sounds are decoded once (48 kHz stereo PCM, about
  23 MB per minute) on the container's state volume. Beyond the cap, the least recently used sounds
  that no key references are deleted.

### Metrics (`:8080`)

`blocks_per_s` (1000 = the audio thread keeps up), `render_ms_max` (worst render of the last
second), `late_blocks` and `reanchors` (the thread fell more than 200 ms behind and re-anchored on
the TAI grid — should stay at 0), `write_errors`, `media_ready` / `media_pending` /
`media_missing`.
