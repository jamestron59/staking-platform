#!/usr/bin/env bash
# hlq health check. Exit 0 healthy, 1 degraded, 2 critical.
#
# Checks the things that fail silently. A trading bot that is "running" is not
# the same as a trading bot that is working: the process can be perfectly alive
# while the feed has been dead for an hour.

set -uo pipefail

DATA_DIR=${HLQ_DATA_DIR:-/var/lib/hlq/data}
STATE_DIR=${HLQ_STATE_DIR:-/var/lib/hlq/state}
LOG_DIR=${HLQ_LOG_DIR:-/var/log/hlq}
STALE_MINUTES=${HLQ_STALE_MINUTES:-5}
MIN_FREE_GB=${HLQ_MIN_FREE_GB:-5}

status=0
note() { echo "[$1] $2"; }
degrade() { status=$(( status < 1 ? 1 : status )); note WARN "$1"; }
critical() { status=2; note CRIT "$1"; }

# ---- services -------------------------------------------------------------
for unit in hlq-recorder hlq-trader; do
    if systemctl list-unit-files "$unit.service" >/dev/null 2>&1 \
       && systemctl is-enabled "$unit" >/dev/null 2>&1; then
        if systemctl is-active --quiet "$unit"; then
            note OK "$unit active"
        else
            critical "$unit is enabled but not running"
        fi
    fi
done

# ---- is data actually arriving? -------------------------------------------
# The check that matters: a healthy TCP connection can deliver nothing.
newest=$(find "$DATA_DIR" -name '*.jsonl.gz' -mmin "-$STALE_MINUTES" 2>/dev/null | head -1)
if [[ -d "$DATA_DIR" ]]; then
    if [[ -z "$newest" ]]; then
        critical "no shard written in the last ${STALE_MINUTES}m — the feed is dead or the recorder is stuck"
    else
        note OK "data flowing (shard touched within ${STALE_MINUTES}m)"
    fi
fi

# ---- disk -----------------------------------------------------------------
if [[ -d "$DATA_DIR" ]]; then
    free_gb=$(df -BG --output=avail "$DATA_DIR" 2>/dev/null | tail -1 | tr -dc '0-9')
    if [[ -n "$free_gb" ]]; then
        if (( free_gb < MIN_FREE_GB )); then
            critical "only ${free_gb}G free on $DATA_DIR — capture will start failing"
        elif (( free_gb < MIN_FREE_GB * 3 )); then
            degrade "${free_gb}G free on $DATA_DIR"
        else
            note OK "${free_gb}G free"
        fi
    fi
fi

# ---- clock ----------------------------------------------------------------
if command -v timedatectl >/dev/null 2>&1; then
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
        note OK "clock synchronised"
    else
        degrade "clock NOT synchronised — recorded timestamps and latency numbers are untrustworthy"
    fi
fi

# ---- kill switch ----------------------------------------------------------
ks="$STATE_DIR/killswitch.json"
if [[ -f "$ks" ]]; then
    if grep -q '"tripped": *true' "$ks" 2>/dev/null; then
        # Not critical: the switch doing its job is the system working. But it
        # will not clear itself, so a human has to look.
        degrade "kill switch is TRIPPED and will not reset itself: $(head -c 300 "$ks")"
    else
        note OK "kill switch clear"
    fi
fi

# ---- unresolved orders ----------------------------------------------------
orders="$STATE_DIR/orders.json"
if [[ -f "$orders" ]] && command -v python3 >/dev/null 2>&1; then
    unresolved=$(python3 -c "
import json,sys
try:
    d=json.load(open('$orders'))
    print(sum(1 for v in d.values() if not v.get('resolved')))
except Exception:
    print(0)
" 2>/dev/null)
    if [[ "${unresolved:-0}" -gt 5 ]]; then
        degrade "$unresolved unresolved orders in local state — reconciliation may be failing"
    fi
fi

# ---- recent errors --------------------------------------------------------
if [[ -f "$LOG_DIR/hlq.jsonl" ]]; then
    errs=$(tail -2000 "$LOG_DIR/hlq.jsonl" 2>/dev/null | grep -c '"level":"ERROR"')
    if [[ "${errs:-0}" -gt 20 ]]; then
        degrade "$errs errors in the last 2000 log lines"
    fi
fi

exit $status
