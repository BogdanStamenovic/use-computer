#!/usr/bin/env bash
# Starts an isolated headless GNOME Shell with a virtual 1920x1080 monitor, so
# end-to-end tests never drive the developer's real desktop.
#
# Isolation constraints, learned the hard way:
# - Own session bus (dbus-run-session): Mutter's RemoteDesktop/ScreenCast names
#   are per-bus, so tools pointed at this bus talk to the headless shell.
# - Own XDG_RUNTIME_DIR: at-spi-bus-launcher binds $XDG_RUNTIME_DIR/at-spi/bus,
#   so starting one against the real runtime dir would clobber the desktop's
#   accessibility bus. D-Bus activation can't be used either: it hands off to
#   the real systemd user instance, where the unit already runs ("unit failed").
# - PipeWire sockets are symlinked in because screencast frames flow through the
#   real PipeWire daemon.
#
# Usage: headless-shell.sh ENVFILE
#   Writes shell `export` lines for the headless session to ENVFILE, then blocks
#   until the shell exits. Kill the process group to stop it.
set -euo pipefail
envfile=${1:?env file}
real_rt=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
rt=$(mktemp -d "${TMPDIR:-/tmp}/uc-headless.XXXXXX")
chmod 700 "$rt"
for s in pipewire-0 pipewire-0-manager; do
  if [ -e "$real_rt/$s" ]; then ln -s "$real_rt/$s" "$rt/$s"; fi
done
trap 'rm -rf "$rt"' EXIT
unset DISPLAY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS AT_SPI_BUS_ADDRESS
export XDG_RUNTIME_DIR=$rt XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=GNOME
export UC_ENVFILE=$envfile
dbus-run-session -- bash -c '
  /usr/lib/at-spi-bus-launcher --launch-immediately &
  for _ in $(seq 50); do [ -S "$XDG_RUNTIME_DIR/at-spi/bus" ] && break; sleep 0.1; done
  # The a11y bus also activates the registry through the real systemd instance,
  # so it has to be started directly, and before the shell registers with it.
  /usr/lib/at-spi2-registryd &
  sleep 0.5
  gnome-shell --headless --wayland --no-x11 --virtual-monitor 1920x1080 --wayland-display uc-test-0 &
  shell=$!
  {
    echo "export DBUS_SESSION_BUS_ADDRESS=\"$DBUS_SESSION_BUS_ADDRESS\""
    echo "export XDG_RUNTIME_DIR=\"$XDG_RUNTIME_DIR\""
    echo "export WAYLAND_DISPLAY=uc-test-0"
    echo "export GDK_BACKEND=wayland"
    echo "unset DISPLAY"
  } > "$UC_ENVFILE.tmp" && mv "$UC_ENVFILE.tmp" "$UC_ENVFILE"
  wait $shell
'
