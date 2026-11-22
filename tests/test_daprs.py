"""
DAPRS: GPS positions in DMR traffic (hblink4/daprs.py).

* embedded GPS Info LC in voice bursts B–E (BPTC 128,72 with Hamming/parity/checksum)
* GPS data calls: data header + rate-½ blocks with NMEA text, also inside IPv4/UDP,
  and Motorola LRRP (UDP 4001)
* the decoder's per-slot state, destination filter and repeat suppression
"""
import struct
import unittest

from hblink4 import daprs as d
from hblink4.lc import _decode_bptc_96

RID = b"\x00\x04\x02\x9c"


def voice_payload(frag: int) -> bytes:
    """33-byte voice burst whose embedded-signalling window (bits 116..147) holds frag."""
    base = bytearray(33)
    v = int.from_bytes(base[14:19], "big")
    v = (v & ~(0xFFFFFFFF << 4)) | (frag << 4)
    base[14:19] = v.to_bytes(5, "big")
    return bytes(base)


def feed_embedded(dec, lc, radio=2621234, tg=262, slot=1):
    out = None
    for vseq, frag in enumerate(d.encode_embedded_lc(lc), 1):
        r = dec.feed(RID, slot, radio.to_bytes(3, "big"), tg.to_bytes(3, "big"), 0, vseq, voice_payload(frag), 0)
        out = r or out
    return out


def feed_data_call(dec, user: bytes, *, src=2621234, dst=900999, sap=0, slot=2, unit=True):
    units = d.build_data_call(user, dst=dst, src=src, sap=sap, group=not unit)
    out = None
    for i, u in enumerate(units):
        dtype = d.DTYPE_DATA_HEADER if i == 0 else d.DTYPE_RATE_12
        r = dec.feed(RID, slot, src.to_bytes(3, "big"), dst.to_bytes(3, "big"), 2, dtype, d.build_data_burst(u, dtype),
                     1 if unit else 0)
        out = r or out
    return out


def ipv4_udp(payload: bytes, port: int) -> bytes:
    udp = struct.pack(">HHHH", 4001, port, 8 + len(payload), 0) + payload
    ip = bytearray(20)
    ip[0] = 0x45
    ip[2:4] = (20 + len(udp)).to_bytes(2, "big")
    ip[8] = 64
    ip[9] = 17
    ip[12:16] = bytes([12, 0, 0, 1])
    ip[16:20] = bytes([13, 0, 0, 1])
    return bytes(ip) + udp


def lrrp_report(lat: float, lon: float) -> bytes:
    lat_raw = int(round(lat * 2147483648.0 / 90.0))
    lon_raw = int(round(lon * 2147483648.0 / 180.0))
    body = b"\x22\x03\x00\x00\x01" + b"\x51" + struct.pack(">ii", lat_raw, lon_raw) + b"\x56\x2d"
    return bytes([0x0D, len(body)]) + body


class TestEmbeddedGps(unittest.TestCase):
    def test_roundtrip_and_coordinates(self):
        lc = d.build_gps_info_lc(52.2897, 8.9161, error=1)
        self.assertEqual(d.decode_embedded_lc(d.encode_embedded_lc(lc)), lc)
        lat, lon, err = d.parse_gps_info_lc(lc)
        self.assertAlmostEqual(lat, 52.2897, places=4)
        self.assertAlmostEqual(lon, 8.9161, places=4)
        self.assertEqual(err, 20)

    def test_southern_and_western_hemisphere(self):
        lat, lon, _ = d.parse_gps_info_lc(d.build_gps_info_lc(-33.86, -70.65))
        self.assertAlmostEqual(lat, -33.86, places=4)
        self.assertAlmostEqual(lon, -70.65, places=4)

    def test_any_bit_error_rejects_the_block(self):
        frags = list(d.encode_embedded_lc(d.build_gps_info_lc(52.0, 8.0)))
        for i in range(4):
            for bit in (0, 7, 19, 31):
                bad = list(frags)
                bad[i] ^= 1 << bit
                self.assertIsNone(d.decode_embedded_lc(tuple(bad)), f"fragment {i} bit {bit}")

    def test_voice_lc_is_not_a_position(self):
        voice_lc = bytes([0x00, 0x00, 0x20, 0x00, 0x01, 0x06, 0x27, 0xFF, 0x33])
        self.assertIsNone(d.parse_gps_info_lc(voice_lc))
        dec = d.DaprsDecoder({})
        self.assertIsNone(feed_embedded(dec, voice_lc))

    def test_decoder_finds_position_in_voice_bursts(self):
        dec = d.DaprsDecoder({})
        fix = feed_embedded(dec, d.build_gps_info_lc(52.2897, 8.9161), radio=2621234, tg=262, slot=1)
        self.assertIsNotNone(fix)
        self.assertEqual((fix.radio_id, fix.source, fix.dst_id, fix.slot, fix.group), (2621234, "embedded", 262, 1, True))
        self.assertAlmostEqual(fix.lat, 52.2897, places=4)
        # the next superframe 2 s later is the same report: suppressed
        self.assertIsNone(feed_embedded(dec, d.build_gps_info_lc(52.2897, 8.9161), radio=2621234))
        # another radio is not
        self.assertIsNotNone(feed_embedded(dec, d.build_gps_info_lc(52.0, 8.5), radio=2625555))

    def test_bursts_out_of_order_are_ignored(self):
        dec = d.DaprsDecoder({})
        frags = d.encode_embedded_lc(d.build_gps_info_lc(52.0, 8.0))
        src, dst = (2621234).to_bytes(3, "big"), (262).to_bytes(3, "big")
        for vseq in (1, 3, 2, 4):          # C before B: no decode
            self.assertIsNone(dec.feed(RID, 1, src, dst, 0, vseq, voice_payload(frags[vseq - 1]), 0))

    def test_switched_off(self):
        dec = d.DaprsDecoder({"embedded_gps": False})
        self.assertIsNone(feed_embedded(dec, d.build_gps_info_lc(52.0, 8.0)))


class TestNmea(unittest.TestCase):
    def test_rmc(self):
        pos = d.parse_nmea("$GPRMC,123519,A,5217.382,N,00854.966,E,012.4,084.4,221126,,,A*6A")
        self.assertAlmostEqual(pos["lat"], 52.28970, places=4)
        self.assertAlmostEqual(pos["lon"], 8.91610, places=4)
        self.assertEqual(pos["course"], 84)
        self.assertAlmostEqual(pos["speed_kmh"], 23.0, places=0)

    def test_rmc_without_fix_and_gga(self):
        self.assertIsNone(d.parse_nmea("$GPRMC,123519,V,5217.382,N,00854.966,E,0,0,221126,,,N*6A"))
        pos = d.parse_nmea("junk$GNGGA,123519,3352.000,S,07039.000,W,1,08,0.9,545.4,M,46.9,M,,*47")
        self.assertAlmostEqual(pos["lat"], -33.8667, places=3)
        self.assertAlmostEqual(pos["lon"], -70.65, places=3)
        self.assertEqual(pos["altitude_m"], 545.4)


class TestDataCalls(unittest.TestCase):
    NMEA = b"$GPRMC,123519,A,5217.382,N,00854.966,E,012.4,084.4,221126,,,A*6A\r\n"

    def test_rate_half_blocks_roundtrip(self):
        raw = bytes(range(12))
        self.assertEqual(_decode_bptc_96(d.build_data_burst(raw, d.DTYPE_RATE_12)), raw)

    def test_nmea_data_call(self):
        dec = d.DaprsDecoder({})
        fix = feed_data_call(dec, self.NMEA, src=2634567, dst=900999)
        self.assertIsNotNone(fix)
        self.assertEqual((fix.radio_id, fix.source, fix.dst_id, fix.group), (2634567, "nmea", 900999, False))
        self.assertAlmostEqual(fix.lat, 52.2897, places=4)
        self.assertEqual(fix.course, 84)

    def test_nmea_as_utf16_sms(self):
        dec = d.DaprsDecoder({})
        fix = feed_data_call(dec, b"\x00\x0c\xa0\x00" + self.NMEA.decode().encode("utf-16-le"), src=2634568)
        self.assertIsNotNone(fix)
        self.assertEqual(fix.source, "nmea")

    def test_lrrp_over_ip(self):
        dec = d.DaprsDecoder({})
        fix = feed_data_call(dec, ipv4_udp(lrrp_report(52.2730, 8.9180), d.LRRP_PORT), src=2639999, sap=4)
        self.assertIsNotNone(fix)
        self.assertEqual(fix.source, "lrrp")
        self.assertAlmostEqual(fix.lat, 52.2730, places=4)
        self.assertAlmostEqual(fix.lon, 8.9180, places=4)
        self.assertEqual(fix.course, 90)

    def test_destination_filter(self):
        dec = d.DaprsDecoder({"data_ids": [900999]})
        self.assertIsNone(feed_data_call(dec, self.NMEA, dst=5057))
        self.assertIsNotNone(feed_data_call(dec, self.NMEA, dst=900999))

    def test_data_without_position(self):
        dec = d.DaprsDecoder({})
        self.assertIsNone(feed_data_call(dec, b"Hello, this is just an SMS without a position"))
        self.assertEqual(dec.stats["undecoded"], 1)

    def test_rate_three_quarter_is_not_guessed(self):
        dec = d.DaprsDecoder({})
        units = d.build_data_call(self.NMEA, dst=900999, src=2621234)
        src, dst = (2621234).to_bytes(3, "big"), (900999).to_bytes(3, "big")
        dec.feed(RID, 2, src, dst, 2, d.DTYPE_DATA_HEADER, d.build_data_burst(units[0], 6), 1)
        self.assertIsNone(dec.feed(RID, 2, src, dst, 2, d.DTYPE_RATE_34, d.build_data_burst(units[1], 8), 1))
        self.assertEqual(dec.stats["undecoded"], 1)

    def test_voice_header_and_other_frames_are_cheap_noops(self):
        dec = d.DaprsDecoder({})
        src, dst = (1).to_bytes(3, "big"), (2).to_bytes(3, "big")
        self.assertIsNone(dec.feed(RID, 1, src, dst, 1, 0, bytes(33), 0))     # voice sync burst A
        self.assertIsNone(dec.feed(RID, 1, src, dst, 2, 1, bytes(33), 0))     # voice header
        self.assertIsNone(dec.feed(RID, 1, src, dst, 0, 5, bytes(33), 0))     # burst F


class TestEmitToDashboard(unittest.TestCase):
    def test_event_carries_repeater_and_login_type(self):
        from hblink4.hblink import HBProtocol
        from hblink4.models import RepeaterState

        class Events:
            def __init__(self):
                self.out = []

            def emit(self, kind, data):
                self.out.append((kind, data))

        p = HBProtocol.__new__(HBProtocol)
        p._events = Events()
        rep = RepeaterState(repeater_id=(262916).to_bytes(4, "big"), ip="203.0.113.5", port=62031)
        rep.callsign = b"DB0QMI  "
        rep.connection_type = "repeater"
        rep.auth_method = "freeradioid"
        p._emit_gps(rep, d.GpsFix(2621234, 52.1, 8.9, "embedded", slot=2, dst_id=262))
        kind, data = p._events.out[0]
        self.assertEqual(kind, "gps_position")
        self.assertEqual((data["radio_id"], data["repeater_id"], data["repeater_callsign"], data["auth_method"]),
                         (2621234, 262916, "DB0QMI", "freeradioid"))


if __name__ == "__main__":
    unittest.main()
