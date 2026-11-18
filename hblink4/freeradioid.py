"""
FreeRadioID.net secure login for HBlink4.

Port of the FreeRadioID "FDMR-Secure-Server" beta (DO5ALF,
https://freeradioid.net/betatest.html) from the FreeDMR/ADN hblink.py to
HBlink4's asyncio server.

How it works
------------
Every HomeBrew login answers the server's 4-byte salt with
    sha256(salt + passphrase)
FreeRadioID users have a personal password stored at freeradioid.net. When a
repeater/hotspot answers the challenge, HBlink4 asks the FreeRadioID API for
the hash it EXPECTS for that radio ID and salt, and compares it with what the
device sent. The password itself never leaves freeradioid.net.

    POST https://freeradioid.net/api/adn-login.php
      cookies: TOKEN = md5("*<id>#<salt>/FRID*"), KEY = salt as 8 hex chars
      form:    action=GPWD, ID=<id>, SALT=<salt>, CIP=<client ip>
    -> {"RESULT": true, "RT": md5("RESULT+<id>-<TOKEN>"), "RH": "<64 hex>"}

RT proves the answer belongs to this request; RH is the expected login hash.

Outcome of a login (when FreeRadioID applies to the matched pattern):
  * hash matches RH                -> 'freeradioid'  full access
  * hash matches local passphrase  -> 'guest'        TX limited to guest_talkgroups
  * neither                        -> rejected

The HTTP request runs in a worker thread so the event loop never blocks.
"""

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from hashlib import md5
from hmac import compare_digest
from typing import Any, Dict, Optional, Set

LOGGER = logging.getLogger(__name__)

DEFAULT_API_URL = 'https://freeradioid.net/api/adn-login.php'
DEFAULT_USER_AGENT = 'HBlink4-Server-Login FreeRadioID-Mod 1.0beta1'
# Talkgroups the FreeRadioID beta opens to unregistered (guest) logins.
DEFAULT_GUEST_TALKGROUPS = [26501, 26502, 26503]

AUTH_LOCAL = 'local'              # FreeRadioID not used for this repeater
AUTH_FREERADIOID = 'freeradioid'  # verified registered FreeRadioID user
AUTH_GUEST = 'guest'              # local/default passphrase while FreeRadioID is active


def request_token(radio_id: int, salt: int) -> str:
    return md5(b'*%d#%d/FRID*' % (radio_id, salt)).hexdigest()


def result_token(radio_id: int, req_token: str) -> str:
    return md5(b'RESULT+%d-%s' % (radio_id, req_token.encode())).hexdigest()


def parse_response(body: str, radio_id: int, req_token: str) -> Optional[bytes]:
    """
    Validate an API answer. Returns the expected 32-byte login hash, or None if
    the user is unknown or the answer does not belong to this request.
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get('RESULT') is not True:
        return None
    rt = data.get('RT')
    rh = data.get('RH')
    if not isinstance(rt, str) or not isinstance(rh, str):
        return None
    if not compare_digest(rt.lower(), result_token(radio_id, req_token)):
        LOGGER.warning(f'FreeRadioID: response token mismatch for {radio_id} - answer ignored')
        return None
    try:
        expected = bytes.fromhex(rh)
    except ValueError:
        return None
    return expected if len(expected) == 32 else None


class FreeRadioIDAuth:
    """Config holder + async API client for the FreeRadioID secure login."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None, logger: Optional[logging.Logger] = None):
        # HBlink4 configures handlers only on its main logger; log through it.
        global LOGGER
        if logger is not None:
            LOGGER = logger
        cfg = cfg or {}
        self.enabled: bool = bool(cfg.get('enabled', False))
        self.api_url: str = cfg.get('api_url', DEFAULT_API_URL)
        self.timeout: float = float(cfg.get('timeout', 3.0))
        self.user_agent: str = cfg.get('user_agent', DEFAULT_USER_AGENT)
        guest = cfg.get('guest_talkgroups', DEFAULT_GUEST_TALKGROUPS)
        # None = guests are unrestricted; [] = guests may not transmit at all.
        self.guest_talkgroups: Optional[Set[bytes]] = (
            None if guest is None else {int(tg).to_bytes(3, 'big') for tg in guest}
        )
        if self.enabled:
            LOGGER.info(
                f'FreeRadioID secure login ENABLED ({self.api_url}); guest TX talkgroups: '
                f'{"unrestricted" if guest is None else sorted(guest)}'
            )

    def applies(self, repeater_config) -> bool:
        """FreeRadioID is on globally unless the matched pattern opts out."""
        if not self.enabled:
            return False
        return getattr(repeater_config, 'freeradioid', None) is not False

    # -- HTTP -------------------------------------------------------------

    def _query_sync(self, radio_id: int, salt: int, client_ip: str) -> Optional[bytes]:
        req_token = request_token(radio_id, salt)
        form = urllib.parse.urlencode({
            'action': 'GPWD', 'ID': radio_id, 'SALT': salt, 'CIP': client_ip,
        }).encode()
        req = urllib.request.Request(self.api_url, data=form, method='POST', headers={
            'User-Agent': self.user_agent,
            'Content-Type': 'application/x-www-form-urlencoded',
            'Cookie': f'TOKEN={req_token}; KEY={salt:08x}',
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status != 200:
                    LOGGER.warning(f'FreeRadioID: HTTP {resp.status} for {radio_id}')
                    return None
                body = resp.read(4096).decode('utf-8', errors='replace')
        except Exception as e:  # network errors must never break a login path
            LOGGER.warning(f'FreeRadioID: API unreachable for {radio_id}: {e}')
            return None
        return parse_response(body, radio_id, req_token)

    async def fetch_expected_hash(self, radio_id: int, salt: int, client_ip: str) -> Optional[bytes]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._query_sync, radio_id, salt, client_ip)
