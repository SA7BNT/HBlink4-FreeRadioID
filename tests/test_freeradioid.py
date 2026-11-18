"""
Tests for the FreeRadioID.net secure login (hblink4/freeradioid.py).

The API is never contacted: FreeRadioIDAuth._query_sync is replaced by a fake
that plays the role of freeradioid.net, holding per-ID passwords.
"""

import asyncio
import json
import os
import sys
import unittest
from hashlib import sha256

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hblink4 import hblink as hblink_mod
from hblink4 import freeradioid as frid
from hblink4.hblink import HBProtocol
from hblink4.constants import RPTACK, MSTNAK

DEFAULT_PASS = 'passw0rd'          # public default passphrase
PRIVATE_PASS = 'my-own-repeaters'  # pattern that opts out of FreeRadioID
USER_ID = 2620001
USER_PASS = 'frid-secret'
OWN_ID = 2629999
ADDR = ('203.0.113.10', 62031)


def rid(n):
    return n.to_bytes(4, 'big')


def login_hash(salt, pw):
    return bytes.fromhex(sha256(salt.to_bytes(4, 'big') + pw.encode()).hexdigest())


def make_config(enabled=True, guest_tgs=frid.DEFAULT_GUEST_TALKGROUPS):
    return {
        'global': {'user_cache': {'timeout': 600}},
        'dashboard': {'enabled': False},
        'freeradioid': {'enabled': enabled, 'guest_talkgroups': guest_tgs},
        'repeater_configurations': {
            'patterns': [{
                'name': 'Own', 'match': {'ids': [OWN_ID]},
                'config': {'passphrase': PRIVATE_PASS, 'freeradioid': False},
            }],
            'default': {'passphrase': DEFAULT_PASS},
        },
    }


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr=None):
        self.sent.append((data, addr))

    def last(self):
        return self.sent[-1][0] if self.sent else None


class TokenTests(unittest.TestCase):
    def test_tokens_match_reference_implementation(self):
        # Values computed exactly as in hblink_secure.py (FreeRadioID beta)
        from hashlib import md5
        req = md5(b'*%d#%d/FRID*' % (USER_ID, 1234)).hexdigest()
        self.assertEqual(frid.request_token(USER_ID, 1234), req)
        self.assertEqual(frid.result_token(USER_ID, req),
                         md5(b'RESULT+%d-%s' % (USER_ID, req.encode())).hexdigest())

    def test_parse_response(self):
        req = frid.request_token(USER_ID, 99)
        h = login_hash(99, USER_PASS)
        good = json.dumps({'RESULT': True, 'RT': frid.result_token(USER_ID, req), 'RH': h.hex()})
        self.assertEqual(frid.parse_response(good, USER_ID, req), h)
        forged = json.dumps({'RESULT': True, 'RT': '0' * 32, 'RH': h.hex()})
        self.assertIsNone(frid.parse_response(forged, USER_ID, req))
        self.assertIsNone(frid.parse_response('{"RESULT": false}', USER_ID, req))
        self.assertIsNone(frid.parse_response('<html>', USER_ID, req))


class LoginFlowTests(unittest.TestCase):
    def setUp(self, cfg=None):
        self._saved = hblink_mod.CONFIG
        hblink_mod.CONFIG = cfg or make_config()
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.hb = HBProtocol()
        self.hb.transport = FakeTransport()
        self.api_calls = 0

        def fake_query(radio_id, salt, ip):  # plays freeradioid.net
            self.api_calls += 1
            if radio_id == USER_ID:
                return login_hash(salt, USER_PASS)
            return None
        self.hb._frid._query_sync = fake_query

    def tearDown(self):
        hblink_mod.CONFIG = self._saved
        self.loop.close()
        asyncio.set_event_loop(None)

    def login(self, radio_id, password):
        """RPTL + RPTK; returns the repeater state (or None) and last packet."""
        r = rid(radio_id)

        async def run():
            self.hb._handle_repeater_login(r, ADDR)
            salt = int.from_bytes(self.hb.transport.last()[6:10], 'big')
            self.hb._handle_auth_response(r, login_hash(salt, password), ADDR)
            if self.hb._auth_tasks:
                await asyncio.gather(*list(self.hb._auth_tasks))
        self.loop.run_until_complete(run())
        return self.hb._repeaters.get(r), self.hb.transport.last()

    def test_registered_user_gets_full_access(self):
        state, pkt = self.login(USER_ID, USER_PASS)
        self.assertEqual(pkt, RPTACK + rid(USER_ID))
        self.assertEqual(state.auth_method, frid.AUTH_FREERADIOID)
        self.assertIsNone(state.guest_tx_talkgroups)
        self.assertEqual(self.api_calls, 1)

    def test_default_passphrase_is_guest_without_api_call(self):
        state, pkt = self.login(USER_ID, DEFAULT_PASS)
        self.assertEqual(pkt, RPTACK + rid(USER_ID))
        self.assertEqual(state.auth_method, frid.AUTH_GUEST)
        self.assertEqual(self.api_calls, 0)
        state.connection_state = 'connected'
        self.assertTrue(self.hb._check_inbound_routing(rid(USER_ID), 1, (26501).to_bytes(3, 'big')))
        self.assertFalse(self.hb._check_inbound_routing(rid(USER_ID), 1, (262).to_bytes(3, 'big')))

    def test_wrong_password_rejected(self):
        state, pkt = self.login(USER_ID, 'wrong')
        self.assertIsNone(state)
        self.assertTrue(pkt.startswith(MSTNAK))

    def test_unregistered_id_with_wrong_password_rejected(self):
        state, pkt = self.login(2620002, USER_PASS)
        self.assertIsNone(state)
        self.assertTrue(pkt.startswith(MSTNAK))

    def test_pattern_opt_out_uses_plain_passphrase(self):
        state, pkt = self.login(OWN_ID, PRIVATE_PASS)
        self.assertEqual(state.auth_method, frid.AUTH_LOCAL)
        self.assertIsNone(state.guest_tx_talkgroups)
        self.assertEqual(self.api_calls, 0)

    def test_api_down_still_allows_guests_and_rejects_others(self):
        def boom(*a):
            raise OSError('unreachable')
        self.hb._frid._query_sync = boom
        state, _ = self.login(USER_ID, USER_PASS)
        self.assertIsNone(state)
        state, _ = self.login(USER_ID, DEFAULT_PASS)
        self.assertEqual(state.auth_method, frid.AUTH_GUEST)


class DisabledTests(LoginFlowTests):
    def setUp(self):
        super().setUp(make_config(enabled=False))

    def test_registered_user_gets_full_access(self):
        state, pkt = self.login(USER_ID, USER_PASS)
        self.assertIsNone(state)  # only the local passphrase counts

    def test_default_passphrase_is_guest_without_api_call(self):
        state, _ = self.login(USER_ID, DEFAULT_PASS)
        self.assertEqual(state.auth_method, frid.AUTH_LOCAL)
        self.assertIsNone(state.guest_tx_talkgroups)
        self.assertEqual(self.api_calls, 0)

    def test_api_down_still_allows_guests_and_rejects_others(self):
        pass


if __name__ == '__main__':
    unittest.main()
