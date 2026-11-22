"""
The APRS server of the dashboard: an APRS-IS compatible server (TCP 14580 with
filters, 10152 full feed), an uplink to the APRS-IS network, the DAPRS gateway
(GPS positions from DMR radios become APRS positions) and the data for the map.

Everything runs in the dashboard's event loop; the DMR server (HBlink4) is not
touched – it only reports positions over the existing event link
('gps_position'). Settings come from the admin area and apply live
(AprsService.reload()).

Packet flow
-----------
  client (verified) ─┐                      ┌─> clients (filter / full feed)
  APRS-IS uplink  ───┼─> dupe check ─> map ─┼─> APRS-IS uplink (not what came from there)
  DAPRS / objects ───┘    q-construct       └─> websockets of the map
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from .filters import FilterError, FilterSet, addressed_to
from .packet import (APRSError, Packet, SOFTWARE, base_call, build, object_body, parse, passcode,
                     position_body, sanitize_comment, valid_call)
from .store import AprsStore

logger = logging.getLogger("dashboard.aprs")

MAX_LINE = 512
LOGIN_TIMEOUT = 30
KEEPALIVE_S = 20
MAX_BACKLOG = 512 * 1024        # bytes waiting for a slow client before it is dropped
FLOOD_PACKETS = 40              # more packets than this in FLOOD_WINDOW seconds: disconnect
FLOOD_WINDOW = 10
PER_IP_LIMIT = 8

_LOGIN_RE = re.compile(r"^user\s+(\S+)(?:\s+pass\s+(-?\d+))?(?:\s+vers\s+(.*?))?(?:\s+(?:filter|udp)\s+(.*))?$", re.I)


def _cfg_get(cfg: Dict[str, Any], key: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


class Client:
    _next = 1

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, kind: str):
        self.id = Client._next
        Client._next += 1
        self.reader, self.writer = reader, writer
        self.kind = kind                     # 'filter' (14580) | 'full' (10152)
        peer = writer.get_extra_info("peername") or ("?", 0)
        self.ip, self.port = str(peer[0]), int(peer[1])
        self.login = ""
        self.verified = False
        self.software = ""
        self.filter = FilterSet("")
        self.connected = time.time()
        self.last_rx = time.time()
        self.rx = self.tx = 0
        self.dropped = 0
        self._recent: Deque[float] = deque(maxlen=FLOOD_PACKETS + 1)
        self.closed = False

    def send(self, line: str) -> bool:
        if self.closed:
            return False
        tr = self.writer.transport
        if tr is None or tr.is_closing():
            self.closed = True
            return False
        if tr.get_write_buffer_size() > MAX_BACKLOG:
            logger.warning(f"📡 APRS client {self.login or self.ip} too slow – disconnected")
            self.close()
            return False
        self.writer.write((line + "\r\n").encode("utf-8", "replace"))
        self.tx += 1
        return True

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.writer.close()
            except Exception:
                pass

    def flooding(self) -> bool:
        now = time.time()
        self._recent.append(now)
        return len(self._recent) > FLOOD_PACKETS and now - self._recent[0] < FLOOD_WINDOW

    def info(self) -> Dict[str, Any]:
        return {"id": self.id, "login": self.login, "verified": self.verified, "software": self.software[:60],
                "port": "full feed" if self.kind == "full" else "filter", "filter": self.filter.text[:200],
                "ip": self.ip, "connected": self.connected, "rx": self.rx, "tx": self.tx}


class AprsService:
    """APRS-IS server + uplink + DAPRS gateway. One instance per dashboard."""

    def __init__(self, cfg: Callable[[], Dict[str, Any]], data_dir, *, version: str = "",
                 callsign_of: Callable[[int], str] = lambda _id: "",
                 name_of: Callable[[int], str] = lambda _id: "",
                 repeaters: Callable[[], List[Dict[str, Any]]] = lambda: []):
        self._cfg = cfg
        self.version = version or "1"
        self.callsign_of = callsign_of
        self.name_of = name_of
        self.repeaters = repeaters
        self.store = AprsStore(data_dir / "aprs.db")
        self.clients: Dict[int, Client] = {}
        self._servers: List[asyncio.AbstractServer] = []
        self._tasks: List[asyncio.Task] = []
        self._dupes: Dict[str, float] = {}
        self._listeners: Set[asyncio.Queue] = set()
        self._daprs_last: Dict[str, float] = {}
        self._obj_sent: Dict[str, Dict[str, Any]] = {}
        self.links: List[Dict[str, Any]] = []
        self.listening: List[str] = []
        self.started = time.time()
        self.stats = {k: 0 for k in ("rx", "tx", "dupes", "invalid", "from_clients", "from_uplink", "from_daprs",
                                     "from_server", "to_uplink", "daprs_positions", "daprs_unknown",
                                     "daprs_gated", "unverified_dropped")}
        self.errors: Deque[Tuple[float, str]] = deque(maxlen=20)
        self.running = False

    # ================================================================ config
    @property
    def cfg(self) -> Dict[str, Any]:
        return self._cfg() or {}

    def c(self, key: str, default: Any = None) -> Any:
        return _cfg_get(self.cfg, key, default)

    @property
    def enabled(self) -> bool:
        return bool(self.c("aprs.enabled", True))

    @property
    def server_id(self) -> str:
        sid = str(self.c("aprs.server_id", "") or "").strip().upper()
        return sid if valid_call(sid) else ""

    @property
    def my_id(self) -> str:
        """Our name in paths (qAS,…) – the server ID, or a neutral one while none is set."""
        return self.server_id or "N8APRS"

    # ============================================================ lifecycle
    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.started = time.time()
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._housekeeping()))
        if not self.enabled:
            logger.info("📡 APRS is switched off (Admin → APRS)")
            return
        if self.c("aprs.server.enabled", False):
            if not self.server_id:
                self._error("APRS server not started: set a server ID (callsign) in Admin → APRS")
            else:
                await self._start_listeners()
        self.links = []
        for lc in self.link_configs():
            if not lc["login"]:
                self._error(f"{lc['name']} not started: set a server ID (callsign) or a login in Admin → APRS")
                continue
            link = {**lc, "server": f"{lc['host']}:{lc['port']}", "state": "connecting", "since": 0.0, "error": "",
                    "verified": False, "server_name": "", "rx": 0, "tx": 0, "writer": None}
            self.links.append(link)
            self._tasks.append(loop.create_task(self._uplink_loop(link)))
        self._tasks.append(loop.create_task(self._objects_loop()))
        if self._servers:
            self._tasks.append(loop.create_task(self._keepalive_loop()))

    async def stop(self) -> None:
        self.running = False
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        for s in self._servers:
            s.close()
        self._servers = []
        self.listening = []
        for cl in list(self.clients.values()):
            cl.close()
        self.clients.clear()
        for link in self.links:
            w = link.get("writer")
            if w:
                try:
                    w.close()
                except Exception:
                    pass
            link.update({"writer": None, "state": "off", "verified": False})
        try:
            await asyncio.to_thread(self.store.flush)
        except Exception as e:
            logger.error(f"📡 APRS database: {e}")

    async def reload(self) -> None:
        """Apply changed settings: restart listeners and uplink (stations stay)."""
        await self.stop()
        await self.start()
        logger.info("📡 APRS settings applied")

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass

    def _error(self, msg: str) -> None:
        logger.error(f"📡 {msg}")
        self.errors.append((time.time(), msg))

    # ====================================================== TCP listeners
    async def _start_listeners(self) -> None:
        bind = str(self.c("aprs.server.bind", "") or "").strip()
        ports = [(int(self.c("aprs.server.port_filter", 14580) or 0), "filter"),
                 (int(self.c("aprs.server.port_full", 10152) or 0), "full")]
        for port, kind in ports:
            if not port:
                continue
            hosts = [bind] if bind else [None]
            try:
                srv = await asyncio.start_server(lambda r, w, k=kind: self._client(r, w, k), host=hosts[0], port=port,
                                                 reuse_address=True, limit=4096)
                self._servers.append(srv)
                self.listening.append(f"{bind or '*'}:{port} ({'full feed' if kind == 'full' else 'filter'})")
                logger.info(f"📡 APRS-IS server listening on {bind or '*'}:{port} ({kind})")
            except OSError as e:
                self._error(f"APRS port {port} not available: {e}")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, kind: str) -> None:
        cl = Client(reader, writer, kind)
        max_clients = int(self.c("aprs.server.max_clients", 200) or 200)
        same_ip = sum(1 for c in self.clients.values() if c.ip == cl.ip)
        if len(self.clients) >= max_clients or same_ip >= PER_IP_LIMIT:
            writer.write(b"# server full\r\n")
            cl.close()
            return
        self.clients[cl.id] = cl
        try:
            cl.send(f"# {SOFTWARE} {self.version}")
            try:
                line = await asyncio.wait_for(reader.readline(), LOGIN_TIMEOUT)
            except asyncio.TimeoutError:
                return
            if not self._login(cl, line.decode("utf-8", "replace").strip()):
                return
            while self.running and not cl.closed:
                try:
                    raw = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    break                       # line longer than the stream limit
                if not raw:
                    break
                cl.last_rx = time.time()
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not text:
                    continue
                if text.startswith("#"):
                    self._client_command(cl, text)
                    continue
                if len(text) > MAX_LINE:
                    self.stats["invalid"] += 1
                    continue
                if not cl.verified:
                    self.stats["unverified_dropped"] += 1
                    continue
                if cl.flooding():
                    logger.warning(f"📡 APRS client {cl.login} ({cl.ip}) sends too fast – disconnected")
                    break
                cl.rx += 1
                self.ingest_line(text, "client", cl)
        except (ConnectionError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            cl.close()
            self.clients.pop(cl.id, None)
            if cl.login:
                logger.info(f"📡 APRS client {cl.login} ({cl.ip}) disconnected")

    def _login(self, cl: Client, line: str) -> bool:
        m = _LOGIN_RE.match(line)
        if not m:
            cl.send("# invalid login")
            return False
        call = m.group(1).upper()
        if not re.match(r"^[A-Z0-9]{1,9}(-[A-Z0-9]{1,2})?$", call):
            cl.send("# invalid callsign")
            return False
        cl.login = call
        cl.software = (m.group(3) or "").strip()
        try:
            pc = int(m.group(2)) if m.group(2) is not None else -1
        except ValueError:
            pc = -1
        cl.verified = pc == passcode(call) and pc >= 0
        flt = (m.group(4) or "").strip()
        if flt and cl.kind == "filter":
            try:
                cl.filter = FilterSet(flt)
            except FilterError as e:
                cl.send(f"# {e}")
        cl.send(f"# logresp {call} {'verified' if cl.verified else 'unverified'}, server {self.my_id}")
        logger.info(f"📡 APRS client {call} ({cl.ip}) {'verified' if cl.verified else 'unverified'}, "
                    f"{cl.software or 'unknown software'}{', filter ' + flt if flt else ''}")
        return True

    def _client_command(self, cl: Client, text: str) -> None:
        m = re.match(r"^#\s*filter\s*(.*)$", text, re.I)
        if m and cl.kind == "filter":
            try:
                cl.filter = FilterSet(m.group(1))
                cl.send(f"# filter {cl.filter.text} active")
            except FilterError as e:
                cl.send(f"# {e}")

    def kick(self, client_id: int) -> bool:
        cl = self.clients.get(client_id)
        if not cl:
            return False
        cl.close()
        return True

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_S)
            stamp = time.strftime("%d %b %Y %H:%M:%S GMT", time.gmtime())
            for cl in list(self.clients.values()):
                if cl.login:
                    cl.send(f"# {SOFTWARE} {self.version} {stamp} {self.my_id}")

    # ======================================================= uplink & downlink
    def link_configs(self) -> List[Dict[str, Any]]:
        """The connections of the chosen mode: APRS-IS, CB-APRS or the manual list."""
        if not self.c("aprs.uplink.enabled", False):
            return []
        mode = self.c("aprs.uplink.mode", "aprsis")
        sid = self.server_id

        def pc(login: str, raw: Any) -> int:
            raw = str(raw if raw is not None else "").strip()
            if raw:
                try:
                    return int(raw)
                except ValueError:
                    return -1
            return passcode(login) if login else -1

        if mode == "cbaprs":
            login = str(self.c("aprs.uplink.cbaprs.login", "") or "").strip().upper() or sid
            return [{"name": "CB-APRS", "host": str(self.c("aprs.uplink.cbaprs.host", "eu.cbaprs.net") or "eu.cbaprs.net"),
                     "port": int(self.c("aprs.uplink.cbaprs.port", 27235) or 27235), "login": login,
                     "passcode": pc(login, self.c("aprs.uplink.cbaprs.passcode", "")),
                     "filter": str(self.c("aprs.uplink.cbaprs.filter", "") or ""), "direction": "both"}]
        if mode == "manual":
            out = []
            for i, o in enumerate(self.c("aprs.uplink.manual", []) or [], 1):
                if not o.get("host") or o.get("enabled") is False:
                    continue
                login = str(o.get("login") or "").upper() or sid
                out.append({"name": str(o.get("name") or o["host"]), "host": str(o["host"]),
                            "port": int(o.get("port") or 14580), "login": login,
                            "passcode": pc(login, o.get("passcode")), "filter": str(o.get("filter") or ""),
                            "direction": o.get("direction", "both") if o.get("direction") in ("both", "up", "down") else "both"})
            return out
        return [{"name": "APRS-IS", "host": str(self.c("aprs.uplink.aprsis.host", "euro.aprs2.net") or "euro.aprs2.net"),
                 "port": int(self.c("aprs.uplink.aprsis.port", 14580) or 14580), "login": sid,
                 "passcode": passcode(sid) if sid else -1,
                 "filter": str(self.c("aprs.uplink.aprsis.filter", "") or ""), "direction": "both"}]

    @property
    def network_label(self) -> str:
        return {"aprsis": "APRS-IS", "cbaprs": "CB-APRS"}.get(self.c("aprs.uplink.mode", "aprsis"), "Downlink")

    @property
    def uplink(self) -> Dict[str, Any]:
        """Summary of all links (for the map and older callers)."""
        if not self.links:
            return {"state": "off", "verified": False, "server": "", "server_name": "", "error": "", "since": 0.0}
        best = max(self.links, key=lambda l: (l["state"] == "connected", l["verified"]))
        return {k: best[k] for k in ("state", "verified", "server", "server_name", "error", "since")}

    async def _uplink_loop(self, link: Dict[str, Any]) -> None:
        backoff = 5
        host, port = link["host"], link["port"]
        while self.running:
            link.update({"state": "connecting", "error": "", "verified": False})
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, limit=8192), 20)
            except (OSError, asyncio.TimeoutError) as e:
                link.update({"state": "error", "error": f"connect: {e or 'timeout'}"})
                logger.warning(f"📡 {link['name']} {host}:{port}: {e or 'timeout'} – retry in {backoff} s")
                await asyncio.sleep(backoff)
                backoff = min(300, backoff * 2)
                continue
            link["writer"] = writer
            login = f"user {link['login']} pass {link['passcode']} vers {SOFTWARE} {self.version}"
            if link["filter"] and link["direction"] != "up":
                login += f" filter {link['filter']}"
            writer.write((login + "\r\n").encode())
            link.update({"state": "connected", "since": time.time()})
            logger.info(f"📡 {link['name']} connected to {host}:{port} as {link['login']} ({link['direction']})")
            try:
                while self.running:
                    raw = await asyncio.wait_for(reader.readline(), 120)
                    if not raw:
                        raise ConnectionError("closed by server")
                    text = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if text.startswith("#"):
                        if "logresp" in text:
                            link["verified"] = " verified" in text and "unverified" not in text
                            m = re.search(r"server\s+(\S+)", text)
                            link["server_name"] = m.group(1) if m else ""
                            if not link["verified"] and link["direction"] != "down":
                                self._error(f"{link['name']} did not verify {link['login']} (passcode?) – nothing is sent up")
                        continue
                    if link["direction"] == "up":
                        continue
                    link["rx"] += 1
                    self.ingest_line(text, "uplink")
                    backoff = 5
            except (OSError, ConnectionError, asyncio.TimeoutError) as e:
                link.update({"state": "error", "error": str(e) or "timeout", "verified": False})
                logger.warning(f"📡 {link['name']} lost: {e or 'no data for 120 s'} – reconnecting")
            finally:
                link["writer"] = None
                try:
                    writer.close()
                except Exception:
                    pass
            await asyncio.sleep(backoff)

    @staticmethod
    def _can_send(link: Dict[str, Any]) -> bool:
        w = link.get("writer")
        return bool(w and link["verified"] and link["direction"] != "down" and w.transport is not None
                    and not w.transport.is_closing())

    def uplink_ready(self) -> bool:
        return any(self._can_send(l) for l in self.links)

    def _to_uplink(self, p: Packet) -> None:
        for link in self.links:
            if not self._can_send(link):
                continue
            w = link["writer"]
            if w.transport.get_write_buffer_size() > MAX_BACKLOG:
                continue
            w.write((p.raw + "\r\n").encode("utf-8", "replace"))
            link["tx"] += 1
            self.stats["to_uplink"] += 1

    # ============================================================== packets
    def _is_dupe(self, p: Packet) -> bool:
        now = time.time()
        window = int(self.c("aprs.server.dupe_seconds", 30) or 30)
        k = p.key
        last = self._dupes.get(k)
        self._dupes[k] = now if last is None or now - last >= window else last
        return last is not None and now - last < window

    def _q_construct(self, p: Packet, cl: Client) -> Optional[Packet]:
        """APRS-IS q algorithm for packets from a verified client (simplified, as aprsc does it)."""
        q, _ = p.q_construct()
        me = self.my_id
        if me in [x.rstrip("*").upper() for x in p.path]:
            return None                                  # loop
        if q:
            return p
        path = list(p.path)
        if p.src.upper() == cl.login:
            path += ["qAC", me]
        elif any(x.startswith("TCPIP") or x.startswith("TCPXX") for x in path):
            path += ["qAO", cl.login]
        else:
            path += ["qAR", cl.login]
        return p.with_path(path)

    def ingest_line(self, line: str, origin: str, cl: Optional[Client] = None) -> bool:
        try:
            p = parse(line)
        except APRSError:
            self.stats["invalid"] += 1
            return False
        if origin == "client" and cl is not None:
            p = self._q_construct(p, cl)
            if p is None:
                self.stats["invalid"] += 1
                return False
        return self.ingest(p, origin, cl)

    def ingest(self, p: Packet, origin: str, cl: Optional[Client] = None,
               extra: Optional[Dict[str, Any]] = None, gate: bool = True) -> bool:
        """Distribute one packet. origin: client | uplink | daprs | server."""
        if self._is_dupe(p):
            self.stats["dupes"] += 1
            return False
        self.stats["rx"] += 1
        self.stats["from_" + {"client": "clients"}.get(origin, origin)] = \
            self.stats.get("from_" + {"client": "clients"}.get(origin, origin), 0) + 1
        st = self.store.add(p, origin, extra)
        lookup = self.store.position_of
        for other in list(self.clients.values()):
            if other is cl or not other.login:
                continue
            if other.kind == "full" or addressed_to(p, other.login) or \
                    (other.filter and other.filter.matches(p, other.login, lookup)):
                if other.send(p.raw):
                    self.stats["tx"] += 1
        if origin != "uplink" and gate:
            self._to_uplink(p)
        self._notify({"type": "packet", "packet": {"ts": time.time(), "call": p.station, "src": p.src,
                                                   "type": p.type, "origin": origin, "raw": p.raw}})
        if st is not None:
            from .store import public
            self._notify({"type": "station", "station": public(st)})
        return True

    # ============================================================ listeners
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._listeners.discard(q)

    def _notify(self, msg: Dict[str, Any]) -> None:
        for q in list(self._listeners):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    # ================================================================ DAPRS
    def _overrides(self) -> Dict[int, Dict[str, Any]]:
        out = {}
        for o in self.c("daprs.overrides", []) or []:
            try:
                out[int(o.get("dmr_id"))] = o
            except (TypeError, ValueError):
                continue
        return out

    def on_gps(self, ev: Dict[str, Any]) -> Optional[Packet]:
        """'gps_position' event from HBlink4 → APRS position (map, clients, APRS-IS)."""
        if not self.running or not self.enabled or not self.c("daprs.enabled", True):
            return None
        try:
            rid = int(ev.get("radio_id") or 0)
            lat, lon = float(ev["lat"]), float(ev["lon"])
        except (TypeError, ValueError, KeyError):
            return None
        self.stats["daprs_positions"] += 1
        ov = self._overrides().get(rid, {})
        if ov.get("off"):
            return None
        call = str(ov.get("callsign") or self.callsign_of(rid) or "").upper().strip()
        if not call or not valid_call(call):
            self.stats["daprs_unknown"] += 1
            logger.info(f"📍 DAPRS: no callsign for DMR ID {rid} – position not used")
            return None
        call = base_call(call)
        ssid = ov.get("ssid", self.c("daprs.ssid", 7))
        try:
            ssid = int(ssid)
        except (TypeError, ValueError):
            ssid = 7
        src = f"{call}-{ssid}" if 0 < ssid <= 15 else call
        if not valid_call(src):
            return None
        symbol = str(ov.get("symbol") or self.c("daprs.symbol", "/[") or "/[")[:2]
        if len(symbol) != 2:
            symbol = "/["
        tg = int(ev.get("dst_id") or 0) if ev.get("group", True) else 0
        values = {"tg": tg or "", "slot": ev.get("slot", ""), "repeater": ev.get("repeater_callsign", ""),
                  "dmrid": rid, "server": self.my_id, "source": ev.get("source", "")}
        template = str(ov.get("comment") or self.c("daprs.comment", "DMR Die N8schicht TG{tg}") or "")
        try:
            comment = template.format(**values)
        except (KeyError, ValueError, IndexError):
            comment = template
        comment = re.sub(r"\s*TG\s*$", "", sanitize_comment(comment))
        body = position_body(lat, lon, symbol, comment, course=ev.get("course"), speed_kmh=ev.get("speed_kmh"),
                             altitude_m=ev.get("altitude_m"))
        try:
            p = build(src, body, ["DMR*", "qAS", self.my_id])
        except APRSError:
            return None
        now = time.time()
        interval = int(self.c("daprs.min_interval", 60) or 60)
        auth = str(ev.get("auth_method") or "")
        gate = (bool(self.c("daprs.to_aprsis", True)) and self.uplink_ready()
                and now - self._daprs_last.get(src, 0) >= interval)
        if gate and self.c("daprs.block_guests", True) and auth == "guest":
            gate = False
        if gate:
            self._daprs_last[src] = now
            self.stats["daprs_gated"] += 1
        extra = {"dmr_id": rid, "dmr_name": (self.name_of(rid) or "")[:40], "tg": tg or None,
                 "via": ev.get("repeater_callsign", ""), "gps": ev.get("source", ""), "auth": auth}
        self.ingest(p, "daprs", extra=extra, gate=gate)
        return p

    # ------------------------------------------------- repeaters & hotspots
    @staticmethod
    def object_name(callsign: str, radio_id: int, ctype: str) -> str:
        call = base_call(callsign)[:6]
        if ctype == "repeater":
            return call
        suffix = str(radio_id)[-2:] if radio_id > 9999999 else "HS"
        return f"{call}-{suffix}"[:9]

    def _object_for(self, r: Dict[str, Any]) -> Optional[Tuple[str, Packet, Dict[str, Any]]]:
        ctype = r.get("connection_type") or ""
        if ctype == "repeater" and not self.c("daprs.objects.repeaters", True):
            return None
        if ctype == "hotspot" and not self.c("daprs.objects.hotspots", False):
            return None
        if ctype not in ("repeater", "hotspot"):
            return None
        try:
            lat = float(str(r.get("latitude", "")).strip() or "nan")
            lon = float(str(r.get("longitude", "")).strip() or "nan")
        except ValueError:
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (abs(lat) < 0.001 and abs(lon) < 0.001):
            return None
        call = str(r.get("callsign") or "").upper()
        if not re.match(r"^[A-Z0-9]{3,7}$", call):
            return None
        rid = int(r.get("repeater_id") or 0)
        name = self.object_name(call, rid, ctype)
        freq = ""
        try:
            tx = int(str(r.get("tx_freq", "")).strip() or 0)
            rx = int(str(r.get("rx_freq", "")).strip() or 0)
            if tx > 1000000:
                freq = f"{tx / 1e6:.3f}MHz"
                if rx and abs(rx - tx) >= 10000:
                    freq += f" {(rx - tx) // 10000:+04d}"
        except ValueError:
            pass
        cc = str(r.get("colorcode", "")).strip()
        parts = [freq, f"CC{cc}" if cc.isdigit() else "", str(self.c("daprs.objects.comment", "DMR Die N8schicht") or "")]
        comment = sanitize_comment(" ".join(x for x in parts if x))
        symbol = str(self.c(f"daprs.objects.{'repeater' if ctype == 'repeater' else 'hotspot'}_symbol",
                            "/r" if ctype == "repeater" else "/n") or "/r")[:2]
        body = object_body(name, lat, lon, symbol if len(symbol) == 2 else "/r", comment)
        p = build(self.my_id, body, ["TCPIP*", "qAS", self.my_id])
        extra = {"dmr_id": rid, "rpt_type": ctype, "freq": freq, "via": r.get("location", "")}
        return name, p, extra

    def object_pass(self) -> int:
        """Send due objects for repeaters/hotspots; kill objects of stations that left. Returns how many sent."""
        if not self.running or not self.enabled or not self.c("daprs.enabled", True):
            return 0
        interval = max(5, int(self.c("daprs.objects.interval_min", 30) or 30)) * 60
        uplink = bool(self.c("daprs.objects.to_aprsis", False))
        now = time.time()
        seen = set()
        sent = 0
        for r in self.repeaters():
            res = self._object_for(r)
            if not res:
                continue
            name, p, extra = res
            seen.add(name)
            last = self._obj_sent.get(name)
            pos = (round(p.lat, 4), round(p.lon, 4), p.comment)
            if last and last["pos"] == pos and now - last["ts"] < interval:
                continue
            self._dupes.pop(p.key, None)
            if self.ingest(p, "server", extra=extra, gate=uplink):
                sent += 1
            self._obj_sent[name] = {"ts": now, "pos": pos, "packet": p, "uplink": uplink}
        for name in [n for n in self._obj_sent if n not in seen]:
            info = self._obj_sent.pop(name)
            old: Packet = info["packet"]
            body = object_body(name, old.lat, old.lon, old.symbol, old.comment, alive=False)
            try:
                kill = build(self.my_id, body, ["TCPIP*", "qAS", self.my_id])
                self.ingest(kill, "server", gate=info.get("uplink", False))
            except APRSError:
                pass
        return sent

    async def _objects_loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                self.object_pass()
            except Exception as e:
                logger.error(f"📡 APRS objects: {e}")
            await asyncio.sleep(60)

    # ======================================================== housekeeping
    async def _housekeeping(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(3)
            n += 1
            try:
                await asyncio.to_thread(self.store.flush)
            except Exception as e:
                logger.error(f"📡 APRS database: {e}")
            if n % 20 == 0:
                now = time.time()
                window = int(self.c("aprs.server.dupe_seconds", 30) or 30)
                self._dupes = {k: v for k, v in self._dupes.items() if now - v < window}
            if n % 1200 == 1:            # at start, then hourly
                try:
                    await asyncio.to_thread(self.purge)
                except Exception as e:
                    logger.error(f"📡 APRS purge: {e}")

    MAX_DAYS = 90       # nothing older is kept in SQLite

    def purge(self) -> None:
        days = lambda k, d: min(self.MAX_DAYS, max(1.0, float(self.c(k, d) or d)))
        self.store.purge(days("aprs.map.station_days", 30), days("aprs.map.track_days", 30),
                         days("aprs.map.packet_days", 7) * 24)

    # ================================================================ status
    def status(self, admin: bool = False) -> Dict[str, Any]:
        sid = self.server_id
        out = {
            "enabled": self.enabled,
            "running": self.running,
            "server_id": sid,
            "passcode": passcode(sid) if (sid and admin) else None,
            "server_enabled": bool(self.c("aprs.server.enabled", False)),
            "listening": self.listening,
            "clients": len(self.clients),
            "uplink": self.uplink,
            "uplink_enabled": bool(self.c("aprs.uplink.enabled", False)),
            "uplink_mode": self.c("aprs.uplink.mode", "aprsis"),
            "network": self.network_label,
            "links": [{k: v for k, v in l.items() if k not in ("writer", "passcode")} for l in self.links],
            "stats": dict(self.stats),
            "counts": self.store.counts(),
            "objects": sorted(self._obj_sent),
            "started": self.started,
            "errors": [{"ts": t, "text": m} for t, m in self.errors],
            "version": self.version,
        }
        if admin:
            out["client_list"] = [c.info() for c in self.clients.values()]
        return out

