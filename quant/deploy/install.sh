#!/usr/bin/env bash
# hlq installer. Idempotent: safe to re-run for upgrades.
#
# Deliberately does NOT start the trader. It installs, validates, and stops —
# starting a process that spends money is a separate, conscious act.
#
#   sudo ./deploy/install.sh
#   sudo systemctl enable --now hlq-recorder     # safe: read-only, no key
#   ...weeks later, after paper...
#   sudo systemctl enable --now hlq-trader       # spends money

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/hlq
CONFIG_DIR=/etc/hlq
DATA_DIR=/var/lib/hlq
LOG_DIR=/var/log/hlq
SERVICE_USER=hlq

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "run as root"

# ---- time synchronisation -------------------------------------------------
# Every recorded timestamp and every latency measurement depends on this. A
# host whose clock drifts produces data that looks fine and backtests wrong.
info "checking time synchronisation"
if command -v timedatectl >/dev/null 2>&1; then
    if ! timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
        info "clock not synchronised — enabling systemd-timesyncd"
        timedatectl set-ntp true || die "could not enable NTP; fix this before recording"
        sleep 3
        timedatectl show -p NTPSynchronized --value | grep -q yes \
            || echo "WARNING: clock still not synchronised. Do not trust recorded data yet."
    fi
    timedatectl show -p NTPSynchronized --value | sed 's/^/    NTPSynchronized=/'
else
    echo "WARNING: timedatectl not available. Verify NTP manually."
fi

# ---- user and directories -------------------------------------------------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    info "creating service user $SERVICE_USER"
    useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_DIR" "$SERVICE_USER"
fi

info "creating directories"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$INSTALL_DIR" "$DATA_DIR" "$LOG_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$DATA_DIR/data" "$DATA_DIR/state"
install -d -o root -g "$SERVICE_USER" -m 0750 "$CONFIG_DIR"

# ---- code and virtualenv --------------------------------------------------
info "installing code to $INSTALL_DIR"
for item in hlq config deploy pyproject.toml requirements.txt README.md; do
    [[ -e "$REPO_DIR/$item" ]] && cp -r "$REPO_DIR/$item" "$INSTALL_DIR/"
done
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
    info "creating virtualenv"
    python3 -m venv "$INSTALL_DIR/venv"
fi
info "installing dependencies"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
"$INSTALL_DIR/venv/bin/pip" install --quiet -e "$INSTALL_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ---- configuration --------------------------------------------------------
if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
    info "installing starter config (recording only)"
    cp "$REPO_DIR/config/config.example.yaml" "$CONFIG_DIR/config.yaml"
    sed -i 's|^  root: "data"|  root: "/var/lib/hlq/data"|' "$CONFIG_DIR/config.yaml"
    sed -i 's|^log_dir: "logs"|log_dir: "/var/log/hlq"|' "$CONFIG_DIR/config.yaml"
    sed -i 's|^state_dir: "state"|state_dir: "/var/lib/hlq/state"|' "$CONFIG_DIR/config.yaml"
    chown root:"$SERVICE_USER" "$CONFIG_DIR/config.yaml"
    chmod 0640 "$CONFIG_DIR/config.yaml"
else
    info "keeping existing $CONFIG_DIR/config.yaml"
fi

if [[ ! -f "$CONFIG_DIR/hlq.env" ]]; then
    info "creating empty secret file"
    cat > "$CONFIG_DIR/hlq.env" <<'ENVEOF'
# Agent (API) wallet private key. NOT your master key — an agent wallet can
# trade but cannot withdraw, which is the main protection when the bot runs on
# the main account with no subaccount boundary.
HL_API_SECRET=
ENVEOF
fi
# Enforced every run, not just at creation: a key readable by other users is
# not protected by anything else in this system.
chown root:"$SERVICE_USER" "$CONFIG_DIR/hlq.env"
chmod 0640 "$CONFIG_DIR/hlq.env"

# ---- systemd --------------------------------------------------------------
info "installing systemd units"
cp "$REPO_DIR/deploy/hlq-recorder.service" /etc/systemd/system/
cp "$REPO_DIR/deploy/hlq-trader.service" /etc/systemd/system/
systemctl daemon-reload

if [[ -f "$REPO_DIR/deploy/logrotate.hlq" ]]; then
    cp "$REPO_DIR/deploy/logrotate.hlq" /etc/logrotate.d/hlq
fi
install -m 0755 "$REPO_DIR/deploy/healthcheck.sh" /usr/local/bin/hlq-healthcheck

# ---- disk sizing ----------------------------------------------------------
AVAIL_GB=$(df -BG --output=avail "$DATA_DIR" | tail -1 | tr -dc '0-9')
info "free space on $DATA_DIR: ${AVAIL_GB}G"
if (( AVAIL_GB < 40 )); then
    echo "WARNING: full L2 book capture runs roughly 0.5-1 GB per coin per day."
    echo "         ${AVAIL_GB}G will not hold a month. Reduce coins, or mount more disk."
fi

# ---- validate -------------------------------------------------------------
info "validating configuration"
if sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/hlq" --config "$CONFIG_DIR/config.yaml" preflight; then
    :
else
    echo "preflight reported a blocking problem — fix $CONFIG_DIR/config.yaml before starting"
fi

cat <<EOF

Installed. Nothing is running yet.

  1. Start recording (safe — read-only, needs no key):
       systemctl enable --now hlq-recorder
       journalctl -u hlq-recorder -f

  2. Measure this host's latency to HL and put the result in the config:
       sudo -u $SERVICE_USER $INSTALL_DIR/venv/bin/hlq --config $CONFIG_DIR/config.yaml latency

  3. After weeks of recording, check the data has no holes:
       sudo -u $SERVICE_USER $INSTALL_DIR/venv/bin/hlq --config $CONFIG_DIR/config.yaml verify

  4. Only when you are ready to spend money: put the agent wallet key in
     $CONFIG_DIR/hlq.env, switch the config to the live profile, re-run
     preflight, then:
       systemctl enable --now hlq-trader

  Health check (wire to cron or your monitoring):
       hlq-healthcheck
EOF
