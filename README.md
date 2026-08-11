# vastai

A Claude Code skill for renting GPU boxes on [Vast.ai](https://vast.ai) without accidentally
leaving one running.

It wraps the official `vastai` CLI in one script that tracks every box you rent, shows what
each one has cost so far, and destroys it at a deadline you set when you rent it. That last
part is the reason the thing exists. A 4090 you forget about is roughly $8 a day. A forgotten
8x H100 box is a much worse morning.

## Requirements

* Python 3.9 or newer (the script is stdlib only, nothing to install)
* the official `vastai` CLI, which `install.sh` will set up for you
* `ssh`, `scp` and `tar`, for talking to the boxes
* a Vast.ai account with some credit on it

## Install

```bash
git clone https://github.com/bednarjosef/claude-vastai-skill.git
cd claude-vastai-skill
./install.sh
```

The installer checks for the `vastai` CLI and symlinks the repo into
`~/.claude/skills/vastai`. Because it is a symlink, `git pull` is all you need to update.

Then authenticate once, with your key from [the Vast console](https://console.vast.ai/) under
Account:

```bash
vastai set api-key <YOUR_KEY>
python3 ~/.claude/skills/vastai/vast.py balance   # should print your credit
```

To uninstall, delete the symlink: `rm ~/.claude/skills/vastai`.

## From Claude Code

Start a new session after installing. The skill picks itself up from `/vastai`, or from
normal phrasing like "rent me a 4090 for two hours", "what am I spending on GPUs", "kill my
instances". Claude reads `SKILL.md` and drives the script from there, including confirming
the price with you before it rents anything.

## From a terminal

Same script, no Claude involved:

```bash
PY="python3 ~/.claude/skills/vastai/vast.py"

$PY balance                                   # credit and current burn rate
$PY search --gpu RTX_4090 --gpus 1            # cheapest offers that pass the filters
$PY up --gpu RTX_4090 --hours 3 --label box1  # rent, with a 3 hour deadline
$PY watchdog &                                # enforces the deadline in the background
$PY status                                    # uptime, spend so far, time left
$PY run "nvidia-smi"                          # run a command on the box
$PY sync ./project                            # upload a directory tree
$PY pull /root/out.txt ./                     # fetch a result back
$PY ssh --exec                                # drop into a shell on the box
$PY extend --hours 2                          # push the deadline out
$PY down                                      # destroy it
```

A search on a normal day looks like this:

```
top 3 of 34 qualifying 1x RTX_4090 offers (cheapest first):
  id=41644200  $0.321/hr ($0.321/gpu)  1x RTX 4090  24GB·vram  cpu=32 ram=126GB  rel=0.984  net=382↓  Czechia, CZ
  id=44637531  $0.322/hr ($0.322/gpu)  1x RTX 4090  24GB·vram  cpu=12 ram=63GB   rel=0.994  net=901↓  Texas, US
  id=43681494  $0.335/hr ($0.335/gpu)  1x RTX 4090  24GB·vram  cpu=18 ram=72GB   rel=0.974  net=926↓  Malaysia, MY
```

## Commands

| command | what it does |
|---------|--------------|
| `balance` | account credit, total burn rate, rough runway |
| `search` | cheapest verified offers matching your GPU, count and quality filters |
| `up` | rent the cheapest qualifying offer (or `--offer-id`), record price and deadline, wait for ssh |
| `wait` | block until a box is running and ssh-ready |
| `status` | tracked boxes: status, uptime, cost, time left |
| `watch` | the same thing, refreshing in place |
| `dashboard` | live HTML view of every tracked box, on localhost |
| `run` | run a command on the box |
| `put` / `pull` | upload or download a file or directory |
| `sync` | upload a directory tree over tar and ssh, which is much faster than scp |
| `ssh` | print the ssh command, or `--exec` to enter a session |
| `logs` | the box's boot and container logs (`--daemon` for system logs) |
| `label` | give a box a friendly name |
| `extend` | push a deadline out |
| `down` | destroy a box and stop tracking it |
| `watchdog` | background loop that destroys boxes when they hit their deadline |
| `ps` | every instance on the account, including ones this tool never rented |
| `nuke` | destroy all of them, for when something has gone wrong |

Anything that targets a box takes `--id <iid>` or `--label <name>`. If exactly one box is
tracked, you can leave both off. `vast.py <command> --help` has the flags.

## Deadlines and the watchdog

`up --hours 3` records a hard deadline for the box. The `watchdog` command is a loop that
checks tracked boxes once a minute and destroys any that are past theirs. One watchdog covers
every box, and it exits on its own when there is nothing left with a deadline to guard.

Worth being clear about the limits, since the whole pitch is about not being billed by
surprise:

* The watchdog is an ordinary local process. If you close the terminal, or the laptop sleeps
  through the deadline, nothing gets destroyed. Vast has no server side auto-destroy to fall
  back on, so run it with `nohup`, in `tmux`, or leave the Claude session open.
* `--hours 0` turns the deadline off entirely. You get a warning, and then it is on you.
* A destroy is only treated as done once the account stops listing the instance. If it fails,
  the box stays tracked and the watchdog keeps retrying rather than quietly forgetting it.
* Destroying is irreversible and wipes the disk. `pull` anything you want to keep first.
* `ps` shows everything on the account and flags what this tool is not tracking, which is how
  you catch a box rented from the web console. `nuke` destroys the lot.

## Several boxes at once

State lives in `~/.vast-claude/state.json`, so it does not matter which directory you run
from. Rent as many boxes as you like, label them, and target them by label afterwards:

```bash
$PY up --gpu RTX_4090 --hours 6 --label trainer
$PY up --gpu RTX_3090 --hours 1 --label eval
$PY run --label eval "python bench.py"
$PY down --label eval
```

## Configuration

| env var | default | meaning |
|---------|---------|---------|
| `VAST_CLAUDE_HOME` | `~/.vast-claude` | where tracking state is stored |
| `VAST_SSH_KEY` | `~/.ssh/id_ed25519` | ssh key for the boxes, generated if missing |
| `CLAUDE_SKILLS_DIR` | `~/.claude/skills` | install target for `install.sh` |

`up` defaults to one RTX 4090, 40 GB of disk, a 3 hour deadline and the `vastai/pytorch`
image, all overridable with flags. Offers are filtered to verified hosts with reliability of
at least 0.95 and 100 Mbit/s down, which you can relax with `--unverified`,
`--min-reliability` and `--min-inet`.

## The python env on the box

The default `vastai/pytorch` image is cached on most Vast hosts, so it boots quickly instead
of pulling several GB of torch wheels, and it ships PyTorch in a venv at `/venv/main`. On
first boot the script finds that env and writes a small activation file to the box, which
`run` and `ssh` both source. So this works with no setup and no pip install:

```bash
$PY run "python -c 'import torch; print(torch.__version__, torch.cuda.is_available())'"
# 2.5.1+cu121 True
$PY run --bare "which python"    # skip the activation, raw system shell
```

The same detection handles a conda base like `pytorch/pytorch` and the usual venv locations
(`/root/.venv`, `/workspace/venv` and friends). `status` reports which torch it found.

## Related

This is the plumbing layer: rent a box, use it, get rid of it. The autonomous multi-GPU
research swarm that runs experiments in parallel and keeps a leaderboard is a separate
project, `vast-autoresearch`, built on top of this kind of control plane.

## License

[MIT](LICENSE).
