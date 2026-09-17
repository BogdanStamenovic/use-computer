#!/usr/bin/env bash
# Ownbox lifecycle for use-computer: setup | update | remove.
#
# Ownbox clones the repo, links the skill and the `use-computer` launcher, and runs
# this script inside the checkout. Everything Ownbox has no manifest key for lives
# here: system packages, the venv, the GNOME accessibility setting, and the MCP
# server registration in ~/.claude.json.
#
# Environment knobs:
#   USE_COMPUTER_TESSERACT_LANGS  extra OCR languages to install (default: "eng osd")
#   USE_COMPUTER_AUDIO=1          also install faster-whisper (model is a separate,
#                                 explicit `use-computer audio setup --no-install`)
#   USE_COMPUTER_NO_SUDO=1        never call sudo; report missing packages instead
set -euo pipefail

here=$(cd -- "$(dirname -- "$0")" && pwd)
cd "$here"
state=.ownbox-state
action=${1:-}

say() { printf 'use-computer: %s\n' "$*" >&2; }

require_linux_gnome() {
  if [ "$(uname -s)" != Linux ]; then
    say "only Linux (GNOME on Wayland) is supported"; exit 1
  fi
  case "${XDG_CURRENT_DESKTOP:-}:${XDG_SESSION_TYPE:-}" in
    *GNOME*:wayland) ;;
    *) say "warning: this session is '${XDG_CURRENT_DESKTOP:-?}' on '${XDG_SESSION_TYPE:-?}'; control only works in GNOME on Wayland" ;;
  esac
}

install_system_packages() {
  local langs=${USE_COMPUTER_TESSERACT_LANGS:-eng osd}
  local pkgs=(python-gobject gst-plugin-pipewire gstreamer at-spi2-core pipewire wl-clipboard tesseract)
  for l in $langs; do pkgs+=("tesseract-data-$l"); done

  if ! command -v pacman >/dev/null 2>&1; then
    say "not an Arch system: install the equivalents of: ${pkgs[*]}"
    say "(Debian/Ubuntu: python3-gi gir1.2-atspi-2.0 gstreamer1.0-pipewire tesseract-ocr wl-clipboard)"
    return 0
  fi
  local missing
  missing=$(pacman -T "${pkgs[@]}" || true)
  [ -z "$missing" ] && { say "system packages present"; return 0; }
  missing=$(echo "$missing" | tr '\n' ' ')
  if [ "${USE_COMPUTER_NO_SUDO:-0}" = 1 ]; then
    say "missing system packages (USE_COMPUTER_NO_SUDO=1, not installing): $missing"
    return 0
  fi
  say "installing system packages: $missing"
  if sudo -n true 2>/dev/null || [ -t 0 ]; then
    # shellcheck disable=SC2086
    sudo pacman -S --needed --noconfirm $missing
  else
    say "cannot sudo without a terminal; run: sudo pacman -S --needed $missing"
    return 0
  fi
}

make_venv() {
  # --system-site-packages: PyGObject and the GI typelibs come from the distro
  # (building PyGObject from PyPI needs cairo/GI headers and must match GNOME anyway).
  local py=/usr/bin/python3
  [ -x "$py" ] || py=$(command -v python3)
  if [ ! -x .venv/bin/python ]; then
    "$py" -m venv --system-site-packages .venv
  fi
  if ! .venv/bin/python -c 'import gi' 2>/dev/null; then
    say "the venv cannot import gi (PyGObject); install python-gobject and re-run"; exit 1
  fi
  local extra=""
  [ "${USE_COMPUTER_AUDIO:-0}" = 1 ] && extra="[audio]"
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python .venv/bin/python -q -e ".$extra"
  else
    .venv/bin/python -m pip install -q -e ".$extra"
  fi
}

enable_accessibility() {
  command -v gsettings >/dev/null 2>&1 || return 0
  local current
  current=$(gsettings get org.gnome.desktop.interface toolkit-accessibility 2>/dev/null || echo unknown)
  if [ ! -f "$state" ]; then
    echo "toolkit_accessibility_before=$current" > "$state"
  fi
  if [ "$current" != true ]; then
    gsettings set org.gnome.desktop.interface toolkit-accessibility true
    say "enabled GNOME toolkit accessibility (apps started from now on expose their UI tree)"
  fi
}

restore_accessibility() {
  [ -f "$state" ] || return 0
  local before
  before=$(sed -n 's/^toolkit_accessibility_before=//p' "$state")
  if [ "$before" = false ] && command -v gsettings >/dev/null 2>&1; then
    gsettings set org.gnome.desktop.interface toolkit-accessibility false
    say "restored toolkit-accessibility=false"
  fi
}

case $action in
  setup|update)
    require_linux_gnome
    install_system_packages
    make_venv
    enable_accessibility
    .venv/bin/use-computer shutdown >/dev/null 2>&1 || true  # reload code on update
    .venv/bin/use-computer mcp-register --command "$here/.venv/bin/use-computer-mcp" >&2
    .venv/bin/use-computer doctor >&2 || say "doctor reported problems (see above)"
    say "done. Start a new Claude session to load the MCP tools."
    ;;
  remove)
    if [ -x .venv/bin/use-computer ]; then
      .venv/bin/use-computer shutdown >/dev/null 2>&1 || true
      .venv/bin/use-computer mcp-unregister >&2 || true
    fi
    restore_accessibility
    say "removed. System packages (tesseract etc.) were left installed."
    ;;
  *)
    say "usage: ownbox.sh setup|update|remove"; exit 2
    ;;
esac
