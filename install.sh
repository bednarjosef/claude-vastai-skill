#!/usr/bin/env bash
# Install the "vastai" skill for Claude Code:
#   1. check python3 and the official `vastai` CLI
#   2. link this repo into ~/.claude/skills/vastai so the skill is discoverable
#
# Safe to re-run. vast.py itself is stdlib-only, so there is no virtualenv to make.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_LINK="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/vastai"

echo "==> Repo:  $REPO_DIR"
echo "==> Skill: $SKILL_LINK"

# 1. prerequisites ------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "!!  python3 not found. Install Python 3.9+ and re-run."
  exit 1
fi
echo "==> python3: $(python3 -V 2>&1)"

if command -v vastai >/dev/null 2>&1; then
  echo "==> vastai CLI: $(command -v vastai) ($(vastai --version 2>/dev/null || echo '?'))"
else
  echo "==> vastai CLI not found, trying to install it"
  if command -v uv >/dev/null 2>&1; then
    uv tool install vastai
  elif command -v pipx >/dev/null 2>&1; then
    pipx install vastai
  elif command -v pip3 >/dev/null 2>&1; then
    # Most distro Pythons are PEP 668 "externally managed", so plain --user fails there.
    pip3 install --user vastai || pip3 install --user --break-system-packages vastai
  else
    echo "!!  No uv, pipx or pip3 available. Install the CLI yourself, then re-run:"
    echo "!!    uv tool install vastai   # or: pipx install vastai"
  fi
  command -v vastai >/dev/null 2>&1 || \
    echo "!!  vastai is still not on PATH (check ~/.local/bin is in your PATH)."
fi

# 2. link into the Claude skills directory ------------------------------------
mkdir -p "$(dirname "$SKILL_LINK")"
if [ -L "$SKILL_LINK" ]; then
  ln -sfn "$REPO_DIR" "$SKILL_LINK"
  echo "==> Updated symlink $SKILL_LINK -> $REPO_DIR"
elif [ -e "$SKILL_LINK" ]; then
  echo "!!  $SKILL_LINK already exists and is not a symlink."
  echo "!!  Move it aside and re-run, or set CLAUDE_SKILLS_DIR to install elsewhere."
  exit 1
else
  ln -s "$REPO_DIR" "$SKILL_LINK"
  echo "==> Linked $SKILL_LINK -> $REPO_DIR"
fi

[ -f "$SKILL_LINK/SKILL.md" ] || { echo "!!  $SKILL_LINK/SKILL.md is missing, the skill will not load."; exit 1; }

# 3. auth status --------------------------------------------------------------
echo
if command -v vastai >/dev/null 2>&1 && vastai show user --raw >/dev/null 2>&1; then
  echo "Done. API key is already set, so you can go straight to:"
  echo "  python3 $SKILL_LINK/vast.py balance"
else
  echo "Done. One thing left, authenticate once:"
  echo "  vastai set api-key <YOUR_KEY>      # https://console.vast.ai/ -> Account"
  echo "  python3 $SKILL_LINK/vast.py balance"
fi
echo
echo "In Claude Code, start a new session and the skill triggers on /vastai or"
echo "phrasing like \"rent a GPU\" / \"spin up a vast box\"."
