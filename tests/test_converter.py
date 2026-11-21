"""Configuration converter: HBlink3, FreeDMR, ADN and HBlink4 configurations to HBlink4 settings."""
import unittest
from pathlib import Path

import pytest

from dashboard.converter import analyze, detect, merge_list, parse_acl, parse_rules, ConvertError

FX = Path(__file__).parent / "fixtures"


def items(res, key):
    return [i for i in res["items"] if i["key"] == key]


def test_detect():
    assert detect((FX / "hblink3_lz5pn.cfg").read_text()) == "hblink3"
    assert detect((FX / "freedmr_sample.cfg").read_text()) == "freedmr"
    assert detect((FX / "adn_sample.yaml").read_text()) == "adn"
    assert detect('{"global": {}}') == "hblink4"
    with pytest.raises(ConvertError):
        detect("hello")


def test_acl():
    assert parse_acl("PERMIT:ALL") == ("PERMIT", [])
    assert parse_acl("DENY:1,1000-2000") == ("DENY", [(1, 1), (1000, 2000)])


def test_hblink3_with_rules():
    res = analyze((FX / "hblink3_lz5pn.cfg").read_text(), "auto", (FX / "hblink3_lz5pn_rules.py").read_text())
    assert res["format"] == "hblink3" and res["bridges"] == 2
    assert items(res, "dmr.global.port_ipv4")[0]["value"] == 54000
    assert items(res, "dmr.repeater_configurations.default.passphrase")[0]["value"] == "s3cr37w0rd"
    assert items(res, "dmr.repeater_configurations.default.slot2_talkgroups")[0]["value"] == [11, 9999]
    trunk = items(res, "dmr.openbridge_connections")[0]["value"][0]
    assert trunk["network_id"] == 3129100 and trunk["target_address"] == "1.2.3.4" and trunk["all_talkgroups"]
    assert not trunk["enabled"] and not items(res, "dmr.openbridge_connections")[0]["warnings"]
    links = [i["value"][0]["name"] for i in items(res, "dmr.outbound_connections")]
    assert links == ["REPEATER-1", "XLX-1"]
    assert any(s["section"] == "ECHOTEST" for s in res["skipped"])


def test_freedmr_obp_uses_server_id():
    cfg = (FX / "freedmr_sample.cfg").read_text().replace("SERVER_ID: 0000", "SERVER_ID: 2624") \
        .replace("[OBP-TEST]\nMODE: OPENBRIDGE\nENABLED: False", "[OBP-TEST]\nMODE: OPENBRIDGE\nENABLED: True") \
        .replace("TARGET_IP: \n", "TARGET_IP: freedmr.example.org\n")
    res = analyze(cfg)
    t = items(res, "dmr.openbridge_connections")[0]["value"][0]
    assert t["network_id"] == 2624 and t["enhanced_obp"] and t["proto_ver"] == 2 and t["relax_checks"]
    assert t["enabled"] and t["all_talkgroups"] and t["target_address"] == "freedmr.example.org"
    assert items(res, "dmr.repeater_configurations.default.trust")[0]["selected"] is False


def test_adn_yaml():
    res = analyze((FX / "adn_sample.yaml").read_text())
    assert res["format"] == "adn"
    assert items(res, "dmr.global.stream_hang_time")[0]["value"] == 15.0
    assert not items(res, "dmr.repeater_configurations.default.passphrase")    # placeholder not taken over
    assert {"PROXY", "DATABASE"} <= {s["section"] for s in res["skipped"]}


def test_hblink4_json():
    res = analyze('{"global": {"port_ipv4": 62031}, "openbridge_connections": [{"name": "T", "network_id": 1}]}')
    assert items(res, "dmr.global.port_ipv4")[0]["value"] == 62031
    assert items(res, "dmr.openbridge_connections")[0]["merge"] == "name"


def test_rules_are_not_executed():
    assert parse_rules("BRIDGES = {'A': [{'SYSTEM': 'M', 'TS': 2, 'TGID': 1}]}")["A"][0]["TGID"] == 1
    with pytest.raises(ConvertError):
        parse_rules("BRIDGES = {'A': __import__('os').system('true')}")


def test_merge_list():
    assert merge_list([{"name": "a", "x": 1}, {"name": "b"}], [{"name": "a", "x": 2}, {"name": "c"}]) == \
        [{"name": "a", "x": 2}, {"name": "b"}, {"name": "c"}]


def test_imported_trunks_and_links_pass_hblink4_validation():
    from hblink4.config import parse_openbridge_connections, parse_outbound_connections
    res = analyze((FX / "hblink3_lz5pn.cfg").read_text(), "auto", (FX / "hblink3_lz5pn_rules.py").read_text())
    trunks = [v for i in items(res, "dmr.openbridge_connections") for v in i["value"]]
    links = [v for i in items(res, "dmr.outbound_connections") for v in i["value"]]
    assert parse_openbridge_connections({"openbridge_connections": trunks}, raise_errors=True)
    assert parse_outbound_connections({"outbound_connections": links}, raise_errors=True)


class TestObpStanzaPaste(unittest.TestCase):
    STANZA = """[OBP-N8schicht]
MODE: OPENBRIDGE
ENABLED: True
IP:
PORT: 62046
NETWORK_ID: 2624
PASSPHRASE: test-obp-pass
TARGET_IP: dmr.example.org
#TARGET_PORT: 62046
TARGET_PORT: 51084
USE_ACL: True
#TGID_ACL: DENY:0-82,92-199
TGID_ACL: PERMIT:ALL
RELAX_CHECKS: True
ENHANCED_OBP: True
PROTO_VER: 5
"""

    def test_own_stanza(self):
        from dashboard.converter import parse_obp_stanzas
        t = parse_obp_stanzas(self.STANZA, ["our.server"])["trunks"][0]
        self.assertEqual((t["local_port"], t["target_address"], t["target_port"]), (62046, "dmr.example.org", 51084))
        self.assertTrue(t["all_talkgroups"]); self.assertEqual(t["proto_ver"], 5); self.assertTrue(t["enhanced_obp"])
        self.assertNotIn("mirrored", t)

    def test_far_end_view_is_mirrored(self):
        from dashboard.converter import parse_obp_stanzas
        r = parse_obp_stanzas(self.STANZA, ["DMR.example.org."])
        t = r["trunks"][0]
        self.assertTrue(t["mirrored"])
        self.assertEqual((t["local_port"], t["target_port"], t["target_address"]), (51084, 62046, ""))
        self.assertTrue(any("far end's view" in n for n in r["notes"]))

    def test_no_obp_section(self):
        from dashboard.converter import parse_obp_stanzas, ConvertError
        with self.assertRaises(ConvertError):
            parse_obp_stanzas("[MASTER]\nMODE: MASTER\n")
