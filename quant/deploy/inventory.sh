#!/usr/bin/env bash
# Read-only inventory of a trading server. Changes nothing, starts nothing,
# stops nothing.
#
# Deliberately never prints secrets: environment variables are shown as names
# only, and files that look like key material are reported by permission and
# size, never by content. Paste the output anywhere without reading it first.
#
#   curl -sO <raw-url>/inventory.sh && bash inventory.sh
#   # or, with an HL address to also report on-chain account state:
#   bash inventory.sh 0xYourAddress

set -uo pipefail
ADDR="${1:-}"

hr() { printf '\n=== %s ===\n' "$1"; }

hr "host"
uname -a 2>/dev/null
echo "uptime:$(uptime -p 2>/dev/null || uptime 2>/dev/null)"
if command -v timedatectl >/dev/null 2>&1; then
    echo "clock synced: $(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
fi

hr "what is running that looks like a bot"
# Match on interpreters and common bot names, not on a fixed path — we do not
# know how the existing process was installed.
#
# Command lines are redacted and truncated. A process started with its key as
# an argument (--api-key=0x..., PRIVATE_KEY=... in the args) would otherwise
# leak it here, which would break this script's one promise.
ps -eo pid,etime,rss,user,args 2>/dev/null \
  | grep -iE 'python|node|hlq|bot|trade|hyperliquid' \
  | grep -v grep \
  | sed -E 's/(0x[0-9a-fA-F]{16,})/<REDACTED-HEX>/g' \
  | sed -E 's/((key|secret|token|password|passwd|pass|seed|mnemonic|private)[=: ]+)[^ ]+/\1<REDACTED>/Ig' \
  | cut -c1-220 \
  | head -25 || echo "(none)"

hr "systemd services"
systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null \
  | grep -iE 'bot|trade|hlq|hyper|python|node' || echo "(no matching running services)"
echo "--- enabled but not running ---"
systemctl list-unit-files --type=service --state=enabled --no-pager --no-legend 2>/dev/null \
  | grep -iE 'bot|trade|hlq|hyper' || echo "(none)"

hr "listening sockets"
(ss -tulpn 2>/dev/null || netstat -tulpn 2>/dev/null) | head -20

hr "likely install directories"
for d in /opt /srv /root /home/*; do
    [[ -d "$d" ]] || continue
    find "$d" -maxdepth 3 \
        \( -name "*.py" -o -name "package.json" -o -name "*.service" \) \
        -newermt "-400 days" 2>/dev/null \
      | grep -iE 'bot|trade|hyper|hlq|strategy' | head -8
done | sort -u | head -25 || echo "(nothing obvious)"

hr "credential files present (NAMES AND PERMISSIONS ONLY, never contents)"
for pat in '.env' '*.env' 'hlq.env' 'config.yaml' 'config.json' '*.key' '*.pem'; do
    # Exclude the system CA bundle: hundreds of files, none of them yours.
    find /opt /etc /root /home -maxdepth 4 -name "$pat" 2>/dev/null \
      | grep -v '^/etc/ssl/certs/' | grep -v '^/etc/ca-certificates/' | head -10
done | sort -u | while read -r f; do
    printf '  %s  mode=%s owner=%s size=%s\n' \
        "$f" "$(stat -c %a "$f" 2>/dev/null)" \
        "$(stat -c %U:%G "$f" 2>/dev/null)" "$(stat -c %s "$f" 2>/dev/null)"
done

hr "environment variable NAMES in bot processes (no values)"
for pid in $(pgrep -f 'python|node' 2>/dev/null | head -6); do
    name=$(ps -p "$pid" -o comm= 2>/dev/null)
    vars=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | cut -d= -f1 \
           | grep -iE 'key|secret|token|wallet|hl_|api' | tr '\n' ' ')
    [[ -n "$vars" ]] && echo "  pid=$pid ($name): $vars"
done
echo "(names only — values are never read)"

hr "disk"
df -h / /var 2>/dev/null | grep -v tmpfs
echo "--- largest data dirs ---"
du -sh /var/lib/* /opt/* 2>/dev/null | sort -rh | head -8

hr "recent errors in journal"
journalctl --since "24 hours ago" -p err --no-pager 2>/dev/null | tail -15 \
  || echo "(journal unavailable)"

hr "network reachability to Hyperliquid"
if curl -sS -o /dev/null -w "  info endpoint: HTTP %{http_code} in %{time_total}s\n" \
   -X POST https://api.hyperliquid.xyz/info \
   -H 'Content-Type: application/json' -d '{"type":"meta"}' --max-time 15; then :;
else echo "  UNREACHABLE — the bot cannot trade from this host"; fi

if [[ -n "$ADDR" ]]; then
    hr "Hyperliquid account state for $ADDR (public data, no key needed)"
    curl -sS -X POST https://api.hyperliquid.xyz/info \
        -H 'Content-Type: application/json' \
        -d "{\"type\":\"clearinghouseState\",\"user\":\"$ADDR\"}" --max-time 20 \
      | python3 -c "
import json,sys
try: d = json.load(sys.stdin)
except Exception: print('  (could not parse response)'); sys.exit()
ms = d.get('marginSummary', {})
print(f\"  account value : \${float(ms.get('accountValue',0)):,.2f}\")
print(f\"  margin used   : \${float(ms.get('totalMarginUsed',0)):,.2f}\")
print(f\"  withdrawable  : \${float(d.get('withdrawable',0)):,.2f}\")
pos = [p['position'] for p in d.get('assetPositions',[]) if abs(float(p['position']['szi']))>0]
print(f'  OPEN POSITIONS: {len(pos)}')
for p in pos:
    print(f\"    {p['coin']:>6} size={p['szi']:>12} entry={p.get('entryPx')} \"
          f\"liq={p.get('liquidationPx')} uPnL={p.get('unrealizedPnl')}\")
" 2>/dev/null || echo "  (query failed)"

    echo "  --- resting orders ---"
    curl -sS -X POST https://api.hyperliquid.xyz/info \
        -H 'Content-Type: application/json' \
        -d "{\"type\":\"openOrders\",\"user\":\"$ADDR\"}" --max-time 20 \
      | python3 -c "
import json,sys
try: o = json.load(sys.stdin)
except Exception: print('  (could not parse)'); sys.exit()
print(f'  {len(o)} resting order(s)')
for x in o[:15]:
    print(f\"    {x.get('coin'):>6} {x.get('side')} sz={x.get('sz')} px={x.get('limitPx')} oid={x.get('oid')}\")
" 2>/dev/null || echo "  (query failed)"
fi

hr "done"
echo "Nothing was started, stopped, or modified."
