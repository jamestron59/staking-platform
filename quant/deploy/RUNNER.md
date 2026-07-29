# Self-hosted runner setup

Registers the trading server as a GitHub Actions runner, so the workflows in
`.github/workflows/` can deploy to it and report its state. This is the path
that works when the operator has no direct network route to the box.

## Prerequisite: the repository must be private

Non-negotiable. On a public repository, anyone can fork it and open a pull
request whose workflow runs on your runner — GitHub documents this as the
reason not to use self-hosted runners with public repos. On a machine holding a
trading key, that is a remote key-exfiltration path open to the internet.

Check: **Settings → General → Danger Zone → Change visibility**. Confirm first
whether any deployment (Vercel, Pages) depends on the repo being public.

## The honest security position

A runner that installs systemd services needs root. And because the runner user
owns the checkout it runs from, *any* sudo right over that checkout is
root-equivalent — it can edit `install.sh` before sudo executes it. A narrow
sudoers file here would be theatre.

So treat it as true: **the runner user is effectively root on this box.**

What actually bounds the damage is the key scope, not the runner config:

- The trading key is an **agent (API) wallet**. It can trade but cannot
  withdraw. Full root on this server therefore costs you open positions, not
  the balance.
- The HL account holds only risk capital. The rest stays off-exchange.
- Push access to the repository is the real perimeter. Everyone with it can run
  code as root here. Keep the collaborator list to yourself.

If that is not acceptable, do not use a runner — run the deploy commands by
hand instead. Both paths are supported.

## Register the runner

GitHub generates a token and shows the current runner version on
**Settings → Actions → Runners → New self-hosted runner**. Copy the commands
from that page rather than from here; a version number written down in a repo
goes stale.

Run them as a dedicated user, not as root and not as the `hlq` service user:

```bash
sudo useradd -m -s /bin/bash ghrunner
sudo -u ghrunner -i
# then paste GitHub's download + tar + config.sh commands, adding the label:
./config.sh --url https://github.com/<owner>/<repo> \
            --token <TOKEN-FROM-GITHUB> \
            --labels hl-trader \
            --unattended
```

The `hl-trader` label matters: every workflow targets
`runs-on: [self-hosted, hl-trader]`, so adding a second server later cannot
accidentally receive a deploy meant for this one.

Install it as a service so it survives reboots:

```bash
exit                       # back to your sudo-capable user
cd /home/ghrunner/actions-runner
sudo ./svc.sh install ghrunner
sudo ./svc.sh start
sudo ./svc.sh status
```

## Grant sudo

```bash
sudo tee /etc/sudoers.d/ghrunner >/dev/null <<'EOF'
# Root-equivalent by construction (see RUNNER.md). Written explicitly rather
# than disguised as a narrow grant.
ghrunner ALL=(ALL) NOPASSWD: ALL
EOF
sudo chmod 0440 /etc/sudoers.d/ghrunner
sudo visudo -c
```

## Workflows must live on the default branch

`workflow_dispatch` only appears in the Actions UI for workflow files present
on the repository's **default branch**. While these live on a feature branch
they cannot be triggered. Merge them to `main` first; after that you can still
choose which branch to *run from* when dispatching.

## What each workflow does

| Workflow | Effect | Spends money |
|---|---|---|
| `hlq inventory` | Read-only report: processes, services, disk, clock, HL positions and resting orders | no |
| `hlq deploy` | Tests, installs/updates, runs preflight. Optionally restarts the recorder | no |
| `hlq control` | `status`, start/stop recorder, stop trader, cancel all orders, **start trader** | only `start-trader` |

None of them are triggered by `push`. Auto-deploying to a box that holds
positions on every commit is how an untested change reaches real money
unattended.

`start-trader` additionally requires typing `trade` in the confirmation input,
mirroring the CLI's interactive speed bump.

## First run

```
1. Make the repo private.
2. Register the runner (above).
3. Merge the workflows to the default branch.
4. Actions → "hlq inventory" → Run workflow, with your HL address.
   Read the log. This is how you find out what is already on the box.
5. Actions → "hlq deploy" → Run workflow.
6. Actions → "hlq control" → start-recorder.
7. Weeks later, after paper trading: control → start-trader, confirm "trade".
```

Step 4 before step 5, always. If another bot is already trading that account,
installing this one alongside it is the wrong move — see the shared-account
section in the main README.
