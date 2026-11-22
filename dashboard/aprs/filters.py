"""
APRS-IS server-side filters (port 14580), as documented at
https://www.aprs-is.net/javAPRSFilter.aspx and implemented by aprsc.

Supported: r/ range, a/ area, p/ prefix, b/ budlist, o/ object, t/ type (also
with /call/dist), s/ symbol, d/ digipeater, e/ entry station, g/ group
(message recipient), u/ unproto (destination), m/ my range, f/ friend range.
A leading '-' turns a filter into an exclusion. '*' is a wildcard where the
spec allows it.
"""
from __future__ import annotations

import math
import re
from typing import Callable, List, Optional, Tuple

from .packet import Packet, base_call

# type letters of t/ → packet types of packet.py
_TYPES = {
    "p": ("position", "weather"),
    "o": ("object",),
    "i": ("item",),
    "m": ("message", "ack"),
    "q": ("query",),
    "s": ("status",),
    "t": ("telemetry",),
    "u": ("other",),
    "n": (),          # NWS bulletins (messages to NWS-*) handled below
    "w": ("weather",),
}


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _wild(pattern: str) -> re.Pattern:
    return re.compile("^" + re.escape(pattern.upper()).replace(r"\*", ".*") + "$")


class FilterError(ValueError):
    pass


PositionLookup = Callable[[str], Optional[Tuple[float, float]]]


class Filter:
    """One filter term such as r/52.3/8.9/100 (negate=True for -r/…)."""

    def __init__(self, text: str):
        self.text = text
        self.negate = text.startswith("-")
        body = text[1:] if self.negate else text
        if "/" not in body:
            raise FilterError(f"unknown filter {text!r}")
        self.kind, rest = body.split("/", 1)
        self.args = [a for a in rest.split("/")]
        k, a = self.kind, self.args
        try:
            if k == "r":
                self.lat, self.lon, self.dist = float(a[0]), float(a[1]), float(a[2])
            elif k == "a":
                self.n, self.w, self.s, self.e = (float(x) for x in a[:4])
            elif k in ("p", "b", "o", "d", "e", "g", "u"):
                if not any(a):
                    raise FilterError(f"empty filter {text!r}")
                self.pats = [_wild(x) if k != "p" else x.upper() for x in a if x]
            elif k == "t":
                self.types = a[0]
                self.near_call = a[1].upper() if len(a) > 2 else ""
                self.near_dist = float(a[2]) if len(a) > 2 else 0.0
            elif k == "s":
                self.pri = a[0] if a else ""
                self.alt = a[1] if len(a) > 1 else ""
                self.over = a[2] if len(a) > 2 else ""
            elif k == "m":
                self.dist = float(a[0])
            elif k == "f":
                self.call, self.dist = a[0].upper(), float(a[1])
            elif k == "q":
                self.qs = a[0]
            else:
                raise FilterError(f"unknown filter type {k!r}")
        except (IndexError, ValueError) as e:
            raise FilterError(f"invalid filter {text!r}") from e

    def matches(self, p: Packet, login: str, lookup: PositionLookup) -> bool:
        k = self.kind
        if k == "r":
            return p.has_position() and distance_km(self.lat, self.lon, p.lat, p.lon) <= self.dist
        if k == "a":
            return p.has_position() and self.s <= p.lat <= self.n and self.w <= p.lon <= self.e
        if k == "p":
            src = p.src.upper()
            return any(src.startswith(x) for x in self.pats)
        if k == "b":
            return any(r.match(p.src.upper()) for r in self.pats)
        if k == "o":
            return p.type in ("object", "item") and any(r.match(p.name.upper()) for r in self.pats)
        if k == "d":
            digis = [x.rstrip("*").upper() for x in p.path if x.endswith("*")]
            return any(r.match(d) for r in self.pats for d in digis)
        if k == "e":
            q, entry = p.q_construct()
            return bool(entry) and any(r.match(entry.upper()) for r in self.pats)
        if k == "g":
            return p.type in ("message", "ack") and any(r.match(p.msg_to.upper()) for r in self.pats)
        if k == "u":
            return any(r.match(p.dst.upper()) for r in self.pats)
        if k == "t":
            ok = False
            for ch in self.types:
                if p.type in _TYPES.get(ch, ()):
                    ok = True
                if ch == "n" and p.type == "message" and p.msg_to.upper().startswith("NWS"):
                    ok = True
            if not ok:
                return False
            if self.near_call:
                c = lookup(self.near_call)
                return bool(c and p.has_position() and distance_km(c[0], c[1], p.lat, p.lon) <= self.near_dist)
            return True
        if k == "s":
            if len(p.symbol) != 2:
                return False
            table, code = p.symbol[0], p.symbol[1]
            if table == "/":
                return code in self.pri
            if code in self.alt:
                return not self.over or table in self.over or (table == "\\" and "\\" in self.over)
            return False
        if k == "m":
            c = lookup(login)
            return bool(c and p.has_position() and distance_km(c[0], c[1], p.lat, p.lon) <= self.dist)
        if k == "f":
            c = lookup(self.call)
            return bool(c and p.has_position() and distance_km(c[0], c[1], p.lat, p.lon) <= self.dist)
        if k == "q":
            q, _ = p.q_construct()
            return bool(q) and q[-1] in self.qs
        return False


class FilterSet:
    """A client's whole filter string. Empty = nothing (except messages to the client)."""

    MAX_TERMS = 30

    def __init__(self, text: str = ""):
        self.text = (text or "").strip()
        self.include: List[Filter] = []
        self.exclude: List[Filter] = []
        for term in self.text.split()[: self.MAX_TERMS]:
            f = Filter(term)
            (self.exclude if f.negate else self.include).append(f)

    def __bool__(self) -> bool:
        return bool(self.include)

    def matches(self, p: Packet, login: str, lookup: PositionLookup) -> bool:
        if any(f.matches(p, login, lookup) for f in self.exclude):
            return False
        return any(f.matches(p, login, lookup) for f in self.include)


def addressed_to(p: Packet, login: str) -> bool:
    """Messages to the login (exact, or any SSID of a client logged in without SSID)."""
    if p.type not in ("message", "ack") or not p.msg_to:
        return False
    to = p.msg_to.upper()
    lg = login.upper()
    return to == lg or ("-" not in lg and base_call(to) == lg)
