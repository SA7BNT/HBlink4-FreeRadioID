"""FreeDMR-compatible OpenBridge: framing, keys, bridge control and the trunk behaviour.

The ``ref_*`` helpers transcribe FreeDMR's hblink.py (OPENBRIDGE.send_system /
send_bcka / send_bcve / send_bcsq and datagramReceived) independently of
hblink4/obp_freedmr.py, so the tests compare against FreeDMR's own rules.
"""
import asyncio
import time
import unittest
from hashlib import blake2b, sha1
from hmac import new as hmac_new
from unittest.mock import MagicMock

from hblink4 import obp_freedmr as fdmr
from hblink4.models import OpenBridgeConnectionConfig, OpenBridgeState

PASS = 'test-obp-pass'
OUR_ID, THEIR_ID = 2624, 26270


# ------------------------------------------------------------- FreeDMR reference
def ref_key(p):                       # config.py: bytes(PASSPHRASE.ljust(20,'\x00')[:20], 'utf-8')
    return bytes(p.ljust(20, '\x00')[:20], 'utf-8')


def ref_send_v5(key, packet, server_id, ts_ns, source_server, source_rptr, hops=1):
    p = b''.join([b'DMRE', packet[4:11], server_id, packet[15:], b'\x00', b'\x00', (5).to_bytes(1, 'big'),
                  ts_ns.to_bytes(8, 'big'), source_server, source_rptr, hops.to_bytes(1, 'big')])
    h = blake2b(key=key, digest_size=16); h.update(p)
    return p + h.digest()


def ref_accepts_dmre(key, packet, network_id):
    """FreeDMR datagramReceived DMRE branch: hash, NETWORK_ID, age, source server 4..7 digits."""
    if packet[55] > 4:
        signed, digest, src_server = packet[:73], packet[73:89], packet[64:68]
    else:
        signed, digest, src_server = packet[:69], packet[69:85], packet[64:68]
    h = blake2b(key=key, digest_size=16); h.update(signed)
    if h.digest() != digest or packet[11:15] != network_id:
        return False
    if int.from_bytes(packet[56:64], 'big') / 1e9 < time.time() - 5:
        return False
    return 4 <= len(str(int.from_bytes(src_server, 'big'))) <= 7


def dmrd(tg=262, src=2621234, sid=b'\x11\x22\x33\x44', rptr=OUR_ID, bits=0x00):
    return (b'DMRD' + b'\x01' + src.to_bytes(3, 'big') + tg.to_bytes(3, 'big') + rptr.to_bytes(4, 'big')
            + bytes([bits]) + sid + bytes(33))


class TestFraming(unittest.TestCase):
    def setUp(self):
        self.k = fdmr.key(PASS)

    def test_key_is_nul_padded_to_20(self):
        self.assertEqual(self.k, ref_key(PASS))
        self.assertEqual(len(self.k), 20)
        self.assertEqual(fdmr.key('x' * 30), b'x' * 20)

    def test_our_dmre_v5_is_byte_identical_to_freedmr(self):
        ts = time.time_ns()
        ours = fdmr.build_dmre(self.k, dmrd(), 5, source_server=OUR_ID, source_rptr=265800, ts_ns=ts)
        ref = ref_send_v5(ref_key(PASS), dmrd(), OUR_ID.to_bytes(4, 'big'), ts, OUR_ID.to_bytes(4, 'big'),
                          (265800).to_bytes(4, 'big'))
        self.assertEqual(len(ours), 89)
        self.assertEqual(ours, ref)
        self.assertTrue(ref_accepts_dmre(ref_key(PASS), ours, OUR_ID.to_bytes(4, 'big')))

    def test_v4_and_wrong_key(self):
        v4 = fdmr.build_dmre(self.k, dmrd(), 4, source_server=OUR_ID)
        self.assertEqual(len(v4), 85)
        self.assertTrue(ref_accepts_dmre(ref_key(PASS), v4, OUR_ID.to_bytes(4, 'big')))
        bad = fdmr.build_dmre(fdmr.key('wrong'), dmrd(), 5, source_server=OUR_ID)
        self.assertFalse(ref_accepts_dmre(ref_key(PASS), bad, OUR_ID.to_bytes(4, 'big')))

    def test_we_parse_freedmr_frames(self):
        ref = ref_send_v5(ref_key(PASS), dmrd(tg=2624, rptr=THEIR_ID), THEIR_ID.to_bytes(4, 'big'), time.time_ns(),
                          THEIR_ID.to_bytes(4, 'big'), (2621001).to_bytes(4, 'big'))
        f = fdmr.parse(self.k, ref)
        self.assertTrue(f.ok)
        self.assertEqual((f.kind, f.version, f.source_server, f.source_rptr, f.hops), ('data', 5, THEIR_ID, 2621001, 1))
        self.assertEqual(f.dmrd[:4], b'DMRD')
        self.assertEqual(f.dmrd[8:11], (2624).to_bytes(3, 'big'))
        self.assertEqual(len(f.dmrd), 53)
        self.assertFalse(fdmr.parse(fdmr.key('wrong'), ref).ok)

    def test_classic_dmrd(self):
        pkt = fdmr.build_dmrd(self.k, dmrd())
        self.assertEqual(len(pkt), 73)
        # HMAC-SHA1 with the padded key equals the plain key (HMAC pads keys itself)
        self.assertEqual(pkt[53:], hmac_new(PASS.encode(), dmrd(), sha1).digest())
        self.assertTrue(fdmr.parse(self.k, pkt).ok)

    def test_bridge_control_matches_freedmr(self):
        rk = ref_key(PASS)
        self.assertEqual(fdmr.build_bcka(self.k), b'BCKA' + hmac_new(rk, b'BCKA', sha1).digest())
        self.assertEqual(fdmr.build_bcve(self.k, 5), b'BCVE\x05' + hmac_new(rk, b'\x05', sha1).digest())
        tg, sid = (91).to_bytes(3, 'big'), b'\xaa\xbb\xcc\xdd'
        self.assertEqual(fdmr.build_bcsq(self.k, tg, sid), b'BCSQ' + tg + sid + hmac_new(rk, b'BCSQ' + tg + sid, sha1).digest())
        for pkt, kind in ((fdmr.build_bcka(self.k), 'bcka'), (fdmr.build_bcve(self.k, 5), 'bcve'),
                          (fdmr.build_bcsq(self.k, tg, sid), 'bcsq')):
            f = fdmr.parse(self.k, pkt)
            self.assertEqual((f.kind, f.ok), (kind, True))
        self.assertFalse(fdmr.parse(fdmr.key('x'), fdmr.build_bcka(self.k)).ok)


# ------------------------------------------------------------- trunk behaviour
def make_trunk(**kw):
    from hblink4.hblink import HBProtocol
    cfg = OpenBridgeConnectionConfig(
        enabled=True, name='OBP-Funkerportal', network_id=OUR_ID, local_address='0.0.0.0', local_port=51084,
        target_address='192.0.2.10', target_port=62046, passphrase=PASS,
        talkgroup_slots={(262).to_bytes(3, 'big'): 2}, preserve_source_peer=False,
        **({'enhanced_obp': True, 'proto_ver': 5, 'relax_checks': True} | kw))
    proto = HBProtocol.__new__(HBProtocol)
    proto._openbridges = {}
    proto._obp_by_tgid = {}
    proto._obp_egress_ids = {}
    proto._events = MagicMock()
    proto._forward_stream = MagicMock()
    proto._calculate_stream_targets = MagicMock(return_value=set())
    proto._emit_stream_start = MagicMock()
    proto._emit_stream_end = MagicMock()
    proto._is_dmr_terminator = MagicMock(return_value=False)
    st = OpenBridgeState(config=cfg, ip='192.0.2.10', port=62046, transport=MagicMock())
    proto._openbridges[cfg.name] = st
    return proto, st


class TestTrunk(unittest.TestCase):
    peer = ('192.0.2.10', 62046)

    def test_freedmr_data_is_routed(self):
        p, st = make_trunk()
        k = fdmr.key(PASS)
        pkt = ref_send_v5(k, dmrd(tg=262, rptr=THEIR_ID), THEIR_ID.to_bytes(4, 'big'), time.time_ns(),
                          THEIR_ID.to_bytes(4, 'big'), (2621001).to_bytes(4, 'big'))
        p._handle_openbridge_packet(st.config.name, pkt, self.peer)
        p._forward_stream.assert_called_once()
        routed = p._forward_stream.call_args[0][0]
        self.assertEqual(routed[:4], b'DMRD')
        self.assertTrue(routed[15] & 0x80)                    # TS2 from talkgroup_slots
        self.assertEqual(p._emit_stream_start.call_args.kwargs['remote_repeater_id'], 2621001)
        self.assertEqual(st.peer_version, 5)
        self.assertTrue(st.last_keepalive > 0)

    def test_unmapped_tg_sends_source_quench_once(self):
        p, st = make_trunk()
        k = fdmr.key(PASS)
        for _ in range(3):
            pkt = ref_send_v5(k, dmrd(tg=91, rptr=THEIR_ID), THEIR_ID.to_bytes(4, 'big'), time.time_ns(),
                              THEIR_ID.to_bytes(4, 'big'), bytes(4))
            p._handle_openbridge_packet(st.config.name, pkt, self.peer)
        p._forward_stream.assert_not_called()
        sent = [c.args[0] for c in st.transport.sendto.call_args_list]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][:4], b'BCSQ')

    def test_old_frames_and_bad_auth_are_dropped(self):
        p, st = make_trunk()
        k = fdmr.key(PASS)
        old = ref_send_v5(k, dmrd(rptr=THEIR_ID), THEIR_ID.to_bytes(4, 'big'), time.time_ns() - 60 * 10**9,
                          THEIR_ID.to_bytes(4, 'big'), bytes(4))
        p._handle_openbridge_packet(st.config.name, old, self.peer)
        bad = ref_send_v5(fdmr.key('nope'), dmrd(rptr=THEIR_ID), THEIR_ID.to_bytes(4, 'big'), time.time_ns(),
                          THEIR_ID.to_bytes(4, 'big'), bytes(4))
        p._handle_openbridge_packet(st.config.name, bad, self.peer)
        p._forward_stream.assert_not_called()

    def test_keepalive_marks_trunk_up_and_follows_address(self):
        p, st = make_trunk()
        p._handle_openbridge_packet(st.config.name, fdmr.build_bcka(fdmr.key(PASS)), ('192.0.2.10', 40000))
        self.assertTrue(st.alive)
        self.assertEqual(st.sockaddr, ('192.0.2.10', 40000))   # relax_checks: follow the peer
        p._events.emit.assert_called()

    def test_strict_source_without_relax(self):
        p, st = make_trunk(relax_checks=False)
        p._handle_openbridge_packet(st.config.name, fdmr.build_bcka(fdmr.key(PASS)), ('198.51.100.1', 1))
        self.assertFalse(st.alive)

    def test_egress_is_dmre_with_network_id_and_honours_quench(self):
        p, st = make_trunk()
        out = p._obp_build_egress(st, dmrd(bits=0x80, rptr=265800), (265800).to_bytes(4, 'big'), b'\x11\x22\x33\x44')
        self.assertEqual(len(out), 89)
        self.assertTrue(ref_accepts_dmre(ref_key(PASS), out, OUR_ID.to_bytes(4, 'big')))
        f = fdmr.parse(fdmr.key(PASS), out)
        self.assertEqual((f.source_rptr, f.source_server), (265800, OUR_ID))
        self.assertFalse(f.dmrd[15] & 0x80)                    # wire slot 1
        st.quench[(262).to_bytes(3, 'big')] = b'\x11\x22\x33\x44'
        self.assertIsNone(p._obp_build_egress(st, dmrd(), (265800).to_bytes(4, 'big'), b'\x11\x22\x33\x44'))

    def test_classic_trunk_unchanged(self):
        p, st = make_trunk(enhanced_obp=False, proto_ver=1, relax_checks=False)
        out = p._obp_build_egress(st, dmrd(), (265800).to_bytes(4, 'big'), b'\x11\x22\x33\x44')
        self.assertEqual(len(out), 73)
        self.assertEqual(out[11:15], OUR_ID.to_bytes(4, 'big'))
        p._handle_openbridge_packet(st.config.name, fdmr.build_dmrd(fdmr.key(PASS), dmrd(rptr=THEIR_ID)), self.peer)
        p._forward_stream.assert_called_once()

    def test_bridge_control_loop_sends_keepalive_and_version(self):
        p, st = make_trunk()

        async def run():
            t = asyncio.ensure_future(p._obp_bridge_control_loop(st))
            await asyncio.sleep(0.05)
            t.cancel()
        asyncio.run(run())
        sent = [c.args[0][:4] for c in st.transport.sendto.call_args_list]
        self.assertIn(b'BCKA', sent)
        self.assertIn(b'BCVE', sent)


if __name__ == '__main__':
    unittest.main()
