"""
OpenBridge framing compatible with FreeDMR (and FreeDMR-based servers such as
FDMR+, FDMR-Secure-Server, ADN-DMR-Peer-Server).

FreeDMR speaks classic OpenBridge (DMRD + HMAC-SHA1, "version 1") and an
extended protocol that is switched on per stanza with ``ENHANCED_OBP: True`` and
``PROTO_VER: 2..5``:

* **DMRE** data frames (version 4 and 5)::

      v5:  DMRE | seq(1) src(3) dst(3) | server_id(4) | bits(1) stream(4) dmr(33)   (53 bytes)
           | ber(1) rssi(1) ver(1) | timestamp_ns(8) | source_server(4) | source_rptr(4) | hops(1)
           | BLAKE2b-128(key, bytes[0:73])                                          = 89 bytes
      v4:  as v5 without source_rptr                                                = 85 bytes

  Bytes 11:15 carry the *sending server's* ID. The receiver drops the frame unless
  it equals the NETWORK_ID of its stanza, so on our side this is ``network_id``.

* **Bridge control** (all signed with HMAC-SHA1):

  - ``BCKA`` keep-alive, every 10 s. A FreeDMR server with ENHANCED_OBP sends
    *no traffic at all* to a trunk whose last BCKA is older than 60 s.
  - ``BCVE`` protocol version, every 60 s (HMAC over the version byte only).
    The far end upgrades to the highest version it hears.
  - ``BCSQ`` source quench: "stop sending stream X on TG Y to me".
  - ``BCST`` stun (ignored here).

* With PROTO_VER > 1 FreeDMR rejects classic DMRD frames.

* The key is the passphrase NUL-padded/truncated to 20 bytes (FreeDMR config.py).
  For HMAC-SHA1 that makes no difference up to 20 characters; for BLAKE2b the key
  length is part of the hash, so the padding is essential.
"""
from __future__ import annotations

from hashlib import blake2b, sha1
from hmac import compare_digest, new as hmac_new
from time import time_ns
from typing import Optional, NamedTuple

DMRD = b'DMRD'
DMRE = b'DMRE'
DMRF = b'DMRF'
BCKA = b'BCKA'
BCSQ = b'BCSQ'
BCST = b'BCST'
BCVE = b'BCVE'

OUR_VERSION = 5          # highest protocol version we speak (same as FreeDMR)
MAX_HOPS = 10
KEEPALIVE_INTERVAL = 10  # seconds (FreeDMR: BCKA every 10 s)
VERSION_INTERVAL = 60    # seconds (FreeDMR: BCVE every 60 s)
KEEPALIVE_TIMEOUT = 60   # FreeDMR stops sending after 60 s without BCKA


def key(passphrase) -> bytes:
    """FreeDMR key: passphrase NUL-padded / truncated to 20 bytes."""
    if isinstance(passphrase, bytes):
        passphrase = passphrase.decode('utf-8', 'replace')
    return bytes(passphrase.ljust(20, '\x00')[:20], 'utf-8')


def _hmac(k: bytes, data: bytes) -> bytes:
    return hmac_new(k, data, sha1).digest()


def _blake(k: bytes, data: bytes) -> bytes:
    h = blake2b(key=k, digest_size=16)
    h.update(data)
    return h.digest()


# ------------------------------------------------------------------ building
def build_dmrd(k: bytes, dmrd53: bytes) -> bytes:
    """Classic OBP: 53-byte DMRD + HMAC-SHA1 = 73 bytes."""
    return dmrd53 + _hmac(k, dmrd53)


def build_dmre(k: bytes, dmrd53: bytes, version: int = 5, *, source_server: int, source_rptr: int = 0,
               hops: int = 1, ber: int = 0, rssi: int = 0, ts_ns: Optional[int] = None) -> bytes:
    """DMRE frame (v4 = 85 bytes, v5 = 89 bytes). dmrd53[11:15] must already hold our server ID."""
    body = [DMRE, dmrd53[4:53], bytes([ber & 0xFF, rssi & 0xFF, 5 if version >= 5 else 4]),
            (ts_ns if ts_ns is not None else time_ns()).to_bytes(8, 'big'),
            int(source_server).to_bytes(4, 'big')]
    if version >= 5:
        body.append(int(source_rptr).to_bytes(4, 'big'))
    body.append(bytes([max(1, min(255, hops))]))
    pkt = b''.join(body)
    return pkt + _blake(k, pkt)


def build_bcka(k: bytes) -> bytes:
    return BCKA + _hmac(k, BCKA)


def build_bcve(k: bytes, version: int = OUR_VERSION) -> bytes:
    v = bytes([version & 0xFF])
    return BCVE + v + _hmac(k, v)


def build_bcsq(k: bytes, tgid: bytes, stream_id: bytes) -> bytes:
    pkt = BCSQ + tgid[:3] + stream_id[:4]
    return pkt + _hmac(k, pkt)


# ------------------------------------------------------------------ parsing
class Frame(NamedTuple):
    kind: str                     # 'data' | 'bcka' | 'bcve' | 'bcsq' | 'bcst'
    ok: bool                      # authentication passed
    dmrd: bytes = b''             # 53-byte DMRD (DMRE/DMRF rewritten to DMRD)
    version: int = 1              # protocol version of a data frame / BCVE
    timestamp_ns: int = 0
    source_server: int = 0
    source_rptr: int = 0
    hops: int = 0
    tgid: bytes = b''             # BCSQ
    stream_id: bytes = b''        # BCSQ
    error: str = ''


def parse(k: bytes, packet: bytes) -> Optional[Frame]:
    """Authenticate and decode one OBP datagram. None = not an OBP packet we know."""
    op = packet[:4]
    if op == DMRD:
        if len(packet) < 73:
            return Frame('data', False, error=f'DMRD too short ({len(packet)} bytes)')
        d = packet[:53]
        return Frame('data', compare_digest(packet[53:73], _hmac(k, d)), dmrd=d, version=1,
                     error='' if len(packet) == 73 else f'DMRD length {len(packet)}')
    if op == DMRE:
        if len(packet) < 85:
            return Frame('data', False, error=f'DMRE too short ({len(packet)} bytes)')
        ver = packet[55]
        if ver > 4:
            if len(packet) < 89:
                return Frame('data', False, error=f'DMRE v{ver} too short ({len(packet)} bytes)')
            signed, digest = packet[:73], packet[73:89]
            rptr, hops = int.from_bytes(packet[68:72], 'big'), packet[72]
        else:
            signed, digest = packet[:69], packet[69:85]
            rptr, hops = 0, packet[68]
        return Frame('data', compare_digest(digest, _blake(k, signed)), dmrd=DMRD + packet[4:53], version=ver,
                     timestamp_ns=int.from_bytes(packet[56:64], 'big'),
                     source_server=int.from_bytes(packet[64:68], 'big'), source_rptr=rptr, hops=hops)
    if op == DMRF:
        # versions 2/3 (old FreeDMR). v2 signs 61 bytes, v3 signs the 53-byte DMRF only.
        if len(packet) < 78:
            return Frame('data', False, error=f'DMRF too short ({len(packet)} bytes)')
        digest = packet[62:78]
        ok = compare_digest(digest, _blake(k, packet[:61])) or compare_digest(digest, _blake(k, packet[:53]))
        return Frame('data', ok, dmrd=DMRD + packet[4:53], version=2,
                     timestamp_ns=int.from_bytes(packet[53:61], 'big'), hops=packet[61])
    if op == BCKA:
        return Frame('bcka', compare_digest(packet[4:24], _hmac(k, BCKA)))
    if op == BCVE:
        if len(packet) < 25:
            return Frame('bcve', False, error='BCVE too short')
        return Frame('bcve', compare_digest(packet[5:25], _hmac(k, packet[4:5])), version=packet[4])
    if op == BCSQ:
        if len(packet) < 31:
            return Frame('bcsq', False, error='BCSQ too short')
        return Frame('bcsq', compare_digest(packet[11:31], _hmac(k, packet[:11])),
                     tgid=packet[4:7], stream_id=packet[7:11])
    if op == BCST:
        return Frame('bcst', True)
    return None
