#!/usr/bin/env bash
# Install intensifier: dependencies + an `intensify` command on your PATH.
#   ./install.sh              install deps (Homebrew if present) and link the command
#   ./install.sh --no-deps    only link the command
#   ./install.sh --uninstall  remove the command link
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${INTENSIFY_BIN_DIR:-$HOME/.local/bin}"
LINK="$BIN_DIR/intensify"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

if [[ "${1-}" == "--uninstall" ]]; then
  if [[ -L "$LINK" ]]; then rm "$LINK" && say "removed $LINK"; else warn "no link at $LINK"; fi
  exit 0
fi

if [[ "${1-}" != "--no-deps" ]]; then
  if command -v brew >/dev/null; then
    say "installing dependencies with Homebrew (Brewfile)"
    brew bundle --file="$REPO/Brewfile"
  elif command -v apt-get >/dev/null; then
    say "Debian/Ubuntu detected - installing with apt (needs sudo)"
    sudo apt-get update -qq && sudo apt-get install -y imagemagick gifsicle librsvg2-bin webp python3
  elif command -v dnf >/dev/null; then
    say "Fedora/RHEL detected - installing with dnf (needs sudo)"
    sudo dnf install -y ImageMagick gifsicle librsvg2-tools libwebp-tools python3
  elif command -v pacman >/dev/null; then
    say "Arch detected - installing with pacman (needs sudo)"
    sudo pacman -S --needed --noconfirm imagemagick gifsicle librsvg libwebp python
  else
    warn "no known package manager; install python3 + imagemagick (+ gifsicle) yourself"
  fi
fi

chmod +x "$REPO/intensify.py"
mkdir -p "$BIN_DIR"
if [[ -e "$LINK" && ! -L "$LINK" ]]; then
  warn "$LINK exists and is not a symlink - leaving it alone"
  exit 1
fi
ln -sfn "$REPO/intensify.py" "$LINK"
say "linked $LINK -> $REPO/intensify.py"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. Add this to your shell profile:"
     echo "    export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

echo
"$REPO/intensify.py" --doctor || true
echo
say "done - run: intensify            (web UI)"
say "        or: intensify image.png  (CLI)"
