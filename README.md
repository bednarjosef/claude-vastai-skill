# vastai — a Claude Code skill for renting GPU boxes safely

Spin up [Vast.ai](https://vast.ai) GPU instances, watch what they cost in real
time, run jobs on them, and tear them down — driven from Claude Code (or your
terminal) through a single, safety-first control-plane CLI.

The defining feature: **nothing bills silently.** Every box is rented with an
auto-destroy deadline and guarded by a background watchdog, so a forgotten GPU
can't quietly drain your account.

```
balance   →   search   →   up (+deadline)   →   watch / run / put / pull   →   down
                                  └── watchdog guards the deadline in the background ──┘
```

## Install

```bash
git clone <this-repo> claude-vastai-skill
cd claude-vastai-skill
./install.sh
```

`install.sh` ensures the official `vastai` CLI is present and symlinks this repo
into `~/.claude/skills/vastai`. Then authenticate once:

```bash
vastai set api-key <YOUR_KEY>      # from https://console.vast.ai/ → Account
```

`vast.py` uses only the Python standard library — no virtualenv, no dependencies
beyond the `vastai` CLI it wraps.

## Use it from Claude Code

Once installed, the skill triggers on `/vastai` or natural phrasing like
*"rent a GPU"*, *"spin up a vast box"*, *"what am I spending on GPUs?"*, or
*"kill my instances"*. Claude reads `SKILL.md` and drives `vast.py` for you.

## Use it from the terminal

```bash
PY="python3 ~/.claude/skills/vastai/vast.py"

$PY balance                                   # credit + current burn rate
$PY search --gpu RTX_4090 --gpus 1            # cheapest qualifying offers
$PY up --gpu RTX_4090 --hours 3 --label box1  # rent + record a 3h deadline
$PY watchdog &                                # background: auto-destroy at deadline
$PY status                                    # uptime, $ spent, time left
$PY dashboard                                 # live HTML view of every box
$PY run "nvidia-smi"                          # run a command on the box
$PY put ./train.py /root/                     # upload a file
$PY sync ./project                            # upload a whole directory tree
$PY pull /root/out.txt ./                     # download a result
$PY ssh --exec                                # interactive shell on the box
$PY extend --hours 2                          # push the deadline out
$PY down                                      # destroy + forget this box
$PY ps                                        # EVERY instance on the account
$PY nuke                                      # destroy them all (panic button)
```

## Commands

| command | what it does |
|---------|--------------|
| `balance` | account credit, total burn rate, rough runway |
| `search` | list cheapest verified offers matching GPU/count/quality filters |
| `up` | rent the cheapest qualifying offer (or `--offer-id`), record price + deadline, wait for ssh |
| `wait` | block until a box is running and ssh-ready |
| `status` | snapshot of tracked boxes: status, uptime, cost, time left |
| `watch` | the same, live-refreshing in the terminal |
| `dashboard` | live HTML dashboard of every tracked box (auto-refresh) |
| `run` | run a command on the box |
| `put` / `pull` | upload / download a file or directory |
| `sync` | upload a local directory tree via tar-over-ssh (fast) |
| `ssh` | print the ssh command, or `--exec` to enter an interactive session |
| `logs` | the box's boot/container logs (`--daemon` for system logs) |
| `label` | set a friendly label on a box |
| `extend` | push a box's auto-destroy deadline out |
| `down` | destroy a box and forget it |
| `watchdog` | background loop that destroys boxes at their deadlines |
| `ps` | list **all** instances on the account (catch untracked orphans) |
| `nuke` | destroy every instance on the account (emergency stop) |

Every box-targeting command accepts `--id <iid>` or `--label <name>`. With exactly
one tracked box, the target is implied. See `vast.py <command> --help` for flags.

## Multiple boxes

State for all tracked boxes lives in `~/.vast-claude/state.json` (override with
`$VAST_CLAUDE_HOME`). You can rent several boxes, label them, and target each by
label; one `watchdog` guards all of their deadlines at once.

## Safety model

- **Deadlines.** `up --hours N` records a hard deadline. `--hours 0` disables it
  (you'll be warned — then *you* are responsible for `down`).
- **Watchdog.** A background `watchdog` process destroys any tracked box the moment
  it passes its deadline, then exits once nothing is left to guard.
- **Orphan control.** `ps` lists everything on the account and flags what isn't
  tracked; `nuke` is the all-stop. Tracking and the real account are reconciled on
  every `status`/`dashboard`.
- **Destroy = irreversible.** It deletes the box's disk. `pull` results first.

## Configuration

| env var | default | meaning |
|---------|---------|---------|
| `VAST_CLAUDE_HOME` | `~/.vast-claude` | where tracking state is stored |
| `VAST_SSH_KEY` | `~/.ssh/id_ed25519` | ssh key used for boxes (auto-generated) |
| `CLAUDE_SKILLS_DIR` | `~/.claude/skills` | install target for `install.sh` |

Defaults that `up` uses (all overridable via flags): GPU `RTX_4090`, 1 GPU, 40 GB
disk, 3 h deadline, image `vastai/pytorch`, ssh + direct connection. Offer quality
floors: reliability ≥ 0.95, download ≥ 100 Mbit/s, verified hosts only.

### Pre-installed venv, auto-activated

The default `vastai/pytorch` image is **pre-cached on most Vast hosts** (so torch
isn't re-downloaded on every boot) and ships PyTorch baked into a venv at
`/venv/main`. On boot, `vast.py` detects that env and writes a small activation
hook to the box, so **`run` and `ssh` use the pre-installed python automatically** —
no manual `export PATH=/venv/main/bin:$PATH`, no `pip install torch`:

```bash
$PY run "python -c 'import torch; print(torch.__version__, torch.cuda.is_available())'"
# 2.5.1+cu121 True   ← uses the image's baked-in venv, nothing re-installed
$PY run --bare "which python"   # bypass activation; raw system shell
```

It also works for other images: a conda base (`pytorch/pytorch`) or any common venv
(`/root/.venv`, `/workspace/venv`, …) is detected the same way. `status` shows the
activated torch version.

## Not the same as `vast-autoresearch`

This is general GPU-box plumbing. The autonomous multi-GPU *research swarm*
(parallel experiments, rounds, leaderboards) is a separate project. This skill is
the reusable control-plane layer underneath that kind of workflow.
