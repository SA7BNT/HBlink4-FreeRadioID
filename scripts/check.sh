#!/bin/bash
# HBlink4 health check: finds the usual reasons why the services do not start.
#
#   sudo ./scripts/check.sh
#
# Checks services, logs, configs (JSON), file ownership, the event socket,
# ports, the Python environment and stray manual starts, and prints a fix for
# every problem it finds. Changes nothing.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[1;34m'; N='\033[0m'
PROBLEMS=0
ok()   { echo -e "  ${G}✓${N} $*"; }
bad()  { echo -e "  ${R}✗${N} $*"; PROBLEMS=$((PROBLEMS + 1)); }
warn() { echo -e "  ${Y}!${N} $*"; }
fix()  { echo -e "      ${Y}→ $*${N}"; }
head_() { echo; echo -e "${B}$*${N}"; }

[ "$(id -u)" -eq 0 ] || warn "Not running as root: some checks (ownership, ports, logs) may be incomplete. Use: sudo $0"

SVC_USER="$(sed -n 's/^User=//p' /etc/systemd/system/hblink4-dash.service 2>/dev/null | head -1)"
SVC_USER="${SVC_USER:-$(stat -c %U "$REPO_ROOT")}"
PY="$REPO_ROOT/venv/bin/python"

# ------------------------------------------------------------------ services
head_ "Services"
if command -v systemctl >/dev/null 2>&1 && [ -f /etc/systemd/system/hblink4.service ]; then
    for svc in hblink4 hblink4-dash; do
        state="$(systemctl is-active "$svc" 2>/dev/null)"
        restarts="$(systemctl show "$svc" -p NRestarts --value 2>/dev/null)"
        if [ "$state" = "active" ] && [ "${restarts:-0}" -lt 3 ]; then
            ok "$svc: active"
        else
            bad "$svc: $state (restarts: ${restarts:-?})"
            if [ "$state" = "deactivating" ]; then
                fix "stop is hanging (older versions waited for running downloads): systemctl kill -s KILL $svc; systemctl start $svc"
            fi
            echo "      last problems (with time -- compare with the time of your last fix):"
            journalctl -u "$svc" -n 300 --no-pager -o short-iso 2>/dev/null \
                | grep -E "Error|error|✗|❌|Traceback|Exception|denied|readonly|in use|cannot|owner:|chown" \
                | grep -v " INFO" | tail -n 8 | awk '{t=substr($1,6,11); $1=$2=$3=""; sub(/^ +/,""); print "        " t "  " $0}'
            echo "      newest lines:"
            journalctl -u "$svc" -n 4 --no-pager -o short-iso 2>/dev/null | awk '{t=substr($1,6,11); $1=$2=$3=""; sub(/^ +/,""); print "        " t "  " $0}'
        fi
    done
    # The dashboard's stop watchdog writes a traceback when a stop got stuck
    STUCK="$(journalctl -u hblink4-dash -n 2000 --no-pager -o cat 2>/dev/null | awk '/Timeout \(0:00:08\)!/{buf=""; on=1} on{buf=buf $0 "\n"} END{printf "%s", buf}' | head -n 40)"
    if [ -n "$STUCK" ]; then
        warn "the dashboard's last stop got stuck; the watchdog ended it after 8 s. Where it hung:"
        echo "$STUCK" | grep -E 'Timeout|File "|Thread' | head -n 25 | sed 's/^/        /'
        fix "please send this block to the developer"
    fi
    echo "      service user: ${SVC_USER}"
else
    warn "systemd services not installed (sudo ./scripts/install_services.sh)"
fi

MANUAL="$(pgrep -af 'run_all.sh|run_hblink.py|run_dashboard.py' | grep -v "$$" || true)"
if [ -n "$MANUAL" ]; then
    bad "manual starts running next to the services:"
    echo "$MANUAL" | sed 's/^/        /'
    fix "pkill -f run_all.sh; pkill -f run_hblink.py; pkill -f run_dashboard.py"
fi

# ------------------------------------------------------------------- configs
head_ "Configuration files"
for f in config/config.json dashboard/config.json; do
    if [ ! -f "$f" ]; then
        bad "$f missing"; fix "cp ${f%.json}_sample.json $f   (dashboard: cp dashboard/config_sample.json dashboard/config.json)"
        continue
    fi
    msg="$(python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$f" 2>&1 | tail -1)"
    if [ -z "$msg" ]; then ok "$f: valid JSON"; else bad "$f: $msg"; fix "fix the line/column named above (often a missing or extra comma)"; fi
done
if [ -f config/config.json ] && grep -q '"passphrase": "CHANGE-ME"' config/config.json; then
    warn "config/config.json still uses the passphrase CHANGE-ME"
fi

# ----------------------------------------------------------------- ownership
head_ "File ownership (service user: ${SVC_USER})"
if [ "$(id -u)" -eq 0 ]; then
    FOREIGN="$(find "$REPO_ROOT" -path "$REPO_ROOT/venv" -prune -o -path "$REPO_ROOT/.git" -prune -o ! -user "$SVC_USER" -print 2>/dev/null)"
    if [ -z "$FOREIGN" ]; then
        ok "everything belongs to $SVC_USER"
    else
        bad "$(echo "$FOREIGN" | wc -l) file(s) do not belong to $SVC_USER, e.g.:"
        echo "$FOREIGN" | head -n 5 | sed 's/^/        /'
        fix "chown -R $SVC_USER:$SVC_USER $REPO_ROOT"
    fi
fi
SOCK=/tmp/hblink4.sock
if [ -S "$SOCK" ]; then
    owner="$(stat -c %U "$SOCK")"
    if [ "$owner" = "$SVC_USER" ]; then ok "$SOCK belongs to $SVC_USER"
    else bad "$SOCK belongs to $owner"; fix "rm -f $SOCK && systemctl restart hblink4-dash hblink4"; fi
fi

# --------------------------------------------------------------------- ports
head_ "Ports"
# Ports come from the admin area (SQLite) if set there, else from the config files.
CUR="$(python3 scripts/setup_apply.py current . 2>/dev/null || true)"
DMR_PORT="$(echo "$CUR" | python3 -c "import json,sys;print(json.load(sys.stdin).get('dmr_port',62031))" 2>/dev/null || echo 62031)"
WEB_PORT="$(echo "$CUR" | python3 -c "import json,sys;print(json.load(sys.stdin).get('web_port',8080))" 2>/dev/null || echo 8080)"
if command -v ss >/dev/null 2>&1; then
    for spec in "udp:$DMR_PORT:DMR (HBlink4)" "tcp:$WEB_PORT:dashboard" "tcp:80:HTTP/Let's Encrypt" "tcp:443:HTTPS"; do
        IFS=: read -r proto port label <<< "$spec"
        flag="-ltnp"; [ "$proto" = udp ] && flag="-lunp"
        line="$(ss -H $flag "sport = :$port" 2>/dev/null | head -1)"
        if [ -z "$line" ]; then
            echo "    $proto/$port ($label): free"
        else
            who="$(echo "$line" | grep -o 'users:(([^)]*' | sed 's/users:((//')"
            echo "    $proto/$port ($label): ${who:-in use}"
        fi
    done
    warn "Each port must be used by exactly one program. Another DMR server (ADN/FreeDMR proxy) on $DMR_PORT blocks HBlink4."
else
    warn "ss not installed (apt install iproute2): port check skipped"
fi

# -------------------------------------------------------------------- python
head_ "Python environment"
if [ -x "$PY" ]; then
    ok "$($PY --version 2>&1) in venv"
    for mod in dmr_utils3 fastapi uvicorn websockets PIL; do
        if "$PY" -c "import $mod" 2>/dev/null; then ok "module $mod"
        else
            if [ "$mod" = PIL ]; then warn "module Pillow missing (avatars are served unscaled)"
            else bad "module $mod missing"; fi
            fix "sudo -u $SVC_USER ./venv/bin/pip install -r requirements.txt -r requirements-dashboard.txt"
        fi
    done
    if [ "$(id -u)" -eq 0 ] && command -v runuser >/dev/null 2>&1; then
        out="$(cd "$REPO_ROOT" && runuser -u "$SVC_USER" -- "$PY" -c "import sys; sys.path.insert(0,'.'); import dashboard.server" 2>&1 | grep -E 'Error|❌|cannot' | head -3)"
        if [ -z "$out" ]; then ok "dashboard imports cleanly as $SVC_USER"
        else bad "dashboard does not start as $SVC_USER:"; echo "$out" | sed 's/^/        /'; fi
    fi
else
    bad "no venv at $REPO_ROOT/venv"
    fix "python3 -m venv venv && ./venv/bin/pip install -r requirements.txt -r requirements-dashboard.txt"
fi

# ------------------------------------------------------------------- summary
echo
if [ "$PROBLEMS" -eq 0 ]; then
    echo -e "${G}No problems found.${N}"
else
    echo -e "${R}$PROBLEMS problem(s) found${N} -- apply the fixes marked with →, then:"
    echo "  systemctl restart hblink4 hblink4-dash"
fi
echo "Logs: journalctl -u hblink4 -u hblink4-dash -n 50 --no-pager"
