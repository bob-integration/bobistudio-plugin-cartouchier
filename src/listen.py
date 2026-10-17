# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Browser listening: GET /listen.pcm?src=pfl|1..N — the audio console's contract, reused.

Same wire format as the console's /listen.pcm (plugins/audio_console/src/mxl_io.py: ListenHub), so
the orchestrator relays it through the same declared stream (`control.streams`, app/plugin_streams.py)
and the browser decodes it with the same ring buffer: int16 little-endian interleaved stereo 48 kHz,
one chunk per 10 ms (480 frames) from the live edge. A slow client loses its OLDEST audio and never
slows the audio thread down.

Sources: a PLAYER (what really goes to the console: the very block written to its MXL flow) or the
PRE-LISTEN (`pfl`): the cart's off-air player, which writes to no flow at all.

Audio thread side: `push(blocks, pfl)` once per 1 ms block. No client = an immediate return. With
clients: each source in use is copied into a carry buffer allocated once; only a full 10 ms chunk is
converted (one bytes object per chunk and source, shared by its clients).
"""
import collections
import threading

import numpy as np

CHUNK_FRAMES = 480
MAX_LISTENERS = 8
CLIENT_QUEUE = 100                       # 1 s per client; beyond, the oldest chunk is dropped


def to_pcm16(x):
    """float32 (n, 2) -> interleaved int16 bytes. Rounded, saturated."""
    return np.rint(np.clip(x, -1.0, 32767.0 / 32768.0) * 32768.0).astype("<i2").tobytes()


class ListenClient:
    def __init__(self, hub, src):
        self.hub, self.src = hub, src
        self.q = collections.deque()
        self.cv = threading.Condition()
        self.dropped = 0
        self.closed = False

    def _put(self, data):
        with self.cv:
            if len(self.q) >= CLIENT_QUEUE:
                self.q.popleft()
                self.dropped += 1
            self.q.append(data)
            self.cv.notify()

    def get(self, timeout=1.0):
        with self.cv:
            if not self.q and not self.closed:
                self.cv.wait(timeout)
            return self.q.popleft() if self.q else None

    def close(self):
        if not self.closed:
            self.closed = True
            self.hub._remove(self)


class ListenHub:
    def __init__(self, n_players, max_clients=MAX_LISTENERS, on_last_pfl=None):
        self.n_players = int(n_players)
        self.max_clients = max_clients
        self.on_last_pfl = on_last_pfl     # the last pre-listener left: the pre-listen stops
        self.lock = threading.Lock()
        self.clients = []
        self._carry = {}                   # src -> [buffer (CHUNK_FRAMES, 2), fill]
        self.chunks = 0

    @staticmethod
    def parse_src(v, n_players):
        """"pfl" | player number 1..N -> source key, or None."""
        v = str(v if v is not None else "pfl").strip().lower()
        if v in ("pfl", "preview", ""):
            return "pfl"
        try:
            n = int(v)
        except ValueError:
            return None
        return n if 1 <= n <= n_players else None

    def open(self, src):
        with self.lock:
            if len(self.clients) >= self.max_clients:
                return None
            c = ListenClient(self, src)
            if src not in self._carry:
                self._carry[src] = [np.zeros((CHUNK_FRAMES, 2), np.float32), 0]
            self.clients.append(c)
            return c

    def _remove(self, c):
        last_pfl = False
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)
                last_pfl = c.src == "pfl" and not any(x.src == "pfl" for x in self.clients)
        if last_pfl and self.on_last_pfl:
            try:
                self.on_last_pfl()
            except Exception:
                pass

    def count(self, src=None):
        return sum(1 for c in self.clients if src is None or c.src == src)

    def wants(self, src):
        return any(c.src == src for c in self.clients)

    def push(self, blocks, pfl=None):
        """blocks: the players' blocks (list of float32 (n, 2)); pfl: the pre-listen block or None
        (silence). Never blocks for long: a short lock, bounded queues."""
        if not self.clients:
            return
        with self.lock:
            clients = list(self.clients)
            srcs = {c.src for c in clients}
            for src in srcs:
                blk = pfl if src == "pfl" else (blocks[src - 1] if src - 1 < len(blocks) else None)
                cb = self._carry.get(src)
                if cb is None:
                    continue
                buf = cb[0]
                n = blk.shape[0] if blk is not None else (blocks[0].shape[0] if blocks else 48)
                off = 0
                while off < n:
                    take = min(n - off, CHUNK_FRAMES - cb[1])
                    if blk is None:
                        buf[cb[1]:cb[1] + take] = 0.0
                    else:
                        buf[cb[1]:cb[1] + take] = blk[off:off + take]
                    cb[1] += take
                    off += take
                    if cb[1] == CHUNK_FRAMES:
                        data = to_pcm16(buf)
                        cb[1] = 0
                        self.chunks += 1
                        for c in clients:
                            if c.src == src:
                                c._put(data)

    def stats(self):
        with self.lock:
            return {"clients": len(self.clients), "pfl": sum(1 for c in self.clients if c.src == "pfl"),
                    "max": self.max_clients, "chunks": self.chunks,
                    "dropped": sum(c.dropped for c in self.clients)}
