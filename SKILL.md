---
name: vastai
description: "Rent, watch, and tear down Vast.ai GPU instances safely. Use whenever the user wants to spin up a cloud GPU box, run something on a GPU/remote machine, search GPU offers, check what's running, ssh into / copy files to a rented box, monitor cost, or shut instances down. Triggers: /vastai, \"rent a GPU\", \"spin up a vast box\", \"vast.ai\", \"GPU instance\", \"how much am I spending on GPUs\", \"kill my instances\"."
---

# vastai — spin up, watch, and tear down GPU boxes safely

This skill drives **Vast.ai** GPU rentals through one control-plane CLI, `vast.py`.
It tracks every box you rent, shows what's running and what it costs, and **never
leaves a box billing silently** — every box gets an auto-destroy deadline guarded
by a background watchdog.

## The one rule: nothing bills silently

A rented GPU bills by the second until destroyed. So **every box gets a deadline**
(`up --hours N`) and a **watchdog** that destroys it at that deadline. Whenever you
rent something, start (or confirm) a watchdog. When in doubt about stray boxes, run
`vast.py ps` (everything on the account) and `vast.py down`/`nuke` to clean up.

## Tooling

Run the bundled CLI with the system python (stdlib only — no venv needed):

```
python3 ~/.claude/skills/vastai/vast.py <command> [flags]
```

It wraps the official `vastai` CLI. Prerequisites (the skill checks and tells the
user if missing):
- `vastai` CLI installed (`uv tool install vastai`, or `pipx install vastai`).
- Authenticated once: `vastai set api-key <KEY>` (get the key from
  https://console.vast.ai/ → Account). If the user needs to do this, have them run
  it themselves via `! vastai set api-key <KEY>` so the key isn't echoed by you.

State (which boxes are tracked, prices, deadlines) lives in
`~/.vast-claude/state.json`, independent of the current directory. The ssh key
`~/.ssh/id_ed25519` is generated and registered automatically on first `up`.

## Command map

| goal | command |
|------|---------|
| check account credit + burn rate | `vast.py balance` |
| browse offers (read-only) | `vast.py search --gpu RTX_4090 --gpus 1` |
| rent a box (+deadline) | `vast.py up --gpu RTX_4090 --hours 3 --label NAME` |
| guard deadlines (run in background) | `vast.py watchdog` |
| see status / uptime / cost / time-left | `vast.py status` |
| live terminal monitor | `vast.py watch` |
| live HTML dashboard | `vast.py dashboard` |
| run a command on the box | `vast.py run "nvidia-smi"` |
| upload a file/dir | `vast.py put ./train.py /root/` |
| upload a whole dir tree (fast) | `vast.py sync ./project` |
| download a result | `vast.py pull /root/out.txt ./` |
| open an interactive shell | `vast.py ssh --exec` |
| boot/container logs | `vast.py logs` |
| extend the deadline | `vast.py extend --hours 2` |
| destroy one box | `vast.py down` |
| list ALL account instances | `vast.py ps` |
| destroy everything (panic) | `vast.py nuke` |

Every box-targeting command takes `--id <iid>` or `--label <name>`. When exactly
one box is tracked, the target is implied — so single-box sessions need no target
flag. Run `vast.py <command> --help` for a command's flags.

## Workflows

### Spin up a box and run something on it

1. **Check funds & price first.** `vast.py balance`, then `vast.py search --gpu
   <MODEL> --gpus <N>` to see real prices. Confirm the GPU/price with the user
   before renting if they didn't pin specifics — renting costs money.
2. **Rent with a deadline + label.**
   `vast.py up --gpu RTX_4090 --hours 3 --label trainer` (defaults: 1× RTX_4090,
   3 h deadline, the `vastai/pytorch` image, 40 GB disk). `up` waits for boot,
   attaches the ssh key, reports `READY ✓`, then **auto-activates the image's
   baked-in python env** (torch is already installed at `/venv/main` — nothing is
   re-downloaded). Pin an exact offer with `--offer-id <id>` from `search`; use a
   different image with `--image`, or a Vast template with `--template`.
3. **Start the watchdog in the background** so the deadline is enforced even if this
   session ends. Launch it as a background process and keep it running:
   `python3 ~/.claude/skills/vastai/vast.py watchdog` (run_in_background). One
   watchdog guards *all* tracked boxes; it exits on its own once none have deadlines.
4. **Work the box:** `run` for commands, `put`/`sync` to upload, `pull` to fetch
   results, `ssh --exec` for an interactive shell, `logs` to debug a bad boot.
5. **Watch it:** `vast.py status` for a snapshot (uptime, $ spent, time left), or
   `vast.py dashboard` for a live HTML view of every box — good for long jobs.
6. **Tear down** when done: `vast.py down` (or just let the watchdog hit the
   deadline). Verify nothing is left: `vast.py ps`.

### "What do I have running / what am I spending?"
`vast.py status` (tracked boxes, cost so far, time left) and `vast.py balance`
(credit + total burn rate + rough runway). `vast.py ps` reveals untracked orphans.

### "Kill everything" / panic
`vast.py nuke` destroys every instance on the account (asks for confirmation; pass
`--yes` only if the user clearly means *all of them now*).

## Conventions & cautions

- **Renting and destroying spend/lose money and data.** Destroying is irreversible
  and wipes the box's disk — `pull` anything you need first. Confirm before renting
  unless the user already gave concrete specs, and before `nuke` always.
- **Always pair `up` with a running `watchdog`** (or a short `--hours`). If the user
  insists on `--hours 0` (no deadline), warn them it won't auto-destroy.
- Prefer `--label` names so multi-box sessions stay legible (`--label trainer`,
  `--label inference`). Reference boxes by label in later commands.
- **The image's pre-installed venv is auto-activated.** `run`/`ssh` use the image's
  baked-in python (e.g. `vastai/pytorch` ships torch at `/venv/main`) with no manual
  `export PATH` and no re-install, so `vast.py run "python -c 'import torch'"` just
  works. Pass `run --bare` to use the raw system shell instead. This is detected once
  on boot (or on first `run`/`ssh` for `--no-wait` boxes) and recorded in `status`.
- For long-running remote jobs, start them detached (e.g. `run "nohup … &"` or
  inside `tmux`) so the job survives the ssh call returning, then poll with `logs`.
- If `vast.py search` finds nothing, loosen filters: `--unverified`, higher
  `--max-price`, fewer `--gpus`, or a different `--gpu`. List models the user can
  pick from (RTX_4090, RTX_3090, A100_PCIE, H100_SXM, etc.).
- This skill is general-purpose GPU-box plumbing. For the autonomous multi-GPU
  *research swarm* workflow, that's a separate project (`vast-autoresearch`); don't
  conflate them.

Full reference: `python3 ~/.claude/skills/vastai/vast.py --help` and the README.
