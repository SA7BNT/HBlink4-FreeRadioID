"""End-to-end check: real HBlink4, a HomeBrew client like Z3DMR and two classic OpenBridge peers on localhost.

Run: python3 scripts/e2e_openbridge.py   (uses UDP ports 62131 and 62140-62151, takes ~15 s)
"""
import json, os, socket, subprocess, sys, time
from hashlib import sha256
from hmac import new as hmac_new
from hashlib import sha1

REPO = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = '/tmp/hblink4-e2e'
os.makedirs(W, exist_ok=True)
for f in os.listdir(W):
    os.remove(os.path.join(W, f))
PORT, A_LOCAL, B_LOCAL, A_PEER, B_PEER = 62131, 62140, 62141, 62150, 62151
PASS_OBP = 'test-obp-pass'
cfg = {
    "global": {"bind_ipv4": "127.0.0.1", "port_ipv4": PORT, "disable_ipv6": True,
               "settings_db": f"{W}/db.db", "logging": {"file": f"{W}/hb.log", "console_level": "INFO",
                                                         "file_level": "DEBUG", "retention_days": 1}},
    "dashboard": {"enabled": False},
    "repeater_configurations": {"default": {"passphrase": "passw0rd",
                                            "slot1_talkgroups": [262, 2620, 91], "slot2_talkgroups": [262, 2620, 91]}},
    "freeradioid": {"enabled": False},
    "openbridge_connections": [
        {"name": "OBP-A", "network_id": 2624, "local_address": "127.0.0.1", "local_port": A_LOCAL,
         "target_address": "127.0.0.1", "target_port": A_PEER, "passphrase": PASS_OBP,
         "all_talkgroups": True, "default_slot": 2},
        {"name": "OBP-B", "network_id": 2625, "local_address": "127.0.0.1", "local_port": B_LOCAL,
         "target_address": "127.0.0.1", "target_port": B_PEER, "passphrase": PASS_OBP,
         "talkgroup_slots": {"262": "2", "2620": "2"}},
    ],
}
json.dump(cfg, open(f'{W}/config.json', 'w'))
proc = subprocess.Popen([sys.executable, 'run_hblink.py', f'{W}/config.json'], cwd=REPO,
                        stdout=open(f'{W}/out.log', 'w'), stderr=subprocess.STDOUT)
time.sleep(3)


def sock(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', port)); s.settimeout(0.3)
    return s


def drain(s, secs=1.0):
    out, end = [], time.time() + secs
    while time.time() < end:
        try:
            out.append(s.recv(2048))
        except socket.timeout:
            pass
    return out


ok = True
def check(cond, msg):
    global ok
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    ok = ok and cond


try:
    # ---- client logs in like Z3DMR
    RID = (2629001).to_bytes(4, 'big')
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); c.bind(('127.0.0.1', 0)); c.settimeout(1)
    srv = ('127.0.0.1', PORT)
    c.sendto(b'RPTL' + RID, srv)
    ack = c.recv(64); salt = ack[6:10]
    c.sendto(b'RPTK' + RID + bytes.fromhex(sha256(salt + b'passw0rd').hexdigest()), srv)
    print('login:', c.recv(64)[:6])
    rptc = (b'RPTC' + RID + b'DL9APP  ' + b'433550000' + b'433550000' + b'01' + b'01' + b'+52.0000' + b'+008.9000'
            + b'045' + b'Minden'.ljust(20) + b'Z3DMR test'.ljust(19) + b'4' + b''.ljust(124)
            + b'20261118'.ljust(40) + b'Z3DMR'.ljust(40))
    assert len(rptc) == 302, len(rptc)
    c.sendto(rptc, srv)
    print('config:', c.recv(64)[:6])
    time.sleep(0.5)
    pa, pb = sock(A_PEER), sock(B_PEER)

    def obp_frame(dst, bits, sid, seq):
        d = (b'DMRD' + bytes([seq]) + (2621234).to_bytes(3, 'big') + dst.to_bytes(3, 'big')
             + (2624).to_bytes(4, 'big') + bytes([bits]) + sid + bytes(33))
        return d + hmac_new(PASS_OBP.encode(), d, sha1).digest()

    def burst(send, dst, sid, mk):
        frames = [0x21] + [0x10 if i % 6 == 0 else i % 6 for i in range(1, 13)] + [0x22]
        for i, b in enumerate(frames):
            send(mk(dst, b, sid, i)); time.sleep(0.06)

    print('\n1) Call from OpenBridge A on TG 262 → client (Z3DMR) and transit to OBP B')
    c.settimeout(0.2)
    burst(lambda p: pa.sendto(p, ('127.0.0.1', A_LOCAL)), 262, b'\x11\x22\x33\x44', obp_frame)
    got = [p for p in drain(c, 1.2) if p[:4] == b'DMRD']
    check(len(got) >= 10, f'client received {len(got)} DMRD frames')
    check(got and all(len(p) == 55 for p in got), f'all frames 55 bytes (lengths: {sorted({len(p) for p in got})})')
    gb = [p for p in drain(pb, 0.5) if p[:4] == b'DMRD']
    check(len(gb) >= 10, f'OBP B (transit) received {len(gb)} frames')
    ga = [p for p in drain(pa, 0.3) if p[:4] == b'DMRD']
    check(len(ga) == 0, 'nothing went back to OBP A')
    time.sleep(4)   # hang time

    print('\n2) Client (Z3DMR) talks on TG 2620 → both trunks in parallel')
    def hb_frame(dst, bits, sid, seq):
        return (b'DMRD' + bytes([seq]) + (2629001).to_bytes(3, 'big') + dst.to_bytes(3, 'big')
                + RID + bytes([bits]) + sid + bytes(33) + b'\x00\x00')
    burst(lambda p: c.sendto(p, srv), 2620, b'\x55\x66\x77\x88', hb_frame)
    ga = [p for p in drain(pa, 1.0) if p[:4] == b'DMRD']
    gb = [p for p in drain(pb, 0.5) if p[:4] == b'DMRD']
    check(len(ga) >= 10, f'OBP A received {len(ga)} frames')
    check(len(gb) >= 10, f'OBP B received {len(gb)} frames')
    check(all(len(p) == 73 for p in ga + gb), 'classic OBP frames are 73 bytes (53 + HMAC)')
    time.sleep(4)

    print('\n3) Echo: our stream comes back over OBP B → dropped')
    burst(lambda p: pb.sendto(p, ('127.0.0.1', B_LOCAL)), 2620, b'\x55\x66\x77\x88', obp_frame)
    got = [p for p in drain(c, 1.0) if p[:4] == b'DMRD']
    check(len(got) == 0, f'client got {len(got)} frames of the echo')
finally:
    proc.terminate(); proc.wait(5)
print('\nRESULT', 'OK' if ok else 'FAILED')
print(open(f'{W}/out.log').read()[-2500:] if not ok else '')
