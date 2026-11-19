#!/bin/bash
# Start both HBlink4 server and dashboard
# Usage: ./run_all.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Virtual environment not found. Create it first:${NC}"
    echo -e "${YELLOW}  python3 -m venv venv${NC}"
    echo -e "${YELLOW}  ./venv/bin/pip install -r requirements.txt -r requirements-dashboard.txt${NC}"
    exit 1
fi

# Neither program starts without its configuration, and the failure is much
# clearer said here than as a traceback two screens further down.
if [ ! -f "config/config.json" ]; then
    echo -e "${YELLOW}No config/config.json yet. Create it from the sample:${NC}"
    echo -e "${YELLOW}  cp config/config_sample.json config/config.json${NC}"
    echo -e "${YELLOW}Then change 'passphrase' from CHANGE-ME to a password of your choosing.${NC}"
    exit 1
fi

if grep -q '"passphrase": "CHANGE-ME"' config/config.json; then
    echo -e "${YELLOW}config/config.json still has the placeholder passphrase CHANGE-ME.${NC}"
    echo -e "${YELLOW}Repeaters will connect with it as-is, but anyone who has read the${NC}"
    echo -e "${YELLOW}sample config knows it. Set a real one before going live.${NC}"
    echo
fi

if [ ! -f "dashboard/config.json" ]; then
    echo -e "${YELLOW}No dashboard/config.json -- creating it from the sample.${NC}"
    cp dashboard/config_sample.json dashboard/config.json
fi

# ---------------------------------------------------------------- preflight
# The most common reason for "HBlink4 crashes right away": a second copy. The
# systemd services (installed by scripts/install_services.sh) and this script
# must not run at the same time -- both bind the same UDP and TCP ports.
if command -v systemctl >/dev/null 2>&1; then
    RUNNING=""
    for svc in hblink4 hblink4-dash; do
        systemctl is-active --quiet "$svc" 2>/dev/null && RUNNING="$RUNNING $svc"
    done
    if [ -n "$RUNNING" ]; then
        echo -e "${RED}The systemd service(s)${RUNNING} are already running.${NC}"
        echo -e "${YELLOW}Use them instead of this script:${NC}"
        echo -e "${YELLOW}  systemctl status hblink4 hblink4-dash${NC}"
        echo -e "${YELLOW}  journalctl -u hblink4 -u hblink4-dash -f${NC}"
        echo -e "${YELLOW}Or stop them first to run in the foreground:${NC}"
        echo -e "${YELLOW}  sudo systemctl stop hblink4 hblink4-dash${NC}"
        exit 1
    fi
fi

# Ports from the configs; refuse to start into a port that is already taken.
read -r DMR_PORT WEB_PORT < <(python3 - <<'PY' 2>/dev/null || echo "62031 8080"
import json
try:
    g = json.load(open("config/config.json")).get("global", {})
except Exception:
    g = {}
try:
    w = json.load(open("dashboard/config.json")).get("web", {})
except Exception:
    w = {}
print(g.get("port_ipv4", 62031), w.get("port", 8080))
PY
)
port_busy() {   # $1 = udp|tcp, $2 = port
    python3 - "$1" "$2" <<'PY'
import socket, sys
kind, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if kind == "udp" else socket.SOCK_STREAM)
if kind == "tcp":
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", port))
except OSError:
    sys.exit(0)      # busy
sys.exit(1)          # free
PY
}
BUSY=0
if port_busy udp "$DMR_PORT"; then
    echo -e "${RED}UDP port ${DMR_PORT} (DMR) is already in use.${NC}"
    command -v ss >/dev/null 2>&1 && ss -Hlunp "sport = :${DMR_PORT}" 2>/dev/null
    BUSY=1
fi
if port_busy tcp "$WEB_PORT"; then
    echo -e "${RED}TCP port ${WEB_PORT} (dashboard) is already in use.${NC}"
    command -v ss >/dev/null 2>&1 && ss -Hltnp "sport = :${WEB_PORT}" 2>/dev/null
    BUSY=1
fi
if [ "$BUSY" = 1 ]; then
    echo -e "${YELLOW}Another HBlink4/dashboard, or another DMR server (e.g. ADN/FreeDMR proxy) uses it.${NC}"
    echo -e "${YELLOW}Find it with: sudo ss -tulpn | grep -E ':(${DMR_PORT}|${WEB_PORT})\\b'${NC}"
    echo -e "${YELLOW}Stop that program, or change the port in config/config.json / dashboard/config.json.${NC}"
    exit 1
fi

# Running as root in a checkout owned by someone else leaves root-owned files in
# logs/ and dashboard/data/ that the service user can no longer write.
OWNER="$(stat -c %U "$SCRIPT_DIR" 2>/dev/null || echo root)"
if [ "$(id -u)" -eq 0 ] && [ "$OWNER" != "root" ] && [ "${ALLOW_ROOT:-0}" != 1 ]; then
    echo -e "${RED}Please do not run this as root: the checkout belongs to '${OWNER}'.${NC}"
    echo -e "${YELLOW}Files created now would be owned by root and break the services later. Use:${NC}"
    echo -e "${YELLOW}  sudo -u ${OWNER} ./run_all.sh${NC}"
    echo -e "${YELLOW}(ALLOW_ROOT=1 ./run_all.sh overrides this check.)${NC}"
    exit 1
fi

# Activate virtual environment
echo -e "${BLUE}Activating virtual environment...${NC}"
source venv/bin/activate

# Check if dashboard requirements are installed
if ! python3 -c "import fastapi" 2>/dev/null; then
    echo -e "${YELLOW}Dashboard dependencies not found. Installing...${NC}"
    pip install -r requirements-dashboard.txt
fi

# Function to cleanup on exit (runs once)
CLEANED=0
cleanup() {
    [ "$CLEANED" = 1 ] && return
    CLEANED=1
    echo -e "\n${YELLOW}Stopping services...${NC}"
    kill $(jobs -p) 2>/dev/null || true
    wait 2>/dev/null || true
    echo -e "${GREEN}All services stopped${NC}"
}

trap cleanup EXIT
trap 'exit 130' INT TERM

echo -e "${GREEN}Starting HBlink4 services...${NC}"
echo

# Start dashboard in background
echo -e "${BLUE}Starting Dashboard (bind/port from dashboard/config.json)...${NC}"
python3 run_dashboard.py &
DASHBOARD_PID=$!

# Give dashboard time to start
sleep 2

# Start HBlink4 server
echo -e "${BLUE}Starting HBlink4 server...${NC}"
python3 run_hblink.py config/config.json &
HBLINK_PID=$!

echo
echo -e "${GREEN}Services started:${NC}"
echo -e "  Dashboard: see 'web' section of dashboard/config.json (PID: $DASHBOARD_PID)"
echo -e "  HBlink4:   see 'global' bind/port in config/config.json (PID: $HBLINK_PID)"
echo
echo -e "${YELLOW}Press CTRL+C to stop all services${NC}"
echo

# Wait until either program exits, then stop the other one too -- otherwise a
# crashed HBlink4 would go unnoticed behind a still-running dashboard.
set +e
wait -n $HBLINK_PID $DASHBOARD_PID
STATUS=$?
if ! kill -0 $HBLINK_PID 2>/dev/null; then
    echo -e "${RED}HBlink4 stopped (exit $STATUS) -- see the messages above.${NC}"
else
    echo -e "${RED}The dashboard stopped (exit $STATUS) -- see the messages above.${NC}"
fi
exit $STATUS
