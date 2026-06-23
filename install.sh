#!/usr/bin/env bash
# Install the "vastai" skill for Claude Code:
#   1. ensure the official `vastai` CLI is available
#   2. link this repo into ~/.claude/skills/vastai so the skill is discoverable
#
# Safe to re-run. `vast.py` itself is stdlib-only — no virtualenv required.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_LINK="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/vastai"

echo "==> Repo:  $REPO_DIR"
echo "==> Skill: $SKILL_LINK"

# 1. ensure the vastai CLI ----------------------------------------------------
if command -v vastai >/dev/null 2>&1; then
  echo "==> vastai CLI: $(command -v vastai) ($(vastai --version 2>/dev/null || echo '?'))"
else
  echo "==> vastai CLI not found — attempting to install…"
  if command -v uv >/dev/null 2>&1; then
    uv tool install vastai
  elif command -v pipx >/dev/null 2>&1; then
    pipx install vastai
  elif command -v pip >/dev/null 2>&1; then
    pip install --user vastai
  else
    echo "!!  Could not auto-install vastai (no uv/pipx/pip)."
    echo "!!  Install it manually, then re-run this script:"
    echo "!!    uv tool install vastai   # or pipx install vastai"
  fi
fi

# 2. link into the Claude skills directory ------------------------------------
mkdir -p "$(dirname "$SKILL_LINK")"
if [ -L "$SKILL_LINK" ]; then
  ln -sfn "$REPO_DIR" "$SKILL_LINK"
  echo "==> Updated symlink $SKILL_LINK -> $REPO_DIR"
elif [ -e "$SKILL_LINK" ]; then
  echo "!!  $SKILL_LINK already exists and is NOT a symlink."
  echo "!!  Back it up / remove it, then re-run (or set CLAUDE_SKILLS_DIR to install elsewhere)."
else
  ln -s "$REPO_DIR" "$SKILL_LINK"
  echo "==> Linked $SKILL_LINK -> $REPO_DIR"
fi

echo
echo "Done. Next:"
echo "  1. Authenticate once:  vastai set api-key <YOUR_KEY>   (https://console.vast.ai/ → Account)"
echo "  2. Try it:             python3 $SKILL_LINK/vast.py search --gpu RTX_4090"
echo "  In Claude Code, the skill triggers on /vastai or \"rent a GPU\" / \"spin up a vast box\"."
