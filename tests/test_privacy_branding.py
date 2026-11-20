"""Addresses/ports are admin-only; uploaded logos are checked."""
import pytest

from dashboard.auth import redact_ips


def test_bridge_and_hotspot_addresses_are_redacted():
    trunk = {"connection_name": "OBP-X", "network_id": 2624, "remote_address": "freedmr.example.org",
             "remote_port": 62046, "local_address": "0.0.0.0", "local_port": 51084,
             "target_address": "x", "target_port": 1, "talkgroup_count": 3}
    hotspot = {"repeater_id": 2621234, "callsign": "DL1ABC", "address": "203.0.113.5:62032", "port": 62032}
    out = redact_ips({"trunks": [trunk], "repeaters": [hotspot], "note": "seen at 203.0.113.5:62032"})
    t, h = out["trunks"][0], out["repeaters"][0]
    for k in ("remote_address", "remote_port", "local_address", "local_port", "target_address", "target_port"):
        assert k not in t
    assert t["network_id"] == 2624 and t["talkgroup_count"] == 3
    assert "address" not in h and "port" not in h and h["callsign"] == "DL1ABC"
    assert "203.0.113.5" not in out["note"]


def test_branding_svg_check():
    from dashboard.server import _sniff_branding, _HTTPError
    assert _sniff_branding(b"\x89PNG\r\n\x1a\n" + b"0" * 20) == "image/png"
    assert _sniff_branding(b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>') == "image/svg+xml"
    assert _sniff_branding(b"\x00\x00\x01\x00rest") == "image/x-icon"
    for bad in (b"<svg><script>alert(1)</script></svg>", b'<svg><a href="https://evil">x</a></svg>',
                b'<svg onload="alert(1)"></svg>'):
        with pytest.raises(_HTTPError):
            _sniff_branding(bad)
    assert _sniff_branding(b"just text") is None
