"""End-to-end check of DAPRS: real HBlink4, a hotspot sending GPS in a voice call and as a data call,
and a fake dashboard on the event socket that must receive both positions.

Run: python3 scripts/e2e_daprs.py   (uses UDP port 62132 and a Unix socket in /tmp, takes ~6 s)
"""
import json, os, socket, subprocess, sys, threading, time
from hashlib import sha256

REPO = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from hblink4 import daprs as d  # noqa: E402

W = '/tmp/hblink4-e2e-daprs'
os.makedirs(W, exist_ok=True)
for f in os.listdir(W):
    os.remove(os.path.join(W, f))
PORT = 62132
SOCK = f'{W}/events.sock'

# ---- fake dashboard: collects the events HBlink4 sends
events = []
lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
lsock.bind(SOCK); lsock.listen(1)


def receiver():
    conn, _ = lsock.accept()
    buf = b''
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            return
        buf += chunk
        while len(buf) >= 4 and len(buf) >= 4 + int.from_bytes(buf[:4], 'big'):
            n = int.from_bytes(buf[:4], 'big')
            events.append(json.loads(buf[4:4 + n]))
            buf = buf[4 + n:]


threading.Thread(target=receiver, daemon=True).start()

cfg = {
    "global": {"bind_ipv4": "127.0.0.1", "port_ipv4": PORT, "disable_ipv6": True,
               "settings_db": f"{W}/db.db", "logging": {"file": f"{W}/hb.log", "console_level": "INFO",
                                                         "file_level": "DEBUG", "retention_days": 1}},
    "dashboard": {"enabled": True, "transport": "unix", "unix_socket": SOCK},
    "repeater_configurations": {"default": {"passphrase": "passw0rd",
                                            "slot1_talkgroups": [262], "slot2_talkgroups": [262]}},
    "freeradioid": {"enabled": False},
    "daprs": {"enabled": True},
}
json.dump(cfg, open(f'{W}/config.json', 'w'))
proc = subprocess.Popen([sys.executable, 'run_hblink.py', f'{W}/config.json'], cwd=REPO,
                        stdout=open(f'{W}/out.log', 'w'), stderr=subprocess.STDOUT)
time.sleep(3)

ok = True


def check(cond, msg):
    global ok
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    ok = ok and cond


def voice_payload(frag):
    base = bytearray(33)
    v = int.from_bytes(base[14:19], 'big')
    v = (v & ~(0xFFFFFFFF << 4)) | (frag << 4)
    base[14:19] = v.to_bytes(5, 'big')
    return bytes(base)


try:
    RID = (2629001).to_bytes(4, 'big')
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); c.bind(('127.0.0.1', 0)); c.settimeout(1)
    srv = ('127.0.0.1', PORT)
    c.sendto(b'RPTL' + RID, srv)
    salt = c.recv(64)[6:10]
    c.sendto(b'RPTK' + RID + bytes.fromhex(sha256(salt + b'passw0rd').hexdigest()), srv)
    c.recv(64)
    rptc = (b'RPTC' + RID + b'DO9QHS  ' + b'430412500' + b'430412500' + b'01' + b'01' + b'+52.2400' + b'+008.9300'
            + b'045' + b'Porta Westfalica'.ljust(20) + b'e2e'.ljust(19) + b'4' + b''.ljust(124)
            + b'20261122'.ljust(40) + b'MMDVM_MMDVM_HS_Hat'.ljust(40))
    c.sendto(rptc, srv)
    print('login + config:', c.recv(64)[:6])
    time.sleep(0.3)

    def frame(src, dst, bits, sid, seq, payload=bytes(33)):
        return (b'DMRD' + bytes([seq]) + src.to_bytes(3, 'big') + dst.to_bytes(3, 'big') + RID + bytes([bits])
                + sid + payload + b'\x00\x00')

    print('\n1) Voice call on TG 262 (TS2) with the GPS Info LC in bursts B–E')
    frags = d.encode_embedded_lc(d.build_gps_info_lc(52.2897, 8.9161, error=1))
    seq = 0
    sid = b'\x01\x02\x03\x04'
    c.sendto(frame(2621234, 262, 0x80 | 0x21, sid, seq), srv); seq += 1      # voice header
    for sf in range(2):
        c.sendto(frame(2621234, 262, 0x80 | 0x10, sid, seq), srv); seq += 1  # burst A (voice sync)
        for vseq in range(1, 6):                                              # bursts B–F
            pl = voice_payload(frags[vseq - 1]) if vseq <= 4 else bytes(33)
            c.sendto(frame(2621234, 262, 0x80 | vseq, sid, seq, pl), srv); seq += 1
            time.sleep(0.06)
    c.sendto(frame(2621234, 262, 0x80 | 0x22, sid, seq), srv)                # terminator
    time.sleep(1.0)

    print('2) GPS data call (NMEA, rate ½) as private call to 900999 (TS2)')
    nmea = b"$GPRMC,123519,A,5201.800,N,00832.400,E,026.0,135.0,221126,,,A*6A\r\n"
    units = d.build_data_call(nmea, dst=900999, src=2634567)
    sid = b'\x05\x06\x07\x08'
    for i, u in enumerate(units):
        dtype = 6 if i == 0 else 7
        c.sendto(frame(2634567, 900999, 0x80 | 0x40 | 0x20 | dtype, sid, i, d.build_data_burst(u, dtype)), srv)
        time.sleep(0.03)
    time.sleep(1.0)

    gps = [e['data'] for e in events if e['type'] == 'gps_position']
    check(len(gps) == 2, f'dashboard received {len(gps)} gps_position events')
    emb = next((g for g in gps if g['source'] == 'embedded'), None)
    check(bool(emb) and emb['radio_id'] == 2621234 and abs(emb['lat'] - 52.2897) < 1e-4 and emb['dst_id'] == 262
          and emb['repeater_callsign'] == 'DO9QHS', 'embedded GPS: ID, position, TG and hotspot callsign')
    nm = next((g for g in gps if g['source'] == 'nmea'), None)
    check(bool(nm) and nm['radio_id'] == 2634567 and abs(nm['lat'] - 52.03) < 1e-4 and nm['dst_id'] == 900999
          and not nm['group'] and nm['course'] == 135, 'NMEA data call: ID, position, course, private call')
    log = open(f'{W}/out.log').read()
    check('📍 DAPRS embedded position from 2621234' in log, 'HBlink4 log line for the embedded position')
finally:
    proc.terminate(); proc.wait(5)
print('\nRESULT', 'OK' if ok else 'FAILED')
print(open(f'{W}/out.log').read()[-2500:] if not ok else '')
