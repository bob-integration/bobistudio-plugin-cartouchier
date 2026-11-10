# Cartouchier — Bobi.Studio's sound cart player

*[Version française](README.md)*

The sound carts of a live production. Submodule of
[bobi.studio](https://github.com/bob-integration/bobistudio), mounted at `plugins/cartouchier/`.

## What it does

A **grid** of carts arranged in pages, or a **playlist** with an **A/B** mode. Sound comes out
through **players**: each one is a stereo audio output on the MXL bus (`<host>_player_<n>`,
48 kHz).

It is a separate plugin, not a function of the audio console: one cart player can feed several
consoles. It is **shared** by everyone who opens it — same pages, same carts. To separate two
positions, deploy two cart players.

Everything works by finger as well as by mouse.

## Tabs, zones and players

A **tab** groups **zones**, and a zone holds a grid or a playlist. The number of players is a
setting (`players`, 2 by default). A dot lights up on a tab whose player is playing, even when the
tab is hidden.

## Audio output

All players write on the bus grid and are sample-aligned. A stopped player writes silence, so its
output never disappears between two carts. The container publishes `write_late_max_ms` and its
real-time state on `:8080`.

## EBU R128 levels

Integrated loudness, sample peak and waveform are measured when each media is loaded, then kept in
cache.

## Non-destructive editing

In, Out, fades, cuts, assembly, volume envelope and gain are **settings of the cart**: the file is
never touched, and the same sound serves several carts with different bounds. "Save as new media"
produces an edited file, leaving the original intact.

## Listening

A `GET /listen.pcm` stream in L16 48 kHz stereo lets you listen to the cart player, in the page or
in a third-party player.

## Public link

The page can be exposed read-only on a public link, one tab per link. Created with "allow
playing", the link also lets you start and stop playback, and nothing else.

## Saved sets

Grids, playlists and pages are stored in the installation's shared library, by kind. A recall
does not cut what is playing.

## Macros and external control

Thirty-one actions are exposed to macros and triggers — fire a cart, stop, change page or tab,
A/B, auto, recall a grid — plus a `param_tree` for what is adjusted continuously.

## Resources

One core, 1 GiB declared, pinned. Measured on 2026-10-30 (r620-2, two tabs, one zone, one player,
seven carts): 60 to 62 MiB.

## Licence

GNU GPL v3 or later — see `LICENSE`.
