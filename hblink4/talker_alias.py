"""
DMR Talker Alias (ETSI TS 102 361-2, 7.2.18): the name a radio sends along with
its voice call – usually "callsign name".

It arrives in two ways:

* embedded in the voice superframe: bursts B–E of a superframe carry one 72-bit
  Embedded LC; Talker Alias uses FLCO 4 (header) and 5–7 (blocks 1–3), each with
  7 bytes of alias data;
* as a separate HomeBrew ``DMRA`` packet (MMDVMHost/DMRGateway):
  ``DMRA`` + repeater id (4) + source id (3) + block type (1, 0–3) + 7 data bytes.

The header's first data byte holds format (2 bits: 0 = 7-bit, 1 = ISO-8859-1,
2 = UTF-8, 3 = UTF-16BE) and length in characters (5 bits). Up to 31 characters.
"""
from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

from .daprs import decode_embedded_lc, emb_fragment

MAX_AGE = 30.0          # forget an alias buffer this long after the last block


class TalkerAliasDecoder:
    """Collects the four alias blocks of one transmission and decodes the text."""

    def __init__(self) -> None:
        self.buf = bytearray(28)
        self.seen = set()

    def add_block(self, block: int, data7: bytes) -> Optional[str]:
        if not 0 <= block <= 3 or len(data7) < 7:
            return None
        self.buf[block * 7:block * 7 + 7] = data7[:7]
        self.seen.add(block)
        return self.text()

    def add_lc(self, lc9: bytes) -> Optional[str]:
        flco = lc9[0] & 0x3F
        if not 4 <= flco <= 7:
            return None
        return self.add_block(flco - 4, lc9[2:9])

    @property
    def size(self) -> int:
        return (self.buf[0] >> 1) & 0x1F if 0 in self.seen else 0

    def blocks_needed(self) -> int:
        """Header carries 49 alias bits, every further block 56."""
        fmt = (self.buf[0] >> 6) & 0x03
        if fmt == 0:
            bits, first = self.size * 7, 49
        else:
            bits, first = self.size * (16 if fmt == 3 else 8), 48
        return 1 if bits <= first else min(4, 1 + (bits - first + 55) // 56)

    def complete(self) -> bool:
        return 0 in self.seen and all(b in self.seen for b in range(self.blocks_needed()))

    def text(self) -> Optional[str]:
        if 0 not in self.seen:
            return None
        fmt = (self.buf[0] >> 6) & 0x03
        n = self.size
        if n == 0:
            return None
        # alias data as a bit string: last bit of the header byte, then everything after it
        bits = [(self.buf[0] & 1)]
        for b in self.buf[1:]:
            bits.extend((b >> (7 - i)) & 1 for i in range(8))
        avail = self._available_bits()
        if fmt == 0:
            chars = []
            for k in range(n):
                s = k * 7
                if s + 7 > avail:
                    break
                v = 0
                for x in bits[s:s + 7]:
                    v = (v << 1) | x
                chars.append(chr(v) if 32 <= v < 127 else " ")
            raw = "".join(chars)
        else:
            # 8/16-bit formats start at the byte after the header byte (the header's data bit is unused,
            # as in MMDVMHost CDMRTA)
            nbytes = n * (2 if fmt == 3 else 1)
            avail_bytes = (avail - 1) // 8
            octets = bytes(self.buf[1:1 + min(nbytes, avail_bytes)])
            enc = {1: "latin-1", 2: "utf-8", 3: "utf-16-be"}[fmt]
            raw = octets.decode(enc, errors="ignore")
        raw = raw.replace("\x00", "").strip()
        return raw or None

    def _available_bits(self) -> int:
        """Bits that belong to blocks already received, counted from the start of the alias text."""
        n = 1                                  # the one bit in the header byte
        for blk in range(4):
            if blk not in self.seen:
                break
            n += 48 if blk == 0 else 56
        return n


class TalkerAliasTracker:
    """Talker Alias per slot / transmission; returns the text whenever it changes."""

    def __init__(self) -> None:
        self._emb: Dict[Tuple, Tuple[bytes, list]] = {}
        self._dec: Dict[Tuple, Tuple[bytes, TalkerAliasDecoder, Optional[str], float]] = {}
        self.stats = {"embedded": 0, "dmra": 0}

    def _decoder(self, key: Tuple, stream_id: bytes) -> TalkerAliasDecoder:
        cur = self._dec.get(key)
        if cur is None or cur[0] != stream_id or time.time() - cur[3] > MAX_AGE:
            cur = (stream_id, TalkerAliasDecoder(), None, time.time())
            self._dec[key] = cur
        return cur[1]

    def _result(self, key: Tuple, text: Optional[str]) -> Optional[Tuple[str, bool]]:
        sid, dec, last, _ = self._dec[key]
        self._dec[key] = (sid, dec, text or last, time.time())
        if text and text != last:
            return text, dec.complete()
        return None

    def feed_burst(self, key: Tuple, stream_id: bytes, vseq: int, payload: bytes) -> Optional[Tuple[str, bool]]:
        """Voice burst (frame type 0) B–E. Returns (alias, complete) when the alias changed."""
        if not 1 <= vseq <= 4 or len(payload) < 19:
            return None
        frag = emb_fragment(payload)
        if vseq == 1:
            self._emb[key] = (stream_id, [frag])
            return None
        cur = self._emb.get(key)
        if cur is None or cur[0] != stream_id or len(cur[1]) != vseq - 1:
            self._emb.pop(key, None)
            return None
        cur[1].append(frag)
        if vseq < 4:
            return None
        frags = cur[1]
        del self._emb[key]
        # cheap pre-check before the full BPTC decode: FLCO 4–7 only
        word = (frags[0] << 32) | frags[1]
        flco = 0
        for c in range(2, 8):
            flco = (flco << 1) | ((word >> (63 - c * 8)) & 1)
        if not 4 <= flco <= 7:
            return None
        lc = decode_embedded_lc(tuple(frags))           # type: ignore[arg-type]
        if not lc:
            return None
        dec = self._decoder(key, stream_id)
        text = dec.add_lc(lc)
        self.stats["embedded"] += 1
        return self._result(key, text)

    def feed_dmra(self, key: Tuple, stream_id: bytes, block: int, data7: bytes) -> Optional[Tuple[str, bool]]:
        dec = self._decoder(key, stream_id)
        text = dec.add_block(block, data7)
        self.stats["dmra"] += 1
        return self._result(key, text)

    def end(self, key: Tuple) -> Optional[str]:
        self._emb.pop(key, None)
        cur = self._dec.pop(key, None)
        return cur[2] if cur else None

    def current(self, key: Tuple) -> Optional[str]:
        cur = self._dec.get(key)
        return cur[2] if cur else None
