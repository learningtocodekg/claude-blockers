#!/usr/bin/env bash
# Set up claude-blockers from a clone. macOS, Linux, WSL, containers.
#
#   ./setup.sh                                  local board on this machine
#   ./setup.sh --remote http://HOST:4317/ --token TOKEN
#                                               post to a board on the host
#
# Anything you pass is handed to `claude-blockers install`, so --home, --remote
# and --token all work here.

set -euo pipefail
cd "$(dirname "$0")"

# uv colours its output, and those escape codes would end up inside the path we
# read back out of it. Ask for plain text.
export NO_COLOR=1

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------- uv
if ! command -v uv >/dev/null 2>&1; then
  if ! command -v curl >/dev/null 2>&1; then
    warn "uv is not installed, and curl is not here to fetch it."
    warn "Install uv yourself and re-run this script:"
    die  "  https://docs.astral.sh/uv/getting-started/installation/"
  fi
  say "uv not found -- installing it (this is the only dependency)."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # The installer drops uv in one of these and updates the shell profile, but
  # this shell was started before that happened.
  for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$candidate/uv" ] && export PATH="$candidate:$PATH"
  done
  if ! command -v uv >/dev/null 2>&1; then
    warn "uv installed, but this shell cannot see it yet -- it read PATH at"
    warn "startup, and the installer changed it afterwards."
    die  "Open a NEW terminal and re-run this script. Nothing is broken."
  fi
fi

# ------------------------------------------------------------------ install
say "Installing claude-blockers from this checkout..."
# --reinstall-package is what makes `git pull && ./setup.sh` actually upgrade.
# --force replaces the tool environment but still accepts a wheel uv already
# built for this version from this path, and the version does not change between
# commits -- so an upgrade would reinstall byte-identical stale code and report
# success. Naming the package rebuilds ours without rebuilding the dependency
# tree behind it.
uv tool install --force --reinstall-package claude-blockers .

BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
EXE="$BIN/claude-blockers"
[ -x "$EXE" ] || die "expected $EXE to exist after install"

# --------------------------------------------------------------- wire it up
say "Wiring it into Claude Code..."
# ${1+"$@"} rather than "$@": macOS still ships bash 3.2 as /bin/bash, and there
# `set -u` treats "$@" with no positional parameters as an unbound variable. That
# would break `./setup.sh` with no arguments -- the headline command -- on every
# stock Mac, while working fine everywhere the author would have tested it.
"$EXE" install ${1+"$@"}

# ------------------------------------------------------------------- finish
# uv can add its own shim directory to your shell profile. Doing it here saves a
# step the user would otherwise have to read about and run themselves.
command -v claude-blockers >/dev/null 2>&1 || uv tool update-shell >/dev/null 2>&1 || true

echo
if command -v claude-blockers >/dev/null 2>&1; then
  say "Done. Start the board with:  claude-blockers serve"
else
  warn "Done -- installed, but this shell's PATH predates it."
  warn "$BIN has been added to your shell profile for new terminals."
  warn "Open one, then run:  claude-blockers serve"
  echo
  echo "  In this shell, the full path still works:"
  echo "    $EXE serve"
fi
echo "Restart any Claude Code sessions that are already running."
