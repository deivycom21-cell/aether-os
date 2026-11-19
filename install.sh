#!/bin/sh
# AetherOS one-line installer for macOS / Linux.
#
# Run it inside your project folder:
#   curl -LsSf https://raw.githubusercontent.com/deivycom21-cell/aether-os/main/install.sh -o /tmp/aether-install.sh
#   sh /tmp/aether-install.sh
#
# 1. installs uv if missing  2. checks git (never runs sudo)  3. installs AetherOS as an isolated tool
# (uv downloads Python 3.12 if needed)  4. runs `aether setup` in the current folder.
#
# Options: AETHER_REF=<branch|tag>  AETHER_SOURCE=<local checkout or URL>  AETHER_SETUP_ARGS="--all --no-hooks"
set -eu

REF="${AETHER_REF:-main}"
SOURCE="${AETHER_SOURCE:-https://github.com/deivycom21-cell/aether-os/archive/refs/heads/$REF.zip}"

step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }

printf '\033[35mAETHER//OS installer\033[0m\n'

step "uv"
if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found, installing it from astral.sh ..."
    uv_installer="$(mktemp)"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh -o "$uv_installer"
    else
        wget -q https://astral.sh/uv/install.sh -O "$uv_installer"
    fi
    sh "$uv_installer"
    rm -f "$uv_installer"
    PATH="${UV_INSTALL_DIR:-$HOME/.local/bin}:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
fi
command -v uv >/dev/null 2>&1 || { echo "uv is not on PATH; open a new terminal and run this again" >&2; exit 1; }
echo "uv: $(command -v uv)"

step "git"
if command -v git >/dev/null 2>&1; then
    echo "git: $(command -v git)"
else
    case "$(uname -s)" in
        Darwin) warn "git not found: run 'xcode-select --install' (AetherOS works without it, with fewer features)" ;;
        *) warn "git not found: install it with your package manager, e.g. 'sudo apt install git' (AetherOS works without it, with fewer features)" ;;
    esac
fi

step "AetherOS ($SOURCE)"
uv tool install --force --python 3.12 --reinstall-package aether-os "$SOURCE"
uv tool update-shell >/dev/null 2>&1 || true
BIN="$(uv tool dir --bin)"
PATH="$BIN:$PATH"
export PATH
"$BIN/aether" version

step "aether setup in $(pwd)"
status=0
# shellcheck disable=SC2086
"$BIN/aether" setup ${AETHER_SETUP_ARGS:-} || status=$?
if [ "$status" -eq 2 ]; then
    warn "AetherOS is installed. Open your project folder and run:  aether setup"
elif [ "$status" -ne 0 ]; then
    warn "aether setup reported problems (see above). Re-run it any time with:  aether setup"
fi
printf '\nNew terminals will find aether on PATH automatically.\n'
