"""
HBlink4 Dashboard - Separate Process
FastAPI + Uvicorn + WebSockets for real-time monitoring

Updates every 10 superframes (60 packets = 1 second) for smooth real-time feel
"""
import asyncio
import json
import socket
import os
import ipaddress
import sys
import atexit
from datetime import datetime, date, timedelta
from collections import deque
from typing import Dict, List, Set, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Body
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import logging

# Allow this file to be run directly by path (python dashboard/server.py) as
# well as imported. Establishing the package context means user_db is imported
# once, as dashboard.user_db, rather than a second bare copy under sys.path
# surgery.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).parent.parent))
    __package__ = "dashboard"
    import dashboard  # noqa: F401  (parent package must be in sys.modules)

from .user_db import UserDatabase, compute_next_refresh_seconds, _age_str
from .auth import AuthStore, AuthError, ADMIN_ROLES, redact_ips, sniff_image
from .settings import SettingsStore, SettingsError, FIELD_BY_KEY
from .avatars import AvatarStore, DEFAULT_URL as FRID_AVATAR_URL

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="DMR Die N8schicht Dashboard", version="1.1.0")

# Load dashboard configuration
def load_config() -> dict:
    """Load dashboard configuration from config.json"""
    config_path = Path(__file__).parent / "config.json"
    default_config = {
        "server_name": "dmr.die-n8schicht.de",
        "server_description": "Amateur Radio DMR Network",
        "dashboard_title": "DMR Die N8schicht",
        "refresh_interval": 1000,
        "max_events": 50,
        "web": {
            "bind": "0.0.0.0",
            "port": 8080
        },
        # Addresses we LISTEN on for hblink4's incoming event connection.
        # bind_* = we listen here (host_* would mean we dial out, which we never do).
        "event_receiver": {
            "transport": "unix",
            "bind_ipv4": "127.0.0.1",
            "bind_ipv6": "::1",
            "port": 8765,
            "unix_socket": "/tmp/hblink4.sock",
            "disable_ipv6": False
        },
        "user_database": {
            "enabled": True,
            "source_url": "https://database.radioid.net/static/user.csv",
            "user_agent": "HBlink4-Dashboard (contact=operator@example.org)",
            "refresh": {
                "schedule": "daily",
                "time_of_day": "03:17",
                "jitter_minutes": 15,
                "on_startup_if_older_than_hours": 36
            },
            "filter": {
                "countries": ["United States", "Canada"],
                "callsign_regex": None,
                "radio_id_ranges": None
            }
        },
        # Login, user management and who may see what
        "auth": {
            "superadmin": {"username": "", "password": "", "password_hash": ""},
            "public_dashboard": True,
            "session_hours": 12,
            "remember_days": 30
        },
        # HTTPS with automatic Let's Encrypt certificates (see dashboard/tls.py)
        "https": {
            "enabled": False,
            "domain": "",
            "email": "",
            "port": 443,
            "http_port": 80,
            "letsencrypt": True,
            "staging": False,
            "cert_file": "",
            "key_file": ""
        },
        # FreeRadioID.net integration: avatars and live profile lookups
        "freeradioid": {
            "avatar_url": "https://freeradioid.net/api/idimg-{id}.png",
            "profile_lookup": True,
            "profile_url": "https://freeradioid.net/api/dmr/user/?id={id}"
        }
    }
    
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
                # Merge with defaults (in case new keys are added)
                return {**default_config, **config}
        except Exception as e:
            logger.warning(f"Failed to load config.json: {e}, using defaults")
            return default_config
    else:
        # Create default config file
        try:
            with open(config_path, 'w') as f:
                json.dump(default_config, f, indent=4)
            logger.info(f"Created default config at {config_path}")
        except Exception as e:
            logger.warning(f"Failed to create config.json: {e}")
        return default_config

dashboard_config = load_config()
# Untouched copy of config.json (+ defaults): "reset to config.json" in the admin area
import copy as _copy
_file_config = _copy.deepcopy(dashboard_config)

def _ensure_writable(data_dir: Path) -> None:
    """
    The dashboard must own its data files. A start as root (run_all.sh, or the
    server by hand) leaves root-owned files behind that the service user cannot
    write -- SQLite then fails with "attempt to write a readonly database".
    Explain that with the exact fix instead of a traceback, and stop.
    """
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    bad = []
    if not os.access(data_dir, os.W_OK):
        bad.append(data_dir)
    for f in data_dir.iterdir() if data_dir.is_dir() else []:
        if f.is_file() and not os.access(f, os.W_OK):
            bad.append(f)
    if not bad:
        return
    import getpass
    try:
        me = getpass.getuser()
    except Exception:
        me = str(os.getuid())
    root = data_dir.parent.parent
    logger.error("❌ The dashboard (running as %s) cannot write these files:", me)
    for f in bad[:10]:
        try:
            owner = f.owner()
        except Exception:
            owner = "?"
        logger.error(f"     {f}  (owner: {owner})")
    logger.error("   They were probably created by a start as root. Fix (as root):")
    logger.error(f"     chown -R {me}:{me} {root}")
    logger.error("   and from now on start only the services (systemctl), or run_all.sh as that user.")
    sys.exit(1)


# In-memory state (could be Redis/database for persistence)
class DashboardState:
    """Maintains state for the dashboard"""
    def __init__(self):
        self.repeaters: Dict[int, dict] = {}
        self.repeater_details: Dict[int, dict] = {}  # Detailed info (sent once per connection)
        self.outbounds: Dict[str, dict] = {}  # Outbound connections (key: connection_name)
        self.openbridges: Dict[str, dict] = {}  # OpenBridge trunks (key: connection_name)
        self.streams: Dict[str, dict] = {}  # key: f"{repeater_id}.{slot}" (OBP: f"{name}.{stream_id}")
        self.events: deque = deque(maxlen=500)  # Ring buffer of recent events
        self.last_heard: List[dict] = []  # Last heard users
        self.last_heard_stats: dict = {}  # User cache statistics
        self.websocket_clients: Set[WebSocket] = set()
        self.hblink_connected: bool = False  # Track HBlink4 connection status
        self.stats = {
            'total_calls_today': 0,      # Total RX calls (streams) received today
            'total_duration_today': 0.0,  # Total duration of RX streams only (seconds)
            'retransmitted_calls': 0,    # Total calls retransmitted today (may be higher than received)
            'start_time': datetime.now().isoformat(),
            'last_reset_date': date.today().isoformat()  # Track when stats were last reset
        }
        
        # Persistence file paths
        # HBLINK4_DASH_DATA_DIR: tests point this at a temporary directory
        self._data_dir = Path(os.environ.get("HBLINK4_DASH_DATA_DIR") or Path(__file__).parent / "data")
        self._stats_file = self._data_dir / "stats.json"
        self._last_heard_file = self._data_dir / "last_heard.json"
        self._persistence_disabled = False  # Will be set to True if write permissions fail

        # User database (radio_id -> callsign) with background refresh.
        # Loaded from disk snapshot on startup; populated/refreshed by
        # user_db_refresh_task() registered in startup_event().
        self.user_db = UserDatabase(self._data_dir)

        # Users, sessions, audit log and settings (SQLite)
        _ensure_writable(self._data_dir)
        self.auth = AuthStore(self._data_dir / "dashboard.db")
        self.auth.sync_superadmin((dashboard_config.get("auth") or {}).get("superadmin") or {})

        # FreeRadioID avatars, cached in the same database
        self.avatars = AvatarStore(
            self.auth, lambda: (dashboard_config.get("freeradioid") or {}).get("avatar_url", FRID_AVATAR_URL))
        try:   # the file cache of an earlier version is no longer used
            import shutil as _shutil
            _shutil.rmtree(self._data_dir / "avatars", ignore_errors=True)
        except Exception:
            pass

        # Settings: seeded from config.json on the first start, SQLite afterwards
        self.settings = SettingsStore(self.auth)
        seeded = self.settings.seed_and_apply(dashboard_config)
        if seeded:
            logger.info(f"⚙️ {seeded} settings taken over from config.json into the database; "
                        "from now on they are edited in the admin area")

        # Load persisted data
        self._load_persisted_data()
    
    def reset_daily_stats(self):
        """Reset daily statistics at midnight"""
        self.stats['total_calls_today'] = 0
        self.stats['total_duration_today'] = 0.0
        self.stats['retransmitted_calls'] = 0
        self.stats['last_reset_date'] = date.today().isoformat()
        logger.info(f"📊 Daily stats reset at midnight (server time)")
    
    def _load_persisted_data(self):
        """Load persisted statistics and last heard data on startup, purge old data"""
        try:
            # Test write permissions early
            self._check_write_permissions()
            self._cleanup_temp_files()
            self._load_stats()
            self._load_last_heard()
            self.user_db.load_from_disk()
            self._purge_old_data()
            logger.info("📁 Loaded persisted dashboard data")
        except PermissionError as e:
            logger.error(f"❌ No write permissions for persistence directory: {e}")
            logger.error("💡 Dashboard will run without persistence - restart data will not be saved")
            self._persistence_disabled = True
        except Exception as e:
            logger.warning(f"⚠️ Could not load persisted data: {e}")
    
    def _check_write_permissions(self):
        """Test write permissions to data directory"""
        try:
            # Create data directory if needed
            self._data_dir.mkdir(exist_ok=True)
            
            # Test write permissions with a temp file
            test_file = self._data_dir / ".write_test"
            test_file.write_text("test")
            test_file.unlink()
            
            # Set flag to enable persistence
            self._persistence_disabled = False
            
        except (PermissionError, OSError) as e:
            logger.error(f"❌ Cannot write to persistence directory {self._data_dir}: {e}")
            raise PermissionError(f"No write access to {self._data_dir}")
    
    def _cleanup_temp_files(self):
        """Remove any orphaned temp files from previous unclean shutdown"""
        try:
            if not self._data_dir.exists():
                return
                
            removed_count = 0
            for temp_file in self._data_dir.glob("*.tmp"):
                try:
                    temp_file.unlink()
                    removed_count += 1
                    logger.debug(f"🗑️ Removed orphaned temp file: {temp_file.name}")
                except OSError as e:
                    logger.warning(f"⚠️ Could not remove temp file {temp_file.name}: {e}")
            
            if removed_count > 0:
                logger.info(f"🧹 Cleaned up {removed_count} orphaned temp file(s) from previous session")
                
        except Exception as e:
            logger.warning(f"⚠️ Could not cleanup temp files: {e}")
    
    def _purge_old_data(self):
        """Remove old data files to prevent accumulation"""
        if self._persistence_disabled:
            return
            
        try:
            # Check if stats file is from a previous day and remove it
            if self._stats_file.exists():
                try:
                    with open(self._stats_file, 'r') as f:
                        saved_stats = json.load(f)
                    
                    today = date.today().isoformat()
                    if saved_stats.get('last_reset_date') != today:
                        self._stats_file.unlink()
                        logger.info("🗑️ Removed old stats file from previous day")
                except (json.JSONDecodeError, KeyError) as e:
                    # If we can't read it, try to remove it
                    try:
                        self._stats_file.unlink()
                        logger.info("🗑️ Removed corrupted stats file")
                    except OSError as remove_error:
                        logger.warning(f"⚠️ Could not remove corrupted stats file: {remove_error}")
                except OSError as e:
                    logger.warning(f"⚠️ Could not read/remove old stats file: {e}")
                
        except Exception as e:
            logger.warning(f"⚠️ Could not purge old data: {e}")
    
    def _load_stats(self):
        """Load statistics from file, validate date, reset if needed"""
        if not self._stats_file.exists():
            logger.debug("No stats file found, using fresh stats")
            return
        
        try:
            with open(self._stats_file, 'r') as f:
                saved_stats = json.load(f)
            
            # Check if stats are from today
            today = date.today().isoformat()
            if saved_stats.get('last_reset_date') == today:
                # Stats are still valid for today, load them
                self.stats.update(saved_stats)
                logger.info(f"📊 Loaded today's stats: {self.stats['total_calls_today']} calls, {self.stats['retransmitted_calls']} retransmitted")
            else:
                # Stats are from a previous day, keep fresh stats but log
                logger.info(f"📊 Previous day's stats found ({saved_stats.get('last_reset_date')}), using fresh daily stats")
        
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"⚠️ Could not parse stats file: {e}")
    
    def _load_last_heard(self):
        """Load last heard users, filtering out entries older than 4 hours (more aggressive)"""
        if not self._last_heard_file.exists():
            logger.debug("No last heard file found, starting fresh")
            return
        
        try:
            with open(self._last_heard_file, 'r') as f:
                saved_data = json.load(f)
            
            # More aggressive filtering - only keep entries from last 4 hours
            cutoff_time = datetime.now() - timedelta(hours=4)
            valid_users = []
            
            for user in saved_data.get('users', []):
                try:
                    last_seen = datetime.fromisoformat(user['last_seen'])
                    if last_seen > cutoff_time:
                        valid_users.append(user)
                except (ValueError, KeyError):
                    continue  # Skip malformed entries
            
            # Only keep data if we have recent users, otherwise start fresh
            if valid_users:
                self.last_heard = valid_users
                self.last_heard_stats = saved_data.get('stats', {})
                logger.info(f"📻 Loaded {len(valid_users)} recent users from last heard")
            else:
                logger.info("📻 No recent users found, starting fresh last heard list")
        
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"⚠️ Could not parse last heard file: {e}")
    
    def save_stats(self):
        """Save current statistics to file with atomic write (called only on shutdown)"""
        if self._persistence_disabled:
            logger.debug("📁 Persistence disabled - skipping stats save")
            return
            
        try:
            self._data_dir.mkdir(exist_ok=True)
            temp_file = self._stats_file.with_suffix('.tmp')
            
            with open(temp_file, 'w') as f:
                json.dump(self.stats, f, indent=2)
            
            # Atomic operation - replace file only if write succeeded
            temp_file.rename(self._stats_file)
            logger.debug(f"📁 Saved stats to {self._stats_file}")
            
        except PermissionError as e:
            logger.error(f"❌ Permission denied saving stats: {e}")
            logger.error("💡 Dashboard will exit gracefully without saving persistence data")
        except Exception as e:
            logger.error(f"❌ Failed to save stats: {e}")
    
    def save_last_heard(self):
        """Save last heard users and stats to file with atomic write (called only on shutdown)"""
        if self._persistence_disabled:
            logger.debug("📁 Persistence disabled - skipping last heard save")
            return
            
        try:
            self._data_dir.mkdir(exist_ok=True)
            temp_file = self._last_heard_file.with_suffix('.tmp')
            
            data = {
                'timestamp': datetime.now().isoformat(),
                'users': self.last_heard,
                'stats': self.last_heard_stats
            }
            
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            # Atomic operation - replace file only if write succeeded
            temp_file.rename(self._last_heard_file)
            logger.debug(f"📁 Saved last heard to {self._last_heard_file}")
            
        except PermissionError as e:
            logger.error(f"❌ Permission denied saving last heard: {e}")
            logger.error("💡 Dashboard will exit gracefully without saving persistence data")
        except Exception as e:
            logger.error(f"❌ Failed to save last heard: {e}")
    
    def save_all_data(self):
        """Save all persistent data"""
        self.save_stats()
        self.save_last_heard()

state = DashboardState()


async def broadcast_hblink_status(connected: bool):
    """Broadcast HBlink4 connection status to all WebSocket clients"""
    state.hblink_connected = connected
    message = {
        'type': 'hblink_status',
        'data': {
            'connected': connected,
            'timestamp': datetime.now().isoformat()
        }
    }
    
    # Broadcast to all connected clients
    disconnected_clients = set()
    for client in state.websocket_clients:
        try:
            await client.send_json(message)
            
            # If HBlink just connected, also send full current state to ensure browser has everything
            if connected:
                await client.send_json({
                    'type': 'initial_state',
                    'data': {
                        'repeaters': list(state.repeaters.values()),
                        'repeater_details': state.repeater_details,
                        'outbounds': list(state.outbounds.values()),
                        'openbridges': list(state.openbridges.values()),
                        'streams': list(state.streams.values()),
                        'events': list(state.events)[-50:],
                        'stats': state.stats,
                        'last_heard': state.last_heard,
                        'hblink_connected': state.hblink_connected
                    }
                })
        except Exception as e:
            logger.debug(f"Failed to send status to client: {e}")
            disconnected_clients.add(client)
    
    # Clean up disconnected clients
    state.websocket_clients -= disconnected_clients


class TCPProtocol(asyncio.Protocol):
    """TCP protocol handler for receiving events from hblink4"""
    
    def __init__(self, callback):
        self.callback = callback
        self.buffer = b''
        self.transport = None
    
    def connection_made(self, transport):
        """Called when TCP connection established"""
        peername = transport.get_extra_info('peername')
        logger.info(f"✅ HBlink4 connected via TCP from {peername}")
        self.transport = transport

        # Tune TCP keepalive on the accepted socket so a silently-severed hblink4
        # connection (NIC flap, DHCP renew, firewall/conntrack eviction) is
        # detected within ~2 minutes and fires connection_lost, instead of the
        # dashboard holding a dead connection (hblink_connected stuck True) until
        # hblink4 happens to reconnect.
        sock = transport.get_extra_info('socket')
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                if hasattr(socket, 'TCP_KEEPIDLE'):
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
            except OSError as e:
                logger.debug(f"Could not tune TCP keepalive on accepted socket: {e}")
        
        # Clear dashboard state on reconnect
        # HBlink4 will re-send all current repeaters via repeater_connected events
        logger.info("🔄 Clearing dashboard state - requesting HBlink4 state sync")
        state.repeaters.clear()
        state.outbounds.clear()
        state.openbridges.clear()
        state.streams.clear()
        
        # Set connection status synchronously (async broadcast may be delayed)
        state.hblink_connected = True
        
        # Send sync request to HBlink4 to trigger initial state send
        try:
            sync_request = json.dumps({'type': 'sync_request'}).encode('utf-8')
            length = len(sync_request)
            frame = length.to_bytes(4, byteorder='big') + sync_request
            transport.write(frame)
            logger.info("📤 Sent sync_request to HBlink4")
        except Exception as e:
            logger.error(f"Failed to send sync_request: {e}")
        
        # Notify all browser clients that HBlink4 is connected
        asyncio.create_task(broadcast_hblink_status(True))
    
    def data_received(self, data):
        """Called when TCP data received (handles framing)"""
        self.buffer += data
        
        # Process all complete frames in buffer
        while len(self.buffer) >= 4:
            # Read length prefix (4 bytes, big-endian)
            length = int.from_bytes(self.buffer[:4], byteorder='big')
            
            # Check if we have complete frame
            if len(self.buffer) < 4 + length:
                break  # Wait for more data
            
            # Extract frame
            frame = self.buffer[4:4+length]
            self.buffer = self.buffer[4+length:]
            
            # Process event
            asyncio.create_task(self.callback(frame))
    
    def connection_lost(self, exc):
        """Called when TCP connection lost"""
        if exc:
            logger.warning(f"⚠️ HBlink4 TCP connection lost: {exc}")
        else:
            logger.info("HBlink4 TCP connection closed")
        
        # Set connection status synchronously (async broadcast may be delayed)
        state.hblink_connected = False
        
        # Notify all browser clients that HBlink4 is disconnected
        asyncio.create_task(broadcast_hblink_status(False))


class UnixProtocol(asyncio.Protocol):
    """Unix socket protocol handler for receiving events from hblink4"""
    
    def __init__(self, callback):
        self.callback = callback
        self.buffer = b''
        self.transport = None
    
    def connection_made(self, transport):
        """Called when Unix socket connection established"""
        logger.info(f"✅ HBlink4 connected via Unix socket")
        self.transport = transport
        
        # Clear dashboard state on reconnect
        # HBlink4 will re-send all current repeaters via repeater_connected events
        logger.info("🔄 Clearing dashboard state - requesting HBlink4 state sync")
        state.repeaters.clear()
        state.outbounds.clear()
        state.openbridges.clear()
        state.streams.clear()
        
        # Set connection status synchronously (async broadcast may be delayed)
        state.hblink_connected = True
        
        # Send sync request to HBlink4 to trigger initial state send
        try:
            sync_request = json.dumps({'type': 'sync_request'}).encode('utf-8')
            length = len(sync_request)
            frame = length.to_bytes(4, byteorder='big') + sync_request
            transport.write(frame)
            logger.info("📤 Sent sync_request to HBlink4")
        except Exception as e:
            logger.error(f"Failed to send sync_request: {e}")
        
        # Notify all browser clients that HBlink4 is connected
        asyncio.create_task(broadcast_hblink_status(True))
    
    def data_received(self, data):
        """Called when data received (handles framing)"""
        self.buffer += data
        
        # Process all complete frames in buffer
        while len(self.buffer) >= 4:
            # Read length prefix (4 bytes, big-endian)
            length = int.from_bytes(self.buffer[:4], byteorder='big')
            
            # Check if we have complete frame
            if len(self.buffer) < 4 + length:
                break  # Wait for more data
            
            # Extract frame
            frame = self.buffer[4:4+length]
            self.buffer = self.buffer[4+length:]
            
            # Process event
            asyncio.create_task(self.callback(frame))
    
    def connection_lost(self, exc):
        """Called when Unix socket connection lost"""
        if exc:
            logger.warning(f"⚠️ HBlink4 Unix socket connection lost: {exc}")
        else:
            logger.info("HBlink4 Unix socket connection closed")
        
        # Set connection status synchronously (async broadcast may be delayed)
        state.hblink_connected = False
        
        # Notify all browser clients that HBlink4 is disconnected
        asyncio.create_task(broadcast_hblink_status(False))


class EventReceiver:
    """Receives events from hblink4 via TCP or Unix socket.

    This side listens; hblink4 (hblink4/events.py EventEmitter) initiates the
    connection and sends the events. The addresses below are bind addresses.
    """
    
    def __init__(self, transport='unix', bind_ipv4='127.0.0.1', bind_ipv6='::1',
                 port=8765, unix_socket='/tmp/hblink4.sock', disable_ipv6=False):
        """
        Initialize event receiver with transport abstraction

        Args:
            transport: 'tcp' or 'unix'
            bind_ipv4: Address to bind the IPv4 listener to (for TCP)
            bind_ipv6: Address to bind the IPv6 listener to (for TCP)
            port: Port to listen on (for TCP)
            unix_socket: Path of the Unix socket to create and listen on
            disable_ipv6: Disable IPv6 (for networks with broken IPv6)
        """
        self.transport = transport.lower()
        self.bind_ipv4 = bind_ipv4
        self.bind_ipv6 = bind_ipv6 if not disable_ipv6 else None
        self.disable_ipv6 = disable_ipv6
        
        if disable_ipv6 and transport == 'tcp':
            logger.warning('⚠️  IPv6 disabled for event receiver - using IPv4 only')
        self.port = port
        self.unix_socket = unix_socket
        self.server = None
        self.server_v6 = None
    
    async def start(self):
        """Start receiving events from hblink4"""
        loop = asyncio.get_event_loop()
        
        if self.transport == 'tcp':
            await self._start_tcp(loop)
        elif self.transport == 'unix':
            await self._start_unix(loop)
        else:
            logger.error(f"Unknown transport: {self.transport} (valid options: 'tcp', 'unix')")
            raise ValueError(f"Unknown transport: {self.transport}")
    
    async def _start_tcp(self, loop):
        """Start TCP server on both IPv4 and IPv6"""
        # Start IPv4 listener
        if self.bind_ipv4:
            try:
                self.server = await loop.create_server(
                    lambda: TCPProtocol(self.process_event),
                    self.bind_ipv4, self.port,
                    family=socket.AF_INET
                )
                logger.info(f"✓ Listening for HBlink4 events via TCP on {self.bind_ipv4}:{self.port} (IPv4)")
            except Exception as e:
                logger.error(f"✗ Failed to start IPv4 TCP listener: {e}")
        
        # Start IPv6 listener
        if self.bind_ipv6:
            try:
                self.server_v6 = await loop.create_server(
                    lambda: TCPProtocol(self.process_event),
                    self.bind_ipv6, self.port,
                    family=socket.AF_INET6
                )
                logger.info(f"✓ Listening for HBlink4 events via TCP on [{self.bind_ipv6}]:{self.port} (IPv6)")
            except Exception as e:
                logger.error(f"✗ Failed to start IPv6 TCP listener: {e}")
    
    async def _start_unix(self, loop):
        """Start Unix socket server"""
        # Remove existing socket file if it exists
        if os.path.exists(self.unix_socket):
            try:
                os.unlink(self.unix_socket)
                logger.info(f"Removed existing socket file: {self.unix_socket}")
            except PermissionError:
                try:
                    owner = Path(self.unix_socket).owner()
                except Exception:
                    owner = "another user"
                logger.error(f"❌ {self.unix_socket} belongs to {owner} (left over from a start as root) and "
                             f"cannot be replaced: HBlink4 will not reach the dashboard. Fix as root: "
                             f"rm -f {self.unix_socket} && systemctl restart hblink4-dash hblink4")
                return
            except Exception as e:
                logger.warning(f"Failed to remove existing socket: {e}")
        
        self.server = await loop.create_unix_server(
            lambda: UnixProtocol(self.process_event),
            self.unix_socket
        )
        
        # Set socket permissions (readable/writable by owner and group)
        try:
            os.chmod(self.unix_socket, 0o660)
        except Exception as e:
            logger.warning(f"Failed to set socket permissions: {e}")
        
        logger.info(f"📡 Listening for HBlink4 events via Unix socket at {self.unix_socket}")
    
    async def process_event(self, data: bytes):
        """Process incoming event from hblink4"""
        try:
            event = json.loads(data.decode('utf-8'))
            await self.handle_event(event)
        except Exception as e:
            logger.error(f"Error processing event: {e}")
    
    async def handle_event(self, event: dict):
        """Update state and broadcast to WebSocket clients"""
        event_type = event['type']
        data = event['data']
        
        # Update internal state based on event type
        if event_type == 'repeater_connected':
            state.repeaters[data['repeater_id']] = {
                **data,
                # Prefer the server's session start. repeater_connected is also
                # emitted on missed pings, on recovery, and when this dashboard
                # reconnects and the server replays state — using the event's
                # arrival time would reset uptime for repeaters that never dropped.
                # Fall back for servers older than this field.
                'connected_at': data.get('connect_time', event['timestamp']),
                'last_activity': event['timestamp'],
                'last_ping': data.get('last_ping', event['timestamp']),
                'missed_pings': data.get('missed_pings', 0),
                'status': 'connected'
            }
            logger.info(f"Repeater connected: {data['repeater_id']} ({data.get('callsign', 'UNKNOWN')})")
        
        elif event_type == 'repeater_keepalive':
            if data['repeater_id'] in state.repeaters:
                state.repeaters[data['repeater_id']]['last_ping'] = data.get('last_ping', event['timestamp'])
                state.repeaters[data['repeater_id']]['missed_pings'] = data.get('missed_pings', 0)
                state.repeaters[data['repeater_id']]['last_activity'] = event['timestamp']
        
        elif event_type == 'repeater_disconnected':
            if data['repeater_id'] in state.repeaters:
                # Remove repeater from state immediately
                del state.repeaters[data['repeater_id']]
                logger.info(f"Repeater disconnected: {data['repeater_id']} ({data.get('callsign', 'UNKNOWN')}) - reason: {data.get('reason', 'unknown')}")
        
        elif event_type == 'repeater_details':
            # Store detailed repeater information (sent once on connection)
            state.repeater_details[data['repeater_id']] = {
                **data,
                'received_at': event['timestamp']
            }
            logger.debug(f"Repeater details received: {data['repeater_id']} - Pattern: {data.get('matched_pattern', 'Unknown')}")
        
        elif event_type == 'repeater_options_updated':
            # RPTO received - update TG lists in real-time
            if data['repeater_id'] in state.repeaters:
                state.repeaters[data['repeater_id']]['slot1_talkgroups'] = data.get('slot1_talkgroups', [])
                state.repeaters[data['repeater_id']]['slot2_talkgroups'] = data.get('slot2_talkgroups', [])
                state.repeaters[data['repeater_id']]['rpto_received'] = data.get('rpto_received', False)
                state.repeaters[data['repeater_id']]['translations'] = data.get('translations', [])
                logger.info(f"Repeater options updated via RPTO: {data['repeater_id']}")
        
        elif event_type == 'stream_start':
            # Handle both repeater streams and outbound connection streams
            connection_type = data.get('connection_type', 'repeater')
            
            if connection_type == 'openbridge':
                # OBP is stream-multiplexed - key by connection_name.stream_id
                key = f"{data['connection_name']}.{data.get('stream_id')}"
            elif connection_type == 'outbound':
                # Outbound stream - key by connection_name.slot
                key = f"{data['connection_name']}.{data['slot']}"
            else:
                repeater_id = data.get('repeater_id')
                if repeater_id is None:
                    logger.error("Stream start missing repeater_id: %s", event)
                    return
                # Repeater stream - key by repeater_id.slot (legacy behavior)
                key = f"{repeater_id}.{data['slot']}"
            
            # Look up callsign from user database
            src_id = data.get('rf_src') or data.get('src_id')  # Handle both field names
            callsign = state.user_db.get(src_id, '') if src_id else ''
            
            state.streams[key] = {
                **data,
                'callsign': callsign,  # Add callsign to stream data
                'start_time': event['timestamp'],
                'packets': 0,
                'duration': 0,
                'status': 'active'
            }
            
            # Count different stream types
            if not data.get('is_assumed', False):
                # RX streams: actual traffic being received (from repeaters OR outbound connections)
                # Only exclude TX streams (is_assumed=True)
                state.stats['total_calls_today'] += 1
                
                # Add/update user in last_heard immediately with "active" status
                # Only for actual received calls, not retransmitted calls
                if src_id:
                    # Build source display: "Name (originating_repeater_id)"
                    # For repeaters: callsign (repeater_id)
                    # For outbound: connection_name (remote_repeater_id)
                    if connection_type == 'openbridge':
                        # OBP source: trunk name + the origin peer that rides in
                        # the frame (bytes 11:15). Present only when the far end
                        # preserves the source peer; a far end stamping its own
                        # network_id sends no usable peer, so fall back to the
                        # bare trunk name. This is the closest-hop source peer —
                        # over multi-hop OBP it reflects the immediate upstream,
                        # not the full path (we can't control other nodes).
                        conn_name = data.get('connection_name', 'OpenBridge')
                        remote_rid = data.get('remote_repeater_id')
                        source_name = f"{conn_name} ({remote_rid})" if remote_rid else conn_name
                    elif connection_type == 'outbound':
                        conn_name = data.get('connection_name', 'Unknown')
                        remote_rid = data.get('remote_repeater_id', 0)
                        source_name = f"{conn_name} ({remote_rid})"
                    else:
                        repeater_id = data.get('repeater_id', 0)
                        # Look up repeater callsign from state
                        repeater_info = state.repeaters.get(repeater_id, {})
                        repeater_callsign = repeater_info.get('callsign', '')
                        if repeater_callsign:
                            source_name = f"{repeater_callsign} ({repeater_id})"
                        else:
                            source_name = str(repeater_id)
                    
                    # Find existing entry or create new one
                    existing_idx = next((i for i, u in enumerate(state.last_heard) if u['radio_id'] == src_id), None)
                    user_entry = {
                        'radio_id': src_id,
                        'callsign': callsign,
                        'source_name': source_name,
                        'slot': data['slot'],
                        'talkgroup': data.get('dst_id', 0),
                        'call_type': data.get('call_type', 'group'),  # "group" or "private" (addressing)
                        'is_data': data.get('is_data', False),  # voice vs data (payload kind)
                        'frid': state.user_db.is_frid(src_id),  # registered at FreeRadioID.net
                        'last_heard': event['timestamp'],
                        'active': True  # Mark as currently active
                    }
                    # Only local repeater ingresses have translation state we can reference;
                    # outbound- and OBP-ingresses are remote and carry no local map.
                    if connection_type == 'repeater':
                        user_entry['source_repeater_id'] = data.get('repeater_id')
                    
                    if existing_idx is not None:
                        state.last_heard[existing_idx] = user_entry
                    else:
                        state.last_heard.insert(0, user_entry)  # Add to front
                    
                    # Keep only most recent 10 entries
                    state.last_heard = state.last_heard[:10]
            else:
                # TX streams: traffic being retransmitted to repeaters or outbound connections
                # Don't add to last_heard - these represent the same call being forwarded
                state.stats['retransmitted_calls'] += 1
            
            # Update repeater/outbound/OBP last activity
            if connection_type == 'openbridge' and data['connection_name'] in state.openbridges:
                state.openbridges[data['connection_name']]['last_activity'] = event['timestamp']
            elif connection_type == 'outbound' and data['connection_name'] in state.outbounds:
                state.outbounds[data['connection_name']]['last_activity'] = event['timestamp']
            elif data.get('repeater_id') in state.repeaters:
                state.repeaters[data['repeater_id']]['last_activity'] = event['timestamp']
        
        elif event_type == 'stream_update':
            # Handle both repeater and outbound streams
            connection_type = data.get('connection_type', 'repeater')
            if connection_type == 'openbridge':
                key = f"{data['connection_name']}.{data.get('stream_id')}"
            elif connection_type == 'outbound':
                key = f"{data['connection_name']}.{data['slot']}"
            else:
                key = f"{data['repeater_id']}.{data['slot']}"

            if key in state.streams:
                state.streams[key]['packets'] = data['packets']
                state.streams[key]['duration'] = data['duration']
        
        elif event_type == 'stream_end':
            # Handle both repeater and outbound streams
            connection_type = data.get('connection_type', 'repeater')
            if connection_type == 'openbridge':
                key = f"{data['connection_name']}.{data.get('stream_id')}"
            elif connection_type == 'outbound':
                key = f"{data['connection_name']}.{data['slot']}"
            else:
                key = f"{data['repeater_id']}.{data['slot']}"

            if key in state.streams:
                # Stream ended and entering hang time (combined event)
                stream = state.streams[key]
                stream['status'] = 'hang_time'
                stream['packets'] = data.get('packet_count', data.get('packets', 0))
                stream['duration'] = data['duration']
                stream['end_reason'] = data.get('end_reason', 'unknown')
                stream['hang_time'] = data.get('hang_time', 0)
                
                # Only accumulate duration for RX streams (not assumed/TX streams or outbound)
                if not data.get('is_assumed', False) and connection_type != 'outbound':
                    state.stats['total_duration_today'] += data['duration']
                
                # Update last_heard entry to mark as no longer active (only for repeater RX streams)
                if connection_type != 'outbound':
                    src_id = stream.get('rf_src') or stream.get('src_id')
                    if src_id:
                        user_entry = next((u for u in state.last_heard if u['radio_id'] == src_id), None)
                        if user_entry:
                            user_entry['active'] = False
                            user_entry['last_heard'] = event['timestamp']

                # OBP is stream-multiplexed with no hang time — drop the ended stream now.
                if connection_type == 'openbridge':
                    del state.streams[key]
        
        elif event_type == 'hang_time_expired':
            # Hang time has expired, clear the slot (handle both repeater and outbound)
            connection_type = data.get('connection_type', 'repeater')
            if connection_type == 'outbound':
                key = f"{data['connection_name']}.{data['slot']}"
            else:
                key = f"{data['repeater_id']}.{data['slot']}"
                
            if key in state.streams:
                del state.streams[key]
                logger.debug(f"Hang time expired for {key}")
        
        elif event_type == 'outbound_connecting':
            # Outbound connection is attempting to connect
            conn_name = data['connection_name']
            state.outbounds[conn_name] = {
                **data,
                'connecting_at': event['timestamp'],
                'last_activity': event['timestamp'],
                'status': 'connecting'
            }
            logger.info(f"Outbound connection attempting: {conn_name} (radio_id={data.get('radio_id')})")
        
        elif event_type == 'outbound_connected':
            # Outbound connection established
            conn_name = data['connection_name']
            state.outbounds[conn_name] = {
                **data,
                'connected_at': event['timestamp'],
                'last_activity': event['timestamp'],
                'status': 'connected'
            }
            logger.info(f"Outbound connection established: {conn_name} (radio_id={data.get('radio_id')}) at {data.get('remote_address')}:{data.get('remote_port')}")

        elif event_type == 'openbridge_connected':
            # OpenBridge trunk is up (bound socket; no auth handshake)
            conn_name = data['connection_name']
            state.openbridges[conn_name] = {
                **data,
                'connected_at': event['timestamp'],
                'last_activity': event['timestamp'],
                'status': 'connected',
            }
            logger.info(f"OpenBridge trunk up: {conn_name} (network_id={data.get('network_id')}) "
                        f"→ {data.get('remote_address')}:{data.get('remote_port')} "
                        f"({len(data.get('talkgroups', {}))} talkgroups)")

        elif event_type == 'openbridge_disconnected':
            conn_name = data['connection_name']
            if conn_name in state.openbridges:
                state.openbridges[conn_name]['status'] = 'disconnected'
                state.openbridges[conn_name]['disconnected_at'] = event['timestamp']

        elif event_type == 'outbound_disconnected':
            # Outbound connection lost - change to disconnected status but don't remove
            conn_name = data['connection_name']
            if conn_name in state.outbounds:
                state.outbounds[conn_name]['status'] = 'disconnected'
                state.outbounds[conn_name]['disconnected_at'] = event['timestamp']
                state.outbounds[conn_name]['disconnect_reason'] = data.get('reason', 'unknown')
                logger.info(f"Outbound connection disconnected: {conn_name} - reason: {data.get('reason', 'unknown')}")
        
        elif event_type == 'outbound_error':
            # Outbound connection error
            conn_name = data['connection_name']
            if conn_name in state.outbounds:
                state.outbounds[conn_name]['status'] = 'error'
                state.outbounds[conn_name]['error_message'] = data.get('error_message', 'Unknown error')
                state.outbounds[conn_name]['last_error'] = event['timestamp']
            else:
                # Connection failed before it was established
                state.outbounds[conn_name] = {
                    **data,
                    'status': 'error',
                    'error_message': data.get('error_message', 'Unknown error'),
                    'last_error': event['timestamp']
                }
            logger.warning(f"Outbound connection error: {conn_name} - {data.get('error_message', 'Unknown error')}")
        

        
        # Add to event log (only for user-facing on-air activity events)
        # Skip system events like repeater_connected, repeater_disconnected, hang_time_expired, repeater_keepalive
        # Skip TX/assumed streams from the events log (but they're still tracked in state.streams for repeater cards)
        if event_type in ['stream_start', 'stream_end']:
            # Only add RX streams to the events log (TX streams have is_assumed=True)
            if not data.get('is_assumed', False):
                state.events.append(event)
        
        # For stream events, include updated last_heard list in the event
        if event_type in ['stream_start', 'stream_end', 'hang_time_expired']:
            event['last_heard'] = state.last_heard
        
        # Send to all WebSocket clients
        await self.send_to_clients(event)
    
    async def send_to_clients(self, event: dict):
        """Send event to all connected WebSocket clients"""
        if not state.websocket_clients:
            return
        
        message = json.dumps(event)
        disconnected = set()
        
        for client in state.websocket_clients:
            try:
                await client.send_text(message)
            except:
                disconnected.add(client)
        
        # Remove disconnected clients
        state.websocket_clients -= disconnected


# ======================================================================
# Authentication, IP visibility, user management
# ======================================================================
SESSION_COOKIE = "n8dash_session"
_PUBLIC_CONFIG_KEYS = ("server_name", "server_description", "dashboard_title", "logo", "favicon",
                       "announcement", "network_info", "refresh_interval", "max_events")
_redact_memo: tuple = (None, None)


def _auth_cfg() -> dict:
    return dashboard_config.get("auth") or {}


def _public_dashboard() -> bool:
    return bool(_auth_cfg().get("public_dashboard", True))


def _client_ip(conn) -> str:
    return conn.client.host if conn.client else ""


def current_user(conn) -> Optional[dict]:
    """User of a Request or WebSocket from its session cookie (or None)."""
    return state.auth.session_user(conn.cookies.get(SESSION_COOKIE))


def is_admin(user: Optional[dict]) -> bool:
    return bool(user and user.get("role") in ADMIN_ROLES)


def view_for(user: Optional[dict], data):
    """IP addresses are for admins only."""
    return data if is_admin(user) else redact_ips(data)


def _redact_text(text: str) -> str:
    global _redact_memo
    if _redact_memo[0] is text:
        return _redact_memo[1]
    try:
        out = json.dumps(redact_ips(json.loads(text)))
    except ValueError:
        out = text
    _redact_memo = (text, out)
    return out


class ClientSocket:
    """A dashboard WebSocket that redacts IP addresses for non-admins."""

    def __init__(self, ws: WebSocket, token: Optional[str], user: Optional[dict]):
        self.ws = ws
        self.token = token
        self.user = user
        self._checked = datetime.now().timestamp()

    @property
    def admin(self) -> bool:
        now = datetime.now().timestamp()
        if self.token and now - self._checked > 60:   # session revoked or demoted?
            self.user = state.auth.session_user(self.token)
            self._checked = now
        return is_admin(self.user)

    async def send_text(self, text: str):
        await self.ws.send_text(text if (text == 'pong' or self.admin) else _redact_text(text))

    async def send_json(self, obj):
        await self.ws.send_json(obj if self.admin else redact_ips(obj))


def _require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise _HTTPError(401, "Please log in.")
    return user


def _require_admin(request: Request) -> dict:
    user = _require_user(request)
    if not is_admin(user):
        raise _HTTPError(403, "Admins only.")
    return user


class _HTTPError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


@app.exception_handler(_HTTPError)
async def _http_error_handler(request: Request, exc: _HTTPError):
    return JSONResponse({"error": exc.message}, status_code=exc.status)


@app.exception_handler(AuthError)
async def _auth_error_handler(request: Request, exc: AuthError):
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.middleware("http")
async def _security_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        # CSRF: state-changing calls must come from our own page (custom header +
        # SameSite=Strict cookie).
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get("x-dashboard") != "1":
            return JSONResponse({"error": "Missing X-Dashboard header."}, status_code=403)
        # Private dashboard: everything but login and branding needs a session.
        if (not _public_dashboard() and not path.startswith("/api/auth/") and path != "/api/config"
                and not current_user(request)):
            return JSONResponse({"error": "Please log in."}, status_code=401)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


def _set_session_cookie(request: Request, response: Response, token: str, max_age: Optional[int]):
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")


@app.post("/api/auth/login")
async def auth_login(request: Request, body: dict = Body(...)):
    remember = bool(body.get("remember"))
    hours = float(_auth_cfg().get("remember_days", 30)) * 24 if remember else float(_auth_cfg().get("session_hours", 12))
    token, user = await asyncio.to_thread(
        state.auth.login, str(body.get("username") or ""), str(body.get("password") or ""),
        _client_ip(request), request.headers.get("user-agent", ""), hours)
    logger.info(f"🔐 Login: {user['username']} ({user['role']}) from {_client_ip(request)}")
    response = JSONResponse({"user": user})
    _set_session_cookie(request, response, token, int(hours * 3600) if remember else None)
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    user = current_user(request)
    state.auth.logout(request.cookies.get(SESSION_COOKIE))
    if user:
        state.auth.audit("logout", user["username"], "", _client_ip(request))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/me")
async def auth_me(request: Request):
    return {"user": current_user(request), "public_dashboard": _public_dashboard(),
            "https": request.url.scheme == "https"}


@app.post("/api/auth/password")
async def auth_password(request: Request, body: dict = Body(...)):
    user = _require_user(request)
    await asyncio.to_thread(state.auth.change_own_password, user, str(body.get("old") or ""),
                            str(body.get("new") or ""), _client_ip(request))
    return {"ok": True}


@app.get("/api/admin/users")
async def admin_users(request: Request):
    _require_admin(request)
    return {"users": state.auth.list_users()}


@app.post("/api/admin/users")
async def admin_create_user(request: Request, body: dict = Body(...)):
    actor = _require_admin(request)
    user = await asyncio.to_thread(
        state.auth.create_user, actor, str(body.get("username") or ""), str(body.get("password") or ""),
        str(body.get("role") or "user"), str(body.get("display_name") or ""), body.get("dmr_id"),
        _client_ip(request))
    return {"user": user}


@app.patch("/api/admin/users/{user_id}")
async def admin_update_user(user_id: int, request: Request, body: dict = Body(...)):
    actor = _require_admin(request)
    allowed = {k: body[k] for k in ("password", "role", "display_name", "dmr_id", "disabled") if k in body}
    user = await asyncio.to_thread(state.auth.update_user, actor, user_id, allowed, _client_ip(request))
    return {"user": user}


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, request: Request):
    actor = _require_admin(request)
    state.auth.delete_user(actor, user_id, _client_ip(request))
    return {"ok": True}


@app.get("/api/admin/sessions")
async def admin_sessions(request: Request):
    _require_admin(request)
    return {"sessions": state.auth.list_sessions()}


@app.delete("/api/admin/sessions/{session_id}")
async def admin_revoke_session(session_id: str, request: Request):
    actor = _require_admin(request)
    state.auth.revoke_session(session_id)
    state.auth.audit("session_revoked", actor["username"], session_id, _client_ip(request))
    return {"ok": True}


@app.get("/api/admin/audit")
async def admin_audit(request: Request):
    _require_admin(request)
    return {"audit": state.auth.audit_log()}


@app.get("/api/admin/system")
async def admin_system(request: Request):
    _require_admin(request)
    https_cfg = dashboard_config.get("https") or {}
    info = {"https_enabled": bool(https_cfg.get("enabled")), "domain": https_cfg.get("domain", ""),
            "letsencrypt": bool(https_cfg.get("letsencrypt", True)), "served_over_https": request.url.scheme == "https",
            "cert_expires": None, "public_dashboard": _public_dashboard(),
            "superadmin": ((_auth_cfg().get("superadmin") or {}).get("username") or ""),
            "users": len(state.auth.list_users()), "systemd": bool(os.environ.get("INVOCATION_ID")),
            "frid_ids": len(state.user_db.frid_ids()), "frid_avatars": state.avatars.stats()["with_avatar"],
            "version": "HBlink4-FreeRadioID"}
    if _tls_manager is not None and _tls_manager.have_cert():
        try:
            import ssl as _ssl
            info["cert_expires"] = _ssl._ssl._test_decode_cert(_tls_manager.cert_file).get("notAfter")
        except Exception:
            pass
    return info


# ---------------------------------------------------------------- settings
_USERDB_KEYS = {"user_database.source_url", "user_database.filter.countries",
                "user_database.freeradioid.enabled", "freeradioid.avatars"}


async def _after_settings_change(changed: List[str]):
    if not changed:
        return
    # Pages reload title, logo, banner etc.
    message = json.dumps({"type": "config_changed", "timestamp": datetime.now().timestamp()})
    for client in list(state.websocket_clients):
        try:
            await client.send_text(message)
        except Exception:
            pass
    if _USERDB_KEYS & set(changed):
        asyncio.create_task(_refresh_userdb_now("settings"))


async def _refresh_userdb_now(trigger: str):
    udb_cfg = dashboard_config.get("user_database", {}) or {}
    # a new source or filter must be downloaded in full, not answered with 304
    state.user_db.meta.last_modified_header = ""
    status = await state.user_db.refresh_from_upstream(udb_cfg)
    await broadcast_user_db_status(trigger=trigger, status=status)
    asyncio.create_task(sync_avatars(trigger))


@app.get("/api/admin/settings")
async def admin_settings(request: Request):
    _require_admin(request)
    return {"sections": state.settings.describe(dashboard_config)}


@app.put("/api/admin/settings")
async def admin_save_settings(request: Request, body: dict = Body(...)):
    actor = _require_admin(request)
    changes = body.get("changes") or {}
    if not isinstance(changes, dict):
        raise _HTTPError(400, "changes must be an object")
    try:
        changed = state.settings.update(dashboard_config, changes, actor["username"])
    except SettingsError as e:
        raise _HTTPError(400, str(e))
    if changed:
        state.auth.audit("settings_changed", actor["username"], ", ".join(changed), _client_ip(request))
    await _after_settings_change(changed)
    restart = any(FIELD_BY_KEY[k].restart for k in changed)
    return {"changed": changed, "restart_required": restart,
            "sections": state.settings.describe(dashboard_config)}


@app.post("/api/admin/settings/reset")
async def admin_reset_settings(request: Request, body: dict = Body(...)):
    actor = _require_admin(request)
    section = str(body.get("section") or "")
    before = {k: _copy.deepcopy(_get_cfg(k)) for k in FIELD_BY_KEY}
    try:
        state.settings.reset_section(dashboard_config, _file_config, section, actor["username"])
    except SettingsError as e:
        raise _HTTPError(400, str(e))
    changed = [k for k in FIELD_BY_KEY if _get_cfg(k) != before[k]]
    state.auth.audit("settings_reset", actor["username"], section, _client_ip(request))
    await _after_settings_change(changed)
    return {"changed": changed, "restart_required": any(FIELD_BY_KEY[k].restart for k in changed),
            "sections": state.settings.describe(dashboard_config)}


def _get_cfg(key: str):
    from .settings import get_path
    return get_path(dashboard_config, key)


@app.post("/api/admin/userdb/refresh")
async def admin_refresh_userdb(request: Request):
    actor = _require_admin(request)
    state.auth.audit("userdb_refresh", actor["username"], "", _client_ip(request))
    asyncio.create_task(_refresh_userdb_now("admin"))
    return {"ok": True}


@app.post("/api/admin/restart")
async def admin_restart(request: Request):
    actor = _require_admin(request)
    if not os.environ.get("INVOCATION_ID"):
        raise _HTTPError(400, "Only possible when the dashboard runs as a systemd service (Restart=always).")
    state.auth.audit("restart", actor["username"], "", _client_ip(request))
    import signal as _signal
    asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), _signal.SIGTERM)
    return {"ok": True}


# ---------------------------------------------------------------- avatars
# Only for IDs registered at FreeRadioID, served from SQLite (see dashboard/avatars.py).
# Proxying also keeps viewers' IPs away from third parties.
def _avatars_enabled() -> bool:
    frid = dashboard_config.get("freeradioid") or {}
    return bool(frid.get("avatars", True) and frid.get("avatar_url", True))


async def sync_avatars(trigger: str = ""):
    """Fetch new/stale avatars of all FreeRadioID-registered IDs into SQLite."""
    if not _avatars_enabled():
        return
    ids = state.user_db.frid_ids()
    if not ids:
        return
    try:
        result = await state.avatars.sync(ids)
    except Exception as e:
        logger.warning(f"🖼️ Avatar sync failed ({trigger}): {e}")
        return
    if result.get("fetched"):
        # open pages switch to the new image URLs (and so past any cached "no avatar")
        message = json.dumps({"type": "avatars_updated", "timestamp": datetime.now().timestamp(),
                              "data": {"version": state.avatars.version()}})
        for client in list(state.websocket_clients):
            try:
                await client.send_text(message)
            except Exception:
                pass


@app.get("/api/avatar/{dmr_id}")
async def get_avatar(dmr_id: int):
    # short: an ID may get an avatar with the next sync
    miss = Response(status_code=404, headers={"Cache-Control": "public, max-age=300"})
    if not _avatars_enabled() or not state.user_db.is_frid(dmr_id):
        return miss       # not registered at FreeRadioID: no avatar, no request to FreeRadioID
    hit = state.avatars.get(dmr_id)
    if hit is None:       # registered but not synced yet: fetch this one now
        try:
            await asyncio.to_thread(state.avatars.fetch_one, dmr_id)
        except Exception as e:
            logger.debug(f"Avatar {dmr_id}: {e}")
            return miss
        hit = state.avatars.get(dmr_id)
    if not hit or not hit[0]:
        return miss
    return Response(hit[2], media_type=hit[1] or "image/png", headers={"Cache-Control": "public, max-age=86400"})


# REST API endpoints
@app.get("/api/config")
async def get_config():
    """Public part of the dashboard configuration (no secrets)."""
    cfg = {k: dashboard_config[k] for k in _PUBLIC_CONFIG_KEYS if k in dashboard_config}
    frid = dashboard_config.get("freeradioid") or {}
    cfg["freeradioid"] = {"avatars": bool(frid.get("avatars", True) and frid.get("avatar_url", True)),
                          "avatar_version": state.avatars.version()}
    cfg["public_dashboard"] = _public_dashboard()
    return cfg


@app.get("/api/repeaters")
async def get_repeaters(request: Request):
    """Get all connected repeaters"""
    return view_for(current_user(request), {"repeaters": list(state.repeaters.values())})


@app.get("/api/outbounds")
async def get_outbounds(request: Request):
    """Get all outbound connections"""
    return view_for(current_user(request), {"outbounds": list(state.outbounds.values())})


@app.get("/api/streams")
async def get_streams(request: Request):
    """Get all active streams"""
    return view_for(current_user(request), {"streams": list(state.streams.values())})


@app.get("/api/events")
async def get_events(request: Request, limit: int = 100):
    """Get recent events"""
    return view_for(current_user(request), {"events": list(state.events)[-limit:]})


@app.get("/api/stats")
async def get_stats():
    """Get system statistics"""
    return {
        "stats": state.stats,
        "repeaters_connected": len([r for r in state.repeaters.values() if r.get('status') == 'connected']),
        "active_streams": len([s for s in state.streams.values() if s.get('status') == 'active'])
    }


# ---------------------------------------------------------------- FreeRadioID
_frid_profile_cache: Dict[int, tuple] = {}   # radio_id -> (timestamp, profile or None)
_FRID_PROFILE_TTL = 3600


def _frid_profile_fetch_sync(radio_id: int) -> Optional[dict]:
    import urllib.request
    cfg = dashboard_config.get("freeradioid", {}) or {}
    url = cfg.get("profile_url", "https://freeradioid.net/api/dmr/user/?id={id}").replace("{id}", str(radio_id))
    req = urllib.request.Request(url, headers={"User-Agent": "HBlink4-Dashboard FreeRadioID"})
    with urllib.request.urlopen(req, timeout=4) as resp:
        raw = json.loads(resp.read(65536).decode("utf-8", errors="replace"))
    results = raw.get("results") or []
    if not results:
        return None
    r = results[0]
    return {
        "registered": bool(raw.get("reg")),
        "callsign": r.get("callsign") or "",
        "name": " ".join(x for x in (r.get("fname"), r.get("surname")) if x) or "",
        "city": r.get("city") or "",
        "state": r.get("state") if isinstance(r.get("state"), str) else "",
        "country": r.get("country") or "",
        "remarks": r.get("remarks") or "",
    }


async def freeradioid_profile(radio_id: int) -> Optional[dict]:
    """
    FreeRadioID profile for a radio ID: live API (cached for an hour), falling
    back to the downloaded registry. None if FreeRadioID knows nothing about it.
    """
    cfg = dashboard_config.get("freeradioid", {}) or {}
    profile = None
    if cfg.get("profile_lookup", True):
        cached = _frid_profile_cache.get(radio_id)
        if cached and datetime.now().timestamp() - cached[0] < _FRID_PROFILE_TTL:
            profile = cached[1]
        else:
            try:
                profile = await asyncio.to_thread(_frid_profile_fetch_sync, radio_id)
            except Exception as e:
                logger.debug(f"FreeRadioID profile lookup for {radio_id} failed: {e}")
                profile = cached[1] if cached else None
            else:
                _frid_profile_cache[radio_id] = (datetime.now().timestamp(), profile)
    if profile is None:
        reg = state.user_db.frid_info(radio_id)
        if reg:
            profile = {**reg, "registered": True, "remarks": ""}
    if profile is not None:
        profile = dict(profile)
        profile["registered"] = profile.get("registered") or state.user_db.is_frid(radio_id)
        profile["avatar"] = f"/api/avatar/{radio_id}"
    return profile


@app.get("/api/freeradioid/{radio_id}")
async def get_freeradioid_profile(radio_id: int):
    """FreeRadioID profile (name, location, registration, avatar) for a radio ID."""
    return {"radio_id": radio_id, "profile": await freeradioid_profile(radio_id)}


@app.get("/api/repeater/{repeater_id}")
async def get_repeater_details(repeater_id: int, request: Request):
    return view_for(current_user(request), await _repeater_details(repeater_id))


async def _repeater_details(repeater_id: int):
    """Get detailed information about a specific repeater"""
    if repeater_id not in state.repeaters:
        raise _HTTPError(404, "Repeater not found")
    
    repeater = state.repeaters[repeater_id]
    details = state.repeater_details.get(repeater_id, {})
    
    # If no details event received yet, try to determine from config
    if not details or not details.get('matched_pattern'):
        # Try to load pattern from config
        config_path = Path(__file__).parent.parent / 'config' / 'config.json'
        try:
            with open(config_path) as f:
                config = json.load(f)
            
            # Import here to avoid circular dependencies
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from hblink4.access_control import RepeaterMatcher
            
            matcher = RepeaterMatcher(config)
            pattern = matcher.get_pattern_for_repeater(repeater_id, repeater.get('callsign'))
            
            if pattern:
                details['matched_pattern'] = pattern.name
                details['pattern_description'] = pattern.description
            else:
                details['matched_pattern'] = 'Default'
                details['pattern_description'] = 'Using default configuration'
        except Exception as e:
            logger.warning(f'Could not determine pattern for repeater {repeater_id}: {e}')
            details['matched_pattern'] = details.get('matched_pattern', 'Default')
            details['pattern_description'] = details.get('pattern_description', 'Using default configuration')
    
    # Calculate runtime statistics
    current_time = datetime.now().timestamp()
    uptime_seconds = int(current_time - repeater.get('connected_at', current_time))
    
    # Count streams for this repeater
    total_streams = len([e for e in state.events 
                        if e.get('type') == 'stream_start' 
                        and e.get('data', {}).get('repeater_id') == repeater_id])
    
    # Find active streams
    slot1_active = any(s.get('repeater_id') == repeater_id and s.get('slot') == 1 
                      and s.get('status') == 'active' 
                      for s in state.streams.values())
    slot2_active = any(s.get('repeater_id') == repeater_id and s.get('slot') == 2 
                      and s.get('status') == 'active' 
                      for s in state.streams.values())
    
    # Clean up IPv4-mapped IPv6 addresses
    address = repeater.get('address', '')
    if address.startswith('::ffff:'):
        address = address[7:]  # Remove '::ffff:' prefix
    
    # Build comprehensive response
    return {
        "repeater_id": repeater_id,
        "callsign": repeater.get('callsign', 'UNKNOWN'),
        "connection_type": repeater.get('connection_type', details.get('connection_type', 'unknown')),
        "connection": {
            "address": address,
            "connected_at": repeater.get('connected_at', 0),
            "uptime_seconds": uptime_seconds,
            "last_ping": repeater.get('last_ping', 0),
            "missed_pings": repeater.get('missed_pings', 0),
            "status": repeater.get('status', 'unknown')
        },
        "location": {
            "location": repeater.get('location', ''),
            "latitude": details.get('latitude', ''),
            "longitude": details.get('longitude', ''),
            "height": details.get('height', '')
        },
        "frequencies": {
            "rx_freq": repeater.get('rx_freq', ''),
            "tx_freq": repeater.get('tx_freq', ''),
            "tx_power": details.get('tx_power', ''),
            "colorcode": repeater.get('colorcode', ''),
            "slots": details.get('slots', '')
        },
        "access_control": {
            "connection_category": details.get('matched_pattern', 'Default'),
            "category_description": details.get('pattern_description', 'Using default configuration'),
            "rpto_received": repeater.get('rpto_received', False),
            "auth_method": repeater.get('auth_method', 'local'),
            "slot1_talkgroups": repeater.get('slot1_talkgroups') if 'slot1_talkgroups' in repeater else None,
            "slot2_talkgroups": repeater.get('slot2_talkgroups') if 'slot2_talkgroups' in repeater else None,
            "talkgroups_source": "RPTO" if repeater.get('rpto_received') else "Pattern/Config"
        },
        "metadata": {
            "description": details.get('description', ''),
            "url": details.get('url', ''),
            "software_id": repeater.get('software_id') or details.get('software_id', ''),
            "package_id": repeater.get('package_id') or details.get('package_id', '')
        },
        "freeradioid": await freeradioid_profile(repeater_id),
        "statistics": {
            "total_streams_today": total_streams,
            "slot1_active": slot1_active,
            "slot2_active": slot2_active
        }
    }


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket connection for real-time updates"""
    token = websocket.cookies.get(SESSION_COOKIE)
    user = state.auth.session_user(token)
    if not _public_dashboard() and not user:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    raw_ws = websocket
    websocket = ClientSocket(raw_ws, token, user)
    state.websocket_clients.add(websocket)
    logger.info(f"🌐 WebSocket client connected (total: {len(state.websocket_clients)})")
    
    # Log current state for debugging
    logger.info(f"📊 Sending initial_state: hblink_connected={state.hblink_connected}, repeaters={len(state.repeaters)}, streams={len(state.streams)}")
    
    # Send initial state
    await websocket.send_json({
        'type': 'initial_state',
        'data': {
            'repeaters': list(state.repeaters.values()),
            'repeater_details': state.repeater_details,
            'outbounds': list(state.outbounds.values()),
            'openbridges': list(state.openbridges.values()),
            'streams': list(state.streams.values()),
            'events': list(state.events)[-50:],
            'stats': state.stats,
            'last_heard': state.last_heard,
            'hblink_connected': state.hblink_connected,
            'user_db_status': state.user_db.status_dict()
        }
    })
    
    try:
        while True:
            # Keep connection alive (client can send ping)
            data = await raw_ws.receive_text()
            if data == 'ping':
                await websocket.send_text('pong')
    except WebSocketDisconnect:
        logger.debug(f"WebSocket client disconnected (remaining: {len(state.websocket_clients) - 1})")
    except asyncio.CancelledError:
        # Expected during shutdown - don't log as error
        logger.debug(f"WebSocket task cancelled during shutdown")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        # Always clean up, regardless of how we exited
        state.websocket_clients.discard(websocket)


# Serve frontend
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve dashboard HTML"""
    html_path = Path(__file__).parent / 'static' / 'dashboard.html'
    if not html_path.exists():
        return HTMLResponse("<h1>Dashboard HTML not found</h1><p>Please create dashboard/static/dashboard.html</p>", status_code=404)
    with open(html_path) as f:
        return HTMLResponse(f.read())


# Mount static files
static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=static_path), name="static")


@app.on_event("startup")
async def startup_event():
    """Start event receiver on startup"""
    receiver_config = dashboard_config.get('event_receiver', {})

    # These are bind addresses -- hblink4 connects in to us. They were once named
    # host_ipv4/host_ipv6, which collided with the connect-target keys of the same
    # name in hblink4's own config. Accept the old names so existing installs keep
    # working, but the current convention is bind_* for listening, host_* for
    # connecting out.
    def _bind_addr(new_key, old_key, default):
        if new_key in receiver_config:
            return receiver_config[new_key]
        if old_key in receiver_config:
            logger.warning(
                f"⚠️  event_receiver.{old_key} is deprecated -- rename it to "
                f"{new_key} (it is a bind address, not a connect target)"
            )
            return receiver_config[old_key]
        return default

    receiver = EventReceiver(
        transport=receiver_config.get('transport', 'unix'),
        bind_ipv4=_bind_addr('bind_ipv4', 'host_ipv4', '127.0.0.1'),
        bind_ipv6=_bind_addr('bind_ipv6', 'host_ipv6', '::1'),
        port=receiver_config.get('port', 8765),
        unix_socket=receiver_config.get('unix_socket', '/tmp/hblink4.sock'),
        disable_ipv6=receiver_config.get('disable_ipv6', False)
    )
    asyncio.create_task(receiver.start())
    asyncio.create_task(midnight_reset_task())
    asyncio.create_task(user_db_refresh_task())
    logger.info("🚀 HBlink4 Dashboard started!")
    logger.info(f"📡 Event transport: {receiver_config.get('transport', 'unix').upper()}")
    # Report the port actually configured, not the default -- an operator who
    # changed it should not be sent to the wrong URL.
    _web_cfg = dashboard_config.get('web', {}) or {}
    logger.info(f"📊 Access dashboard at http://localhost:{_web_cfg.get('port', 8080)}")

    # Warn operator if they left the placeholder contact in the User-Agent.
    udb_cfg = dashboard_config.get("user_database", {}) or {}
    ua = udb_cfg.get("user_agent", "")
    if "operator@example.org" in ua:
        logger.warning(
            "⚠️ user_database.user_agent still contains the placeholder contact "
            "'operator@example.org' — update it in config.json so radioid.net can "
            "reach you if there's an issue with your traffic."
        )


@app.on_event("shutdown")
async def shutdown_event():
    """Clean shutdown - save data"""
    logger.info("💾 Dashboard shutting down, saving data...")
    save_persistent_data()
    logger.info("✅ Dashboard shutdown complete")


async def midnight_reset_task():
    """Background task to reset daily stats at midnight"""
    while True:
        # Check if date has changed
        current_date = date.today().isoformat()
        if current_date != state.stats.get('last_reset_date'):
            state.reset_daily_stats()
            # Send stats update to all WebSocket clients
            await send_stats_update()

        # Check every 60 seconds
        await asyncio.sleep(60)


async def user_db_refresh_task():
    """
    Background task that refreshes the user database from radioid.net.

    - On startup: if the on-disk snapshot is missing or older than
      `on_startup_if_older_than_hours`, run one catch-up refresh immediately.
    - Then loop: sleep until the next scheduled time_of_day + jitter, refresh,
      repeat.
    - Respects `user_database.enabled=false` as a hard disable.
    - Broadcasts a user_db_status event after each refresh attempt so the UI
      can show "DB updated Nh ago" without a full page reload.
    """
    # Wait a moment for the startup log to settle, then evaluate state.
    await asyncio.sleep(2)

    udb_cfg = dashboard_config.get("user_database", {}) or {}
    if not udb_cfg.get("enabled", True):
        logger.info("📘 user_database.enabled=false — background refresh disabled")
        return

    refresh_cfg = udb_cfg.get("refresh", {}) or {}
    startup_threshold_h = float(refresh_cfg.get("on_startup_if_older_than_hours", 36))

    # Startup catch-up: run now if the snapshot is missing or stale.
    age_h = state.user_db.snapshot_age_hours()
    if age_h is None or age_h >= startup_threshold_h:
        logger.info(
            f"📘 user_db snapshot age={age_h}h >= threshold {startup_threshold_h}h — "
            "running catch-up refresh"
        )
        status = await state.user_db.refresh_from_upstream(udb_cfg)
        await broadcast_user_db_status(trigger="startup", status=status)
        asyncio.create_task(sync_avatars("startup"))
    else:
        frid_age = state.user_db.frid_age_hours()
        if frid_age is None or frid_age >= startup_threshold_h:
            status = await state.user_db.refresh_freeradioid(udb_cfg)
            await broadcast_user_db_status(trigger="startup", status=status)
        asyncio.create_task(sync_avatars("startup"))

    while True:
        # Compute next scheduled refresh.
        delay = compute_next_refresh_seconds(
            schedule=refresh_cfg.get("schedule", "daily"),
            time_of_day=refresh_cfg.get("time_of_day", "03:17"),
            jitter_minutes=int(refresh_cfg.get("jitter_minutes", 15)),
        )
        logger.info(
            f"📘 Next user_db refresh in {delay / 3600:.1f}h "
            f"(schedule={refresh_cfg.get('schedule', 'daily')}, "
            f"time_of_day={refresh_cfg.get('time_of_day', '03:17')})"
        )
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.debug("user_db_refresh_task cancelled during sleep")
            return

        status = await state.user_db.refresh_from_upstream(udb_cfg)
        await broadcast_user_db_status(trigger="scheduled", status=status)
        asyncio.create_task(sync_avatars("scheduled"))


async def broadcast_user_db_status(trigger: str, status: str):
    """Send a user_db_status event to all connected clients."""
    if not state.websocket_clients:
        return
    payload = state.user_db.status_dict()
    payload["trigger"] = trigger
    payload["status"] = status
    message = json.dumps({
        "type": "user_db_status",
        "timestamp": datetime.now().timestamp(),
        "data": payload,
    })
    disconnected = set()
    for client in state.websocket_clients:
        try:
            await client.send_text(message)
        except Exception:
            disconnected.add(client)
    state.websocket_clients -= disconnected


async def send_stats_update():
    """Send stats update to all WebSocket clients"""
    if not state.websocket_clients:
        return
    
    message = json.dumps({
        'type': 'stats_reset',
        'timestamp': datetime.now().timestamp(),
        'data': state.stats
    })
    
    disconnected = set()
    for client in state.websocket_clients:
        try:
            await client.send_text(message)
        except:
            disconnected.add(client)
    
    # Remove disconnected clients
    state.websocket_clients -= disconnected


# ========== GRACEFUL SHUTDOWN HANDLING ==========

def save_persistent_data():
    """Save all persistent data on shutdown - called by shutdown_event and atexit"""
    try:
        if state._persistence_disabled:
            logger.info("💾 Dashboard shutdown complete (persistence was disabled)")
            return
            
        state.save_all_data()
        logger.info("💾 Dashboard data saved on shutdown")
    except Exception as e:
        logger.error(f"❌ Failed to save data on shutdown: {e}")
        logger.info("💾 Dashboard shutdown complete (with save errors)")

# SIGTERM/SIGINT are left to uvicorn, which shuts the server down gracefully and
# triggers shutdown_event() above -- that is what saves the data. Installing our
# own handlers here would pre-empt that and exit mid-request.
atexit.register(save_persistent_data)  # Fallback for emergency exit


_tls_manager = None   # dashboard.tls.CertManager when HTTPS is enabled


class _Server:
    """uvicorn.Server without its own signal handling: several servers share one process."""

    @staticmethod
    def make(app_, **kwargs):
        import contextlib
        import uvicorn

        class S(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self):
                yield

        kwargs.setdefault("log_level", "info")
        kwargs.setdefault("access_log", False)
        kwargs.setdefault("loop", "asyncio")   # uvloop breaks Unix socket connections
        return S(uvicorn.Config(app_, **kwargs))


async def _serve(host: str, port: int):
    """
    Start the dashboard over HTTP, or over HTTPS with Let's Encrypt.

    The dashboard itself comes up first (HTTPS if a certificate exists, plain
    HTTP otherwise), so it is reachable while certbot works in the background.
    A stop signal ends everything at once, in any phase.
    """
    import signal
    global _tls_manager
    https_cfg = dashboard_config.get("https") or {}
    servers = []
    tasks = []        # server tasks: the process ends when one of them ends
    helpers = []      # background jobs (certbot, renewal): never end the process
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    def stop_all():
        if not stopping.is_set():
            logger.info("🛑 Stop signal received, shutting down …")
            # Watchdog: whatever happens, the process is gone after 8 s, and if it
            # gets that far the journal shows where it was stuck.
            import faulthandler
            try:
                faulthandler.dump_traceback_later(8, exit=True)
            except Exception:
                pass
        stopping.set()
        for srv in servers:
            srv.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_all)
        except (NotImplementedError, RuntimeError):
            pass

    async def start(srv):
        servers.append(srv)
        task = asyncio.create_task(srv.serve())
        while not srv.started and not task.done() and not stopping.is_set():
            await asyncio.sleep(0.05)
        if not srv.started and not stopping.is_set():
            port_ = srv.config.port
            logger.error(f"❌ Cannot listen on port {port_}: it is in use or not permitted. Is the dashboard "
                         f"already running (systemctl status hblink4-dash, or run_all.sh)? "
                         f"Check: sudo ss -tlpn 'sport = :{port_}'")
            raise SystemExit(1)
        return task

    async def start_optional(srv, what: str):
        try:
            tasks.append(await start(srv))
            return True
        except (Exception, SystemExit):
            servers.remove(srv)
            logger.error(f"🔒 {what}: port {srv.config.port} unavailable (in use, or CAP_NET_BIND_SERVICE missing)")
            return False

    if https_cfg.get("enabled"):
        from .tls import CertManager, build_http_app
        _tls_manager = cm = CertManager(https_cfg, state._data_dir)
        http_port = int(https_cfg.get("http_port", 80))
        http_app = build_http_app(cm, redirect=True)
        await start_optional(_Server.make(http_app, host=host, port=http_port, lifespan="off"),
                             "Let's Encrypt challenge / redirect")

        def tls_server(lifespan: str):
            return _Server.make(app, host=host, port=cm.https_port, lifespan=lifespan,
                                ssl_certfile=cm.cert_file, ssl_keyfile=cm.key_file)

        async def renew_loop(main):
            while True:
                await asyncio.sleep(12 * 3600)
                if await cm.renew() and main.config.ssl is not None:
                    main.config.ssl.load_cert_chain(cm.cert_file, cm.key_file)
                    logger.info("🔒 Renewed certificate loaded")

        if cm.have_cert():
            main = tls_server("on")
            tasks.append(await start(main))
            print(f"Dashboard on {cm.https_url()}")
            if port not in (http_port, cm.https_port):
                await start_optional(_Server.make(http_app, host=host, port=port, lifespan="off"),
                                     "redirect to HTTPS")
            helpers.append(asyncio.create_task(renew_loop(main)))
        else:
            # Serve over HTTP right away; get the certificate in the background
            tasks.append(await start(_Server.make(app, host=host, port=port)))
            print(f"Dashboard on http://{host}:{port} (requesting a certificate in the background)")

            async def obtain_then_serve():
                if await cm.obtain() and not stopping.is_set():
                    main = tls_server("off")       # app already started by the HTTP server
                    if await start_optional(main, "HTTPS"):
                        print(f"Dashboard on {cm.https_url()}")
                        logger.info("🔒 HTTPS is up. Restart the dashboard once to make "
                                    f"port {port} redirect to HTTPS.")
                        await renew_loop(main)
                elif not stopping.is_set():
                    logger.error("🔒 No certificate: the dashboard stays on plain HTTP. Fix the problem above "
                                 "and restart the dashboard.")
            helpers.append(asyncio.create_task(obtain_then_serve()))
    else:
        tasks.append(await start(_Server.make(app, host=host, port=port)))
        print(f"Dashboard on http://{host}:{port}")
    print("Press CTRL+C to stop\n")

    # Wait for a stop signal or a server ending, then stop the rest
    stop_wait = asyncio.create_task(stopping.wait())
    await asyncio.wait([*tasks, stop_wait], return_when=asyncio.FIRST_COMPLETED)
    stop_all()
    for h in helpers:
        h.cancel()
    # Servers shut down gracefully (lifespan shutdown saves the data)
    pending = [t for t in tasks if not t.done()]
    if pending:
        done, still = await asyncio.wait(pending, timeout=5)
        if still:
            logger.warning(f"🛑 {len(still)} server(s) did not stop within 5 s - exiting anyway")
    logger.info("🛑 Servers stopped, exiting")
    # asyncio.run() would now wait for worker threads that are still
    # downloading (callsign list, avatars) -- minutes on a slow line, which made
    # "systemctl stop/restart" hang. Everything is saved; leave right here.
    _hard_exit(0)


def _hard_exit(code: int):
    try:
        save_persistent_data()
    except Exception as e:
        logger.error(f"Saving data on exit failed: {e}")
    try:
        state.auth._db.close()
    except Exception:
        pass
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def run():
    """Console-script entry point (hblink4-dashboard).

    Bind address and port come from the 'web' section of config.json; optional
    CLI arguments override them for one-off/development runs. With https.enabled
    the dashboard is served over HTTPS (see dashboard/tls.py).

    The app object is passed to uvicorn rather than the "dashboard.server:app"
    import string so this module is not executed a second time under a different
    name -- two copies would mean two DashboardState objects and two atexit
    saves, the later one clobbering the real data.
    """
    web_config = dashboard_config.get('web', {})
    host = sys.argv[1] if len(sys.argv) > 1 else web_config.get('bind', '0.0.0.0')
    port = int(sys.argv[2]) if len(sys.argv) > 2 else int(web_config.get('port', 8080))
    code = 0
    try:
        asyncio.run(_serve(host, port))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except KeyboardInterrupt:
        pass
    _hard_exit(code)


if __name__ == "__main__":
    run()
