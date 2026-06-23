#!/usr/bin/env python3
"""vast.py — a safe, general-purpose control plane for Vast.ai GPU instances.

This is the engine behind the `vastai` Claude skill. It wraps the official
`vastai` CLI with three things an agent (or a human) actually needs:

  1. **Spin up** — search verified offers, rent the cheapest that qualifies (or a
     specific offer id), with a working ssh endpoint and an auto-destroy deadline.
  2. **Watch** — `status` / `watch` (live terminal) / `dashboard` (live HTML) show
     every tracked box's status, uptime, cost-so-far and time left at a glance.
  3. **Tear down safely** — every box is rented with a deadline; a background
     `watchdog` destroys boxes the moment they pass it, so nothing is ever left
     silently billing. `ps`/`nuke` catch orphans across the whole account.

Unlike a one-box research harness, this tracks MANY instances at once. Every
command targets a box by `--id <iid>` or `--label <name>`; when exactly one box
is tracked, the target is implied. State lives in ~/.vast-claude/state.json
(override with $VAST_CLAUDE_HOME) — independent of whatever directory you run from.

Auth: run `vastai set api-key <KEY>` once (the official CLI stores it). An ssh
key (~/.ssh/id_ed25519) is generated + registered automatically if missing.

Quick tour:
  vast.py balance                       # check account credit first
  vast.py search --gpu RTX_4090         # cheapest qualifying offers
  vast.py up --gpu RTX_4090 --hours 3 --label trainer   # rent + record deadline
  vast.py watchdog &                    # background auto-destroy at the deadline
  vast.py status                        # status, uptime, $ so far, time left
  vast.py dashboard                     # live HTML view of every box
  vast.py run "nvidia-smi"              # run a command on the box
  vast.py put ./script.py /root/        # upload a file
  vast.py ssh                           # print the ssh command (or --exec to enter)
  vast.py pull /root/out.txt ./         # download a result
  vast.py extend --hours 2              # push the deadline out
  vast.py down                          # destroy + forget (or let the watchdog do it)
  vast.py ps                            # every instance on the account
  vast.py nuke                          # destroy them all (emergency stop)

Run `vast.py <command> --help` for the flags of any command.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from string import Template

# --- paths / state -------------------------------------------------------------

HOME = Path(os.environ.get("VAST_CLAUDE_HOME", Path.home() / ".vast-claude"))
STATE = HOME / "state.json"
SSH_KEY = Path(os.environ.get("VAST_SSH_KEY", Path.home() / ".ssh/id_ed25519"))

# Default remote working dir that `sync`/`put`/`run --dir` use when none is given.
REMOTE_DIR = "/root/work"

# The default launch image: Vast's own PyTorch image. It's pre-cached on most
# hosts (so it doesn't re-download multi-GB torch wheels on every boot) and ships
# torch baked into a venv at /venv/main. On boot we auto-activate whatever baked-in
# env the image carries (see setup_remote_env), so `run`/`ssh` use the existing
# torch with no manual `export PATH=/venv/main/bin:$PATH` and no re-install.
# Vast injects ssh for `--ssh` launches, so a raw `--image` works too.
DEFAULT_IMAGE = "vastai/pytorch"
DEFAULT_GPU = "RTX_4090"

# Written on the box on first boot; every shell (and our `run`) sources it so the
# image's pre-installed python env (torch etc.) is active without any redownload.
REMOTE_ACTIVATE = "/root/.vast_activate"

# Sensible offer-quality floors (all overridable on `search`/`up`). These keep you
# off flaky/throttled hosts without being ML-specific.
MIN_RELIABILITY = 0.95      # host's measured uptime fraction
MIN_INET_DOWN = 100         # Mbit/s down — pulling images/data shouldn't crawl


# --- vastai CLI wrapper --------------------------------------------------------

def _vastai() -> str:
    return shutil.which("vastai") or str(Path.home() / ".local/bin/vastai")


def vast(args: list[str], raw: bool = False, check: bool = True):
    """Invoke the official `vastai` CLI. With raw=True, parse --raw JSON output."""
    if not (shutil.which("vastai") or Path(_vastai()).exists()):
        sys.exit("The `vastai` CLI isn't installed. Install it with:\n"
                 "  uv tool install vastai   # or: pipx install vastai / pip install --user vastai\n"
                 "then authenticate once:  vastai set api-key <YOUR_KEY>")
    cmd = [_vastai(), *args]
    if raw:
        cmd.append("--raw")
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        if "api" in err.lower() and "key" in err.lower():
            err += "\n(hint: run `vastai set api-key <YOUR_KEY>` to authenticate.)"
        sys.exit(f"`vastai {' '.join(args)}` failed:\n{err}")
    if raw:
        try:
            return json.loads(p.stdout or "null")
        except json.JSONDecodeError:
            sys.exit(f"could not parse JSON from `vastai {' '.join(args)}`:\n{p.stdout}")
    return p.stdout


# --- state (multi-instance) ----------------------------------------------------

def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {"instances": {}}


def save_state(d: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, indent=2))


def tracked() -> dict:
    """Map of instance_id(str) -> record for every box we're tracking."""
    return load_state().get("instances", {})


def put_instance(rec: dict) -> None:
    st = load_state()
    st.setdefault("instances", {})[str(rec["instance_id"])] = rec
    save_state(st)


def forget_instance(iid) -> None:
    st = load_state()
    st.get("instances", {}).pop(str(iid), None)
    save_state(st)


def resolve_target(a, *, required: bool = True) -> dict | None:
    """Pick the box a command acts on, by --id, --label, or (if unambiguous) the
    only tracked box. Returns the record, or None when nothing is tracked and the
    command tolerates that (required=False)."""
    insts = tracked()
    wanted_id = getattr(a, "id", None)
    wanted_label = getattr(a, "label", None)
    if wanted_id is not None:
        rec = insts.get(str(wanted_id))
        if rec:
            return rec
        # Allow operating on an account instance we don't yet track (e.g. ssh/run/down).
        return {"instance_id": int(wanted_id), "label": None, "host": None,
                "port": None, "ready": False, "untracked": True}
    if wanted_label:
        hits = [r for r in insts.values() if r.get("label") == wanted_label]
        if not hits:
            sys.exit(f"no tracked box labelled '{wanted_label}'. See `vast.py status`.")
        if len(hits) > 1:
            sys.exit(f"label '{wanted_label}' matches {len(hits)} boxes; use --id instead.")
        return hits[0]
    if len(insts) == 1:
        return next(iter(insts.values()))
    if not insts:
        if required:
            sys.exit("no tracked instances. Start one with `vast.py up`.")
        return None
    ids = ", ".join(f"{r['instance_id']}"
                    + (f"({r['label']})" if r.get("label") else "") for r in insts.values())
    sys.exit(f"{len(insts)} boxes tracked — choose one with --id or --label.\n  {ids}")


# --- ssh -----------------------------------------------------------------------

def ensure_ssh_key() -> None:
    pub = SSH_KEY.with_suffix(".pub")
    if not pub.exists():
        print("generating an ssh key (ed25519)…")
        SSH_KEY.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(SSH_KEY)], check=True)
    # idempotent: registers the pubkey on the account so new boxes accept it
    vast(["create", "ssh-key", pub.read_text().strip()], check=False)


def _ssh_opts() -> list[str]:
    opts = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=15"]
    if SSH_KEY.exists():
        opts += ["-i", str(SSH_KEY)]
    return opts


def ssh_base(s: dict) -> list[str]:
    return ["ssh", *_ssh_opts(), "-p", str(s["port"]), f"root@{s['host']}"]


def ssh_run(s: dict, remote_cmd: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(ssh_base(s) + [remote_cmd], capture_output=True, text=True, check=check)


def scp_to(s: dict, local: str, remote: str, recursive: bool = False) -> None:
    flags = ["-r"] if recursive else []
    subprocess.run(["scp", *_ssh_opts(), *flags, "-P", str(s["port"]), local,
                    f"root@{s['host']}:{remote}"], check=True)


def attach_ssh(s: dict) -> None:
    pub = SSH_KEY.with_suffix(".pub")
    if pub.exists():
        vast(["attach", "ssh", str(s["instance_id"]), pub.read_text().strip()], check=False)


def _ssh_endpoint(iid) -> tuple[str | None, int | None]:
    out = vast(["ssh-url", str(iid)], check=False) or ""
    m = re.match(r"ssh://[^@]+@([^:\s]+):(\d+)", out.strip())
    return (m.group(1), int(m.group(2))) if m else (None, None)


def refresh_endpoint(s: dict) -> dict:
    """Make sure host/port are current (vast can reassign them); persist if tracked."""
    host, port = _ssh_endpoint(s["instance_id"])
    if host:
        s["host"], s["port"] = host, port
        if not s.get("untracked"):
            put_instance(s)
    return s


def wait_ssh(s: dict, tries: int = 40) -> bool:
    r = None
    for _ in range(tries):
        if s.get("host"):
            r = ssh_run(s, "true")
            if r.returncode == 0:
                return True
        else:
            s = refresh_endpoint(s)
        time.sleep(5)
    print("WARN: ssh did not come up:", (r.stderr if r else "").strip()[:200])
    return False


def require_ready(s: dict) -> dict:
    """Resolve + verify the box is reachable over ssh before a run/put/pull/sync."""
    s = refresh_endpoint(s)
    if not s.get("host"):
        sys.exit(f"instance {s['instance_id']} has no ssh endpoint yet — "
                 f"is it running? `vast.py status --id {s['instance_id']}`")
    return s


# --- remote env: auto-activate the image's baked-in venv -----------------------

# One-time, run on the box right after ssh comes up. Finds the python env the image
# already ships (Vast's /venv/main, a conda base, or any common venv), writes an
# activation line to REMOTE_ACTIVATE, and makes interactive shells source it. Our
# `run` sources the same file, so torch & friends are importable everywhere with NO
# re-install and no manual PATH twiddling. If the image has nothing special, the
# activation file is empty and the system python is used — still harmless.
_ENV_PROBE = r"""
set -e
ACT="{ACT}"
pick=""
if [ -x /venv/main/bin/python ]; then
  pick='export PATH=/venv/main/bin:$PATH'           # Vast PyTorch image (vastai/pytorch)
elif [ -f /opt/conda/etc/profile.d/conda.sh ]; then
  pick='. /opt/conda/etc/profile.d/conda.sh; conda activate base 2>/dev/null || true'  # conda images
elif [ -x /opt/conda/bin/python ]; then
  pick='export PATH=/opt/conda/bin:$PATH'
else
  for v in /root/.venv /workspace/venv /venv /root/venv /opt/venv; do
    if [ -x "$v/bin/python" ]; then pick="export PATH=$v/bin:\$PATH"; break; fi
  done
fi
printf '%s\n' "$pick" > "$ACT"
# Make every interactive shell pick it up too (idempotent).
for rc in /root/.bashrc /root/.profile /root/.bash_profile; do
  touch "$rc" 2>/dev/null || continue
  grep -q '.vast_activate' "$rc" 2>/dev/null || \
    printf '\n[ -f %s ] && . %s\n' "$ACT" "$ACT" >> "$rc"
done
# Report what got activated.
. "$ACT" 2>/dev/null || true
echo "REMOTE_PY=$(command -v python 2>/dev/null || command -v python3 2>/dev/null || echo none)"
python -c 'import torch;print("TORCH="+torch.__version__+" cuda="+str(torch.cuda.is_available()))' 2>/dev/null \
  || python3 -c 'import torch;print("TORCH="+torch.__version__+" cuda="+str(torch.cuda.is_available()))' 2>/dev/null \
  || echo "TORCH=none"
"""


def setup_remote_env(s: dict) -> dict:
    """Detect + activate the box's pre-installed python env; record what we found."""
    r = ssh_run(s, _ENV_PROBE.replace("{ACT}", REMOTE_ACTIVATE))
    out = (r.stdout or "") + (r.stderr or "")
    py = re.search(r"REMOTE_PY=(\S+)", out)
    torch = re.search(r"TORCH=(\S+.*)", out)
    s["remote_py"] = py.group(1) if py and py.group(1) != "none" else None
    s["torch"] = None if (not torch or torch.group(1).startswith("none")) else torch.group(1).strip()
    s["env_ready"] = True
    if not s.get("untracked"):
        put_instance(s)
    return s


def ensure_env(s: dict) -> dict:
    """Make sure the baked-in venv has been detected/activated on the box (probes
    once; cheap no-op afterwards). Covers boxes started with --no-wait or targeted
    by --id that never went through the boot-time setup."""
    if s.get("env_ready"):
        return s
    return setup_remote_env(s)


def _activate_prefix() -> str:
    """Prefix that activates the baked-in venv for a non-interactive `ssh host cmd`."""
    return f"[ -f {REMOTE_ACTIVATE} ] && . {REMOTE_ACTIVATE}; "


# --- offers --------------------------------------------------------------------

def find_offers(a) -> list[dict]:
    """Search verified, rentable offers matching the GPU/count and quality floors,
    cheapest first. A raw --query is appended verbatim for power users."""
    q = [f"gpu_name={a.gpu}", f"num_gpus={a.gpus}", "rentable=true"]
    if not getattr(a, "unverified", False):
        q.append("verified=true")
    if getattr(a, "query", None):
        q.append(a.query)
    offers = vast(["search", "offers", " ".join(q), "-o", "dph_total"], raw=True) or []
    cap = getattr(a, "max_price", None)
    out = []
    for o in offers:
        country = str(o.get("geolocation", "")).split(",")[-1].strip()
        per_gpu = o.get("dph_total", 1e9) / max(1, o.get("num_gpus", 1))
        if (o.get("reliability2", 0) >= a.min_reliability
                and o.get("inet_down", 0) >= a.min_inet
                and (cap is None or per_gpu <= cap)
                and country not in set(getattr(a, "block", []) or [])):
            out.append(o)
    out.sort(key=lambda o: o["dph_total"])
    return out


def fmt_offer(o: dict) -> str:
    n = o.get("num_gpus", 1)
    cpu = o.get("cpu_cores_effective") or o.get("cpu_cores", 0)
    ram = o.get("cpu_ram", 0) / 1024
    return (f"id={o['id']:<9} ${o['dph_total']:.3f}/hr (${o['dph_total']/n:.3f}/gpu)  "
            f"{n}x {o['gpu_name']}  {o.get('gpu_total_ram',0)/1024:.0f}GB·vram  "
            f"cpu={cpu:.0f} ram={ram:.0f}GB  rel={o.get('reliability2',0):.3f}  "
            f"net={o.get('inet_down',0):.0f}↓  {o.get('geolocation','?')}")


# --- account / status helpers --------------------------------------------------

def _account_instances() -> dict:
    """instance_id(str) -> raw record for everything on the account, by one call."""
    rows = vast(["show", "instances"], raw=True, check=False) or []
    return {str(r.get("id")): r for r in rows if r.get("id") is not None}


def _cost_so_far(rec: dict) -> float:
    return max(0.0, (time.time() - rec.get("created_at", time.time())) / 3600 * rec.get("dph", 0))


def _fmt_dt(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


# --- commands: spin up ---------------------------------------------------------

def cmd_search(a) -> None:
    offers = find_offers(a)
    if not offers:
        print(f"no rentable {a.gpus}x {a.gpu} offers meet the filters "
              f"(try --unverified, a higher --max-price, fewer --gpus, or a different --gpu).")
        return
    print(f"top {min(a.limit, len(offers))} of {len(offers)} qualifying {a.gpus}x {a.gpu} offers "
          f"(cheapest first):")
    for o in offers[:a.limit]:
        print("  " + fmt_offer(o))
    print("\nrent one with:  vast.py up --gpu "
          f"{a.gpu} --gpus {a.gpus}   (or --offer-id <id> to pin an exact offer)")


def cmd_up(a) -> None:
    ensure_ssh_key()
    if a.offer_id:
        offers = find_offers(a)
        best = next((o for o in offers if o["id"] == a.offer_id), None)
        if not best:  # pinned offer didn't pass filters / vanished — fetch it directly
            direct = vast(["search", "offers", f"id={a.offer_id}"], raw=True) or []
            best = direct[0] if direct else None
        if not best:
            sys.exit(f"offer {a.offer_id} not found — run `vast.py search` for current offers.")
    else:
        offers = find_offers(a)
        if not offers:
            sys.exit(f"no {a.gpus}x {a.gpu} offers meet the filters "
                     f"(try --unverified, a higher --max-price, fewer --gpus, or a different --gpu).")
        best = offers[0]
    print("renting: " + fmt_offer(best))

    create = ["create", "instance", str(best["id"]), "--disk", str(a.disk)]
    if a.template:
        create += ["--template_hash", a.template]   # template carries its own ssh runtype
    else:
        create += ["--image", a.image, "--ssh", "--direct"]  # raw image: have Vast inject ssh
    if a.label:
        create += ["--label", a.label]
    if a.onstart:
        create += ["--onstart-cmd", a.onstart]
    if a.env:
        create += ["--env", a.env]
    res = vast(create, raw=True)
    iid = res.get("new_contract") if isinstance(res, dict) else None
    if not iid:
        sys.exit(f"create failed: {res}")

    now = time.time()
    rec = {
        "instance_id": iid, "label": a.label, "offer_id": best["id"], "dph": best["dph_total"],
        "gpu": best["gpu_name"], "num_gpus": best.get("num_gpus", a.gpus),
        "image": None if a.template else a.image, "template": a.template, "disk": a.disk,
        "created_at": now, "deadline": (now + a.hours * 3600) if a.hours else None,
        "host": None, "port": None, "ready": False,
    }
    put_instance(rec)
    dl = f"auto-destroy in {a.hours}h" if a.hours else "NO deadline (no auto-destroy!)"
    print(f"created instance {iid} ({best.get('num_gpus', a.gpus)}x {best['gpu_name']}) "
          f"at ${best['dph_total']:.3f}/hr — {dl}.")
    if a.hours:
        print("IMPORTANT: keep a watchdog running so it can't be left billing:\n"
              "  python vast.py watchdog &")
    else:
        print("WARNING: no --hours means no auto-destroy. Run `vast.py down` when finished.")
    if a.no_wait:
        print("not waiting (--no-wait). Check readiness with `vast.py status`.")
        return
    _wait_running(rec)


def _wait_running(rec: dict) -> None:
    print("waiting for the box to boot…")
    for _ in range(60):
        info = vast(["show", "instance", str(rec["instance_id"])], raw=True, check=False)
        status = info.get("actual_status") if isinstance(info, dict) else None
        print(f"  status={status}")
        if status == "running":
            rec = refresh_endpoint(rec)
            if rec.get("host"):
                attach_ssh(rec)
                if wait_ssh(rec):
                    rec["ready"] = True
                    put_instance(rec)
                    print(f"READY ✓  ssh -p {rec['port']} root@{rec['host']}")
                    print("activating the image's baked-in python env…")
                    rec = setup_remote_env(rec)
                    if rec.get("torch"):
                        print(f"  torch {rec['torch']} ready (no re-install) — "
                              f"`run python …` uses it automatically.")
                    elif rec.get("remote_py"):
                        print(f"  python: {rec['remote_py']} (no torch detected in the image).")
                    print("next: `vast.py run \"nvidia-smi\"`  ·  `vast.py put <file> /root/`  ·  "
                          "`vast.py status`")
                    return
        time.sleep(10)
    print("still not running after ~10 min — check `vast.py status` or the Vast console.")


def cmd_wait(a) -> None:
    s = resolve_target(a)
    _wait_running(s)


# --- commands: watch -----------------------------------------------------------

def _status_line(rec: dict, live: dict | None) -> list[str]:
    iid = str(rec["instance_id"])
    info = live.get(iid, {}) if live else {}
    status = info.get("actual_status", "?")
    up = time.time() - rec.get("created_at", time.time())
    lines = [
        f"● {iid}" + (f" [{rec['label']}]" if rec.get("label") else "")
        + f"  {rec.get('num_gpus','?')}x {rec.get('gpu','?')}  ${rec.get('dph',0):.3f}/hr"
        + f"  status={status}",
        f"    up {_fmt_dt(up)}   spent ${_cost_so_far(rec):.2f}",
    ]
    if rec.get("deadline"):
        left = rec["deadline"] - time.time()
        lines[-1] += (f"   deadline in {_fmt_dt(left)}" if left > 0 else "   DEADLINE PASSED ⚠")
    else:
        lines[-1] += "   no deadline ⚠"
    if rec.get("host"):
        lines.append(f"    ssh -p {rec['port']} root@{rec['host']}")
    if rec.get("torch"):
        lines.append(f"    env: torch {rec['torch']} (auto-activated)")
    return lines


def cmd_status(a) -> None:
    insts = tracked()
    if getattr(a, "id", None) or getattr(a, "label", None):
        insts = {str(resolve_target(a)["instance_id"]): resolve_target(a)}
    if not insts:
        print("no tracked instances. Start one with `vast.py up`, "
              "or `vast.py ps` to see the whole account.")
        return
    live = _account_instances()
    total_rate = sum(r.get("dph", 0) for r in insts.values())
    total_spent = sum(_cost_so_far(r) for r in insts.values())
    print(f"tracking {len(insts)} instance(s) — ${total_rate:.3f}/hr total, "
          f"${total_spent:.2f} spent so far")
    for rec in insts.values():
        print("\n".join(_status_line(rec, live)))
    # Flag tracked boxes that have vanished from the account (already destroyed elsewhere).
    gone = [iid for iid in insts if iid not in live]
    if gone:
        print(f"\nnote: {', '.join(gone)} no longer on the account — "
              f"`vast.py down --id <id>` to forget, or ignore.")


def cmd_watch(a) -> None:
    print("live status — Ctrl-C to stop.")
    try:
        while True:
            sys.stdout.write("\x1b[2J\x1b[H")  # clear screen
            print(time.strftime("%H:%M:%S"), "· vast.py watch (refresh "
                  f"{a.interval}s)\n")
            cmd_status(a)
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print("\nstopped.")


# --- commands: remote ops ------------------------------------------------------

def cmd_ssh(a) -> None:
    s = ensure_env(require_ready(resolve_target(a)))  # install the .bashrc venv hook
    cmd = ssh_base(s)
    if a.exec:
        os.execvp(cmd[0], cmd)  # hand the terminal over to an interactive ssh
    print(" ".join(cmd))


def cmd_run(a) -> None:
    s = ensure_env(require_ready(resolve_target(a)))
    workdir = a.dir or REMOTE_DIR
    # Source the baked-in venv first, so `python`/`pip` are the image's pre-installed
    # ones (torch already present) with no manual activation. --bare skips it.
    prefix = "" if getattr(a, "bare", False) else _activate_prefix()
    remote = f"{prefix}mkdir -p {workdir} && cd {workdir} && {a.command}"
    sys.exit(subprocess.run(ssh_base(s) + [remote]).returncode)


def cmd_put(a) -> None:
    s = require_ready(resolve_target(a))
    local = Path(a.local)
    if not local.exists():
        sys.exit(f"local path not found: {local}")
    remote = a.remote or (REMOTE_DIR + "/")
    if remote.endswith("/"):
        ssh_run(s, f"mkdir -p {remote}")
    print(f"uploading {local} -> {remote}")
    scp_to(s, str(local), remote, recursive=local.is_dir())
    print("done.")


def cmd_pull(a) -> None:
    s = require_ready(resolve_target(a))
    src = f"root@{s['host']}:{a.remote}"
    print(f"downloading {a.remote} -> {a.local}")
    subprocess.run(["scp", *_ssh_opts(), "-r", "-P", str(s["port"]), src, a.local], check=True)
    print("done.")


def cmd_sync(a) -> None:
    """Upload a local directory tree to the box via tar-over-ssh (fast, no per-file
    round-trips). Excludes the usual heavy/secret dirs."""
    s = require_ready(resolve_target(a))
    local = Path(a.local).resolve()
    if not local.is_dir():
        sys.exit(f"--local must be a directory: {local}")
    dest = a.remote or REMOTE_DIR
    excludes = " ".join(f"--exclude=./{x}" for x in
                        (".git", ".venv", "venv", "node_modules", "__pycache__",
                         ".vast-claude", *(a.exclude or [])))
    print(f"syncing {local} -> {dest} (tar over ssh)…")
    remote = " ".join(ssh_base(s)) + f" 'mkdir -p {dest} && tar xzf - -C {dest}'"
    subprocess.run(f"tar czf - {excludes} -C {local} . | {remote}", shell=True, check=True)
    print("sync complete.")


def cmd_logs(a) -> None:
    s = resolve_target(a)
    args = ["logs", str(s["instance_id"]), "--tail", str(a.tail)]
    if a.daemon:
        args.append("--daemon-logs")
    sys.stdout.write(vast(args, check=False) or "(no logs yet — the box may still be booting)\n")


def cmd_label(a) -> None:
    s = resolve_target(a)
    vast(["label", "instance", str(s["instance_id"]), a.new_label])
    if not s.get("untracked"):
        s["label"] = a.new_label
        put_instance(s)
    print(f"instance {s['instance_id']} labelled '{a.new_label}'.")


# --- commands: lifecycle / safety ----------------------------------------------

def cmd_extend(a) -> None:
    s = resolve_target(a)
    if s.get("untracked"):
        sys.exit("can only extend a tracked instance's deadline.")
    s["deadline"] = time.time() + a.hours * 3600
    put_instance(s)
    print(f"instance {s['instance_id']} deadline set to {a.hours}h from now.")


def _destroy(iid) -> None:
    subprocess.run([_vastai(), "destroy", "instance", str(iid)], input="y\n", text=True,
                   capture_output=True)


def cmd_down(a) -> None:
    s = resolve_target(a)
    _destroy(s["instance_id"])
    forget_instance(s["instance_id"])
    print(f"destroyed instance {s['instance_id']} and removed it from tracking.")


def cmd_watchdog(a) -> None:
    """Background guardian: every minute, destroy any tracked box past its deadline.
    Exits once no tracked box has a future deadline left to guard. Reads state fresh
    each loop, so boxes added by later `up` calls are picked up automatically."""
    print("watchdog: auto-destroying boxes at their deadlines (Ctrl-C to stop).")
    while True:
        insts = tracked()
        if not insts:
            print("watchdog: nothing tracked; exiting.")
            return
        now = time.time()
        guarding = False
        for iid, rec in list(insts.items()):
            dl = rec.get("deadline")
            if dl is None:
                continue
            if now >= dl:
                print(f"watchdog: instance {iid} hit its deadline -> destroying.")
                _destroy(iid)
                forget_instance(iid)
            else:
                guarding = True
        if not guarding:
            print("watchdog: no remaining deadlines to guard; exiting.")
            return
        time.sleep(60)


def cmd_ps(a) -> None:
    """Every instance on the account (the orphan catcher) — marks which we track."""
    rows = vast(["show", "instances"], raw=True, check=False) or []
    if not rows:
        print("no instances on the account. (nothing is being billed)")
        return
    mine = tracked()
    total = sum(r.get("dph_total", 0) for r in rows)
    print(f"{len(rows)} instance(s) on the account — ${total:.3f}/hr total:")
    for r in rows:
        iid = str(r.get("id"))
        flag = "tracked" if iid in mine else "UNTRACKED"
        print(f"  id={iid:<9} {r.get('num_gpus')}x {r.get('gpu_name')}  "
              f"{r.get('actual_status')}  ${r.get('dph_total',0):.3f}/hr  [{flag}]")
    orphans = [str(r.get("id")) for r in rows if str(r.get("id")) not in mine]
    if orphans:
        print(f"\n{len(orphans)} untracked — `vast.py nuke` to destroy ALL, "
              f"or `vast.py down --id <id>` one at a time.")


def cmd_nuke(a) -> None:
    rows = vast(["show", "instances"], raw=True, check=False) or []
    if not rows:
        print("nothing to nuke.")
        return
    if not a.yes:
        print("about to DESTROY every instance on the account:")
        for r in rows:
            print(f"  id={r.get('id')}  {r.get('num_gpus')}x {r.get('gpu_name')}  "
                  f"${r.get('dph_total',0):.3f}/hr")
        if input("type 'nuke' to confirm: ").strip() != "nuke":
            print("aborted.")
            return
    for r in rows:
        print(f"destroying {r.get('id')} ({r.get('gpu_name')})")
        _destroy(r.get("id"))
    save_state({"instances": {}})
    print("all instances destroyed; tracking cleared.")


def cmd_balance(a) -> None:
    u = vast(["show", "user"], raw=True, check=False)
    if not isinstance(u, dict):
        print("could not read account info (is the api key set? `vastai set api-key <KEY>`).")
        return
    credit = u.get("credit", u.get("balance"))
    rate = sum(r.get("dph_total", 0) for r in (vast(["show", "instances"], raw=True, check=False) or []))
    if credit is not None:
        print(f"account credit: ${credit:.2f}")
    if rate:
        runway = f" (~{credit / rate:.1f}h runway)" if credit else ""
        print(f"current burn rate: ${rate:.3f}/hr across running instances{runway}")
    else:
        print("no instances currently billing.")
    if u.get("email"):
        print(f"account: {u.get('email')}")


# --- dashboard (live HTML) -----------------------------------------------------

PAGE = Template("""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=5><title>vast.ai · live</title><style>
*{box-sizing:border-box}body{margin:0;background:#0b0e13;color:#e5e7eb;
font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;padding:22px}
h1{margin:0 0 2px;font-size:20px}.sub{color:#34d399;font-size:12px}
.tot{color:#9ca3af;margin:8px 0 18px;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:14px}
.card{background:#111722;border:1px solid #1f2937;border-radius:10px;padding:16px}
.card.warn{border-color:#f59e0b}.card.dead{border-color:#ef4444}
.row{display:flex;justify-content:space-between;align-items:baseline}
.id{font-size:15px;color:#fff;font-weight:600}.lbl{color:#34d399;font-size:12px;margin-left:6px}
.st{font-size:11px;border:1px solid #1f2937;border-radius:999px;padding:2px 10px}
.running{color:#34d399;border-color:#155e4b}.other{color:#fbbf24;border-color:#7c5e12}
.chips{margin:10px 0 6px}.chips span{display:inline-block;background:#0f1419;
border:1px solid #1f2937;border-radius:999px;padding:3px 10px;margin:0 6px 6px 0;font-size:12px}
.bar{height:7px;background:#0f1419;border-radius:999px;overflow:hidden;margin:10px 0 4px}
.fill{height:100%;background:linear-gradient(90deg,#34d399,#10b981)}
.fill.warn{background:linear-gradient(90deg,#f59e0b,#ef4444)}
.muted{color:#6b7280;font-size:12px}code{background:#0f1419;padding:1px 6px;border-radius:4px;font-size:12px}
.empty{color:#6b7280;border:1px dashed #1f2937;border-radius:10px;padding:30px;text-align:center}
</style></head><body>
<h1>vast.ai <span class=sub>● live · refreshes every 5s</span></h1>
<div class=tot>$total</div>$cards
</body></html>""")


def _dash_card(rec: dict, live: dict) -> str:
    iid = str(rec["instance_id"])
    info = live.get(iid, {})
    status = info.get("actual_status", "gone")
    up = time.time() - rec.get("created_at", time.time())
    cls, fillcls = "card", "fill"
    deadline_html = '<span>no deadline ⚠</span>'
    if rec.get("deadline"):
        total = rec["deadline"] - rec.get("created_at", rec["deadline"])
        left = rec["deadline"] - time.time()
        frac = max(0.0, min(1.0, up / total)) if total else 1.0
        if left <= 0:
            cls, fillcls = "card dead", "fill warn"
            deadline_html = '<span>DEADLINE PASSED ⚠</span>'
        else:
            if left < 1800:
                cls, fillcls = "card warn", "fill warn"
            deadline_html = f'<span>{_fmt_dt(left)} left</span>'
        bar = f'<div class=bar><div class="{fillcls}" style="width:{frac*100:.0f}%"></div></div>'
    else:
        cls = "card warn"
        bar = ""
    stcls = "running" if status == "running" else "other"
    ssh = (f'<div class=muted style="margin-top:8px">'
           f'<code>ssh -p {rec["port"]} root@{rec["host"]}</code></div>'
           if rec.get("host") else '')
    chips = "".join(f"<span>{c}</span>" for c in [
        f'{rec.get("num_gpus","?")}× {html.escape(str(rec.get("gpu","?")))}',
        f'${rec.get("dph",0):.3f}/hr',
        f'up {_fmt_dt(up)}',
        f'spent ${_cost_so_far(rec):.2f}',
        deadline_html.replace("<span>", "").replace("</span>", ""),
    ])
    lbl = f'<span class=lbl>{html.escape(rec["label"])}</span>' if rec.get("label") else ""
    return (f'<div class="{cls}"><div class=row><div><span class=id>{iid}</span>{lbl}</div>'
            f'<span class="st {stcls}">{html.escape(status)}</span></div>'
            f'<div class=chips>{chips}</div>{bar}{ssh}</div>')


def _render_dashboard() -> str:
    insts = tracked()
    if not insts:
        return PAGE.safe_substitute(
            total="no tracked instances",
            cards='<div class=empty>No tracked instances.<br>Start one with '
                  '<code>python vast.py up</code></div>')
    live = _account_instances()
    rate = sum(r.get("dph", 0) for r in insts.values())
    spent = sum(_cost_so_far(r) for r in insts.values())
    total = (f"{len(insts)} instance(s) · ${rate:.3f}/hr · ${spent:.2f} spent so far")
    cards = '<div class=grid>' + "".join(
        _dash_card(r, live) for r in sorted(insts.values(),
                                            key=lambda r: r.get("created_at", 0))) + '</div>'
    return PAGE.safe_substitute(total=total, cards=cards)


def cmd_dashboard(a) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                body = _render_dashboard().encode()
            except Exception as e:  # a transient read must never crash the server
                body = f"<pre>dashboard error: {html.escape(str(e))}</pre>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # quiet
            pass

    url = f"http://127.0.0.1:{a.port}"
    srv = HTTPServer(("127.0.0.1", a.port), Handler)
    print(f"dashboard live at {url}  (Ctrl-C to stop; reads tracking state live)")
    if not a.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard stopped.")


# --- argument parsing ----------------------------------------------------------

def _add_target(p) -> None:
    """Flags every box-targeting command shares."""
    p.add_argument("--id", type=int, default=None, help="target this instance id")
    p.add_argument("--label", default=None, help="target the tracked box with this label")


def _add_search(p, *, full: bool) -> None:
    p.add_argument("--gpu", default=DEFAULT_GPU, help=f"GPU model (default {DEFAULT_GPU})")
    p.add_argument("--gpus", type=int, default=1, help="GPUs per machine (default 1)")
    p.add_argument("--max-price", type=float, default=None, help="cap in $/GPU/hr (default: none)")
    p.add_argument("--min-reliability", dest="min_reliability", type=float,
                   default=MIN_RELIABILITY, help=f"min host reliability (default {MIN_RELIABILITY})")
    p.add_argument("--min-inet", type=float, default=MIN_INET_DOWN,
                   help=f"min download Mbit/s (default {MIN_INET_DOWN})")
    p.add_argument("--unverified", action="store_true", help="allow unverified hosts")
    p.add_argument("--block", action="append", default=[],
                   help="exclude a country code (repeatable), e.g. --block CN")
    p.add_argument("--query", default=None,
                   help="extra raw vastai offer query appended verbatim, e.g. \"cuda_max_good>=12.4\"")
    if not full:
        p.add_argument("--limit", type=int, default=8, help="how many offers to list")


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="vast.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help=""):
        p = sub.add_parser(name, help=help)
        p.set_defaults(fn=fn)
        return p

    # spin up
    sp = add("search", cmd_search, "list cheapest qualifying offers")
    _add_search(sp, full=False)

    up = add("up", cmd_up, "rent a box + record a deadline")
    _add_search(up, full=True)
    up.add_argument("--offer-id", type=int, default=None, help="rent this exact offer id")
    up.add_argument("--hours", type=float, default=3.0,
                    help="auto-destroy deadline in hours (0 = none; default 3)")
    up.add_argument("--disk", type=int, default=40, help="disk GB (default 40)")
    up.add_argument("--image", default=DEFAULT_IMAGE, help="docker image (default %(default)s)")
    up.add_argument("--template", default=None, help="vast template_hash (overrides --image)")
    up.add_argument("--label", default=None, help="friendly name for this box")
    up.add_argument("--onstart", default=None, help="onstart shell command to run on boot")
    up.add_argument("--env", default=None, help="env/port string, e.g. '-p 8080:8080 -e FOO=bar'")
    up.add_argument("--no-wait", action="store_true", help="don't block waiting for boot")

    wt = add("wait", cmd_wait, "block until a box is running + ssh-ready")
    _add_target(wt)

    # watch
    st = add("status", cmd_status, "status/uptime/cost/deadline of tracked boxes")
    _add_target(st)
    wa = add("watch", cmd_watch, "live-refreshing status in the terminal")
    _add_target(wa)
    wa.add_argument("--interval", type=int, default=10, help="refresh seconds (default 10)")
    da = add("dashboard", cmd_dashboard, "live HTML dashboard of every tracked box")
    da.add_argument("--port", type=int, default=8724)
    da.add_argument("--no-open", action="store_true", help="don't auto-open a browser")

    # remote ops
    sh = add("ssh", cmd_ssh, "print the ssh command (or --exec to enter)")
    _add_target(sh)
    sh.add_argument("--exec", action="store_true", help="exec an interactive ssh session")
    rn = add("run", cmd_run, "run a command on the box")
    _add_target(rn)
    rn.add_argument("command", help="remote command (quote it)")
    rn.add_argument("--dir", default=None, help=f"remote working dir (default {REMOTE_DIR})")
    rn.add_argument("--bare", action="store_true",
                    help="don't auto-activate the image's venv (use the raw system shell)")
    pu = add("put", cmd_put, "upload a local file/dir to the box")
    _add_target(pu)
    pu.add_argument("local", help="local file or directory")
    pu.add_argument("remote", nargs="?", default=None, help=f"remote dest (default {REMOTE_DIR}/)")
    pl = add("pull", cmd_pull, "download a remote file/dir from the box")
    _add_target(pl)
    pl.add_argument("remote", help="remote path on the box")
    pl.add_argument("local", nargs="?", default=".", help="local dest (default .)")
    sy = add("sync", cmd_sync, "upload a local directory tree (tar over ssh)")
    _add_target(sy)
    sy.add_argument("local", help="local directory to upload")
    sy.add_argument("remote", nargs="?", default=None, help=f"remote dir (default {REMOTE_DIR})")
    sy.add_argument("--exclude", action="append", default=[], help="extra path to exclude (repeatable)")
    lg = add("logs", cmd_logs, "show the box's boot/container logs")
    _add_target(lg)
    lg.add_argument("--tail", type=int, default=200, help="lines from the end (default 200)")
    lg.add_argument("--daemon", action="store_true", help="daemon/system logs instead of container")
    lb = add("label", cmd_label, "set a friendly label on a box")
    _add_target(lb)
    lb.add_argument("new_label", help="the label to set")

    # lifecycle / safety
    ex = add("extend", cmd_extend, "push a box's auto-destroy deadline out")
    _add_target(ex)
    ex.add_argument("--hours", type=float, default=2.0, help="new deadline, hours from now")
    dn = add("down", cmd_down, "destroy a box + forget it")
    _add_target(dn)
    add("watchdog", cmd_watchdog, "background loop: destroy boxes at their deadlines")
    add("ps", cmd_ps, "list ALL account instances (catch orphans)")
    nk = add("nuke", cmd_nuke, "destroy ALL account instances (emergency stop)")
    nk.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    add("balance", cmd_balance, "show account credit + current burn rate")

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
