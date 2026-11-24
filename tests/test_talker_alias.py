"""DMR Talker Alias: embedded LC (FLCO 4–7) and DMRA blocks → text."""
import json
import os

from hblink4.talker_alias import TalkerAliasDecoder, TalkerAliasTracker

FX = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "talker_alias.json"), encoding="utf-8"))


def _feed(text, sid=b"\x00\x00\x00\x01"):
    t = TalkerAliasTracker()
    got = []
    for vseq, hexp in FX[text]["bursts"]:
        r = t.feed_burst(("repeater", b"\x00\x00\x00\x01", 2), sid, vseq, bytes.fromhex(hexp))
        if r:
            got.append(r)
    return got


def test_7bit_alias_from_embedded_lc():
    got = _feed("DO1TEST Tobias")
    assert got[-1] == ("DO1TEST Tobias", True)


def test_latin1_alias_with_umlauts():
    assert _feed("Jürgen DL9ÄÖ")[-1] == ("Jürgen DL9ÄÖ", True)


def test_long_alias_four_blocks_partial_first():
    got = _feed("DB0XYZ Relais Minden JO42 xyz")
    assert got[0][1] is False                     # first block: partial text
    assert got[-1] == ("DB0XYZ Relais Minden JO42 xyz", True)


def test_dmra_blocks():
    d = TalkerAliasDecoder()
    for i, h in enumerate(FX["DB0XYZ Relais Minden JO42 xyz"]["dmra"]):
        txt = d.add_block(i, bytes.fromhex(h))
    assert txt == "DB0XYZ Relais Minden JO42 xyz" and d.complete()


def test_new_stream_resets_alias():
    t = TalkerAliasTracker()
    for vseq, hexp in FX["DO1TEST Tobias"]["bursts"]:
        t.feed_burst(("r", 1, 1), b"AAAA", vseq, bytes.fromhex(hexp))
    first = None
    for vseq, hexp in FX["Jürgen DL9ÄÖ"]["bursts"]:
        r = t.feed_burst(("r", 1, 1), b"BBBB", vseq, bytes.fromhex(hexp))
        first = first or r
    assert first and first[0].startswith("Jürgen")


def test_garbage_is_ignored():
    t = TalkerAliasTracker()
    for vseq in (1, 2, 3, 4):
        assert t.feed_burst(("r", 1, 1), b"CCCC", vseq, bytes(33)) is None
