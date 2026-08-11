---
name: vastai
description: "Rent, watch, and tear down Vast.ai GPU instances safely. Use whenever the user wants to spin up a cloud GPU box, run something on a GPU/remote machine, search GPU offers, check what's running, ssh into / copy files to a rented box, monitor cost, or shut instances down. Triggers: /vastai, \"rent a GPU\", \"spin up a vast box\", \"vast.ai\", \"GPU instance\", \"how much am I spending on GPUs\", \"kill my instances\"."
---

# vastai: spin up, watch, and tear down GPU boxes safely

This skill drives Vast.ai GPU rentals through one control-plane CLI, `vast.py`. It tracks
every box that gets rented, shows what is running and what it costs, and gives each box an
auto-destroy deadline enforced by a background watchdog.

## The one rule: nothing bills silently

A rented GPU bills by the second until it is destroyed. So every box gets a deadline
(`up --hours N`) and a watchdog that destroys it at that deadline. Whenever you rent
something, start a watchdog and keep it running. If stray boxes might exist, `vast.py ps`
lists everything on the account and `vast.py down` / `nuke` cleans up.

## Tooling

Run the bundled CLI with the system python (stdlib only, no venv needed):

```
python3 ~/.claude/skills/vastai/vast.py <command> [flags]
```

It wraps the official `vastai` CLI. Prerequisites, which the script checks and reports on:

- `vastai` CLI installed (`uv tool install vastai`, or `pipx install vastai`).
- Authenticated once with `vastai set api-key <KEY>`, key from https://console.vast.ai/ under
  Account. If the user still needs to do this, have them run it themselves via
  `! vastai set api-key <KEY>` so you never echo the key.

State (tracked boxes, prices, deadlines) lives in `~/.vast-claude/state.json`, independent of
the working directory. The ssh key `~/.ssh/id_ed25519` is generated and registered on the
first `up`.

## Command map

| goal | command |
|------|---------|
| check account credit and burn rate | `vast.py balance` |
| browse offers (read-only) | `vast.py search --gpu RTX_4090 --gpus 1` |
| rent a box with a deadline | `vast.py up --gpu RTX_4090 --hours 3 --label NAME` |
| enforce deadlines (background) | `vast.py watchdog` |
| status, uptime, cost, time left | `vast.py status` |
| live terminal monitor | `vast.py watch` |
| live HTML dashboard | `vast.py dashboard` |
| run a command on the box | `vast.py run "nvidia-smi"` |
| upload a file or dir | `vast.py put ./train.py /root/` |
| upload a whole dir tree (fast) | `vast.py sync ./project` |
| download a result | `vast.py pull /root/out.txt ./` |
| interactive shell | `vast.py ssh --exec` |
| boot and container logs | `vast.py logs` |
| extend the deadline | `vast.py extend --hours 2` |
| destroy one box | `vast.py down` |
| list all account instances | `vast.py ps` |
| destroy everything | `vast.py nuke` |

Every box-targeting command takes `--id <iid>` or `--label <name>`. With exactly one tracked
box the target is implied, so single-box sessions need no target flag. Use
`vast.py <command> --help` for a command's flags.

## Workflows

### Spin up a box and run something on it

1. Check funds and price first. Run `vast.py balance`, then
   `vast.py search --gpu <MODEL> --gpus <N>` for real prices. Confirm the GPU and price with
   the user before renting unless they already gave specifics. Renting costs money.
2. Rent with a deadline and a label:
   `vast.py up --gpu RTX_4090 --hours 3 --label trainer`. Defaults are 1x RTX_4090, a 3 hour
   deadline, the `vastai/pytorch` image and 40 GB of disk. `up` waits for boot, attaches the
   ssh key, prints `READY ✓`, then activates the image's baked-in python env (torch is
   already at `/venv/main`, nothing is re-downloaded). Pin an exact offer with
   `--offer-id <id>` from `search`, or pick a different image with `--image` / `--template`.
3. Start the watchdog in the background so the deadline holds even if this session ends:
   `python3 ~/.claude/skills/vastai/vast.py watchdog` (run_in_background). One watchdog
   guards all tracked boxes and exits by itself once none have deadlines left.
4. Work the box: `run` for commands, `put` / `sync` to upload, `pull` to fetch results,
   `ssh --exec` for a shell, `logs` to debug a bad boot.
5. Watch it: `vast.py status` for a snapshot, or `vast.py dashboard` for a live view during
   long jobs.
6. Tear down with `vast.py down`, or let the watchdog hit the deadline. Confirm with
   `vast.py ps` that nothing is left.

### "What do I have running, what am I spending?"

`vast.py status` for tracked boxes, cost so far and time left, plus `vast.py balance` for
credit, burn rate and rough runway. `vast.py ps` reveals anything untracked.

### "Kill everything"

`vast.py nuke` destroys every instance on the account. It asks for confirmation; only pass
`--yes` when the user clearly means all of them, right now.

## Conventions and cautions

- Renting and destroying spend money and lose data. Destroying is irreversible and wipes the
  disk, so `pull` anything needed first. Confirm before renting unless the user already gave
  concrete specs, and always confirm before `nuke`.
- Pair every `up` with a running `watchdog`, or with short `--hours`. If the user insists on
  `--hours 0` there is no auto-destroy at all, and they should hear that plainly.
- A destroy only counts once the account stops listing the instance. If `down`, `nuke` or the
  watchdog reports that a box is still listed, treat it as still billing: retry, and check
  https://console.vast.ai/ if it keeps failing.
- Prefer `--label` names so multi-box sessions stay readable (`--label trainer`,
  `--label inference`), then reference boxes by label later.
- The image's pre-installed venv is activated automatically, so
  `vast.py run "python -c 'import torch'"` works with no PATH fiddling and no re-install. Use
  `run --bare` for the raw system shell. Detection happens once on boot, or on the first
  `run` / `ssh` for `--no-wait` boxes, and shows up in `status`.
- For long remote jobs, start them detached (`run "nohup … &"` or inside `tmux`) so they
  survive the ssh call returning, then poll with `logs`.
- If `search` finds nothing, loosen the filters: `--unverified`, a higher `--max-price`,
  fewer `--gpus`, or a different `--gpu`. Offer the user models to pick from (RTX_4090,
  RTX_3090, A100_PCIE, H100_SXM and so on).
- This skill is general-purpose GPU-box plumbing. The autonomous multi-GPU research swarm is
  a separate project (`vast-autoresearch`); do not conflate the two.

Full reference: `python3 ~/.claude/skills/vastai/vast.py --help` and the README.
