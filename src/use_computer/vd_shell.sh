#!/usr/bin/env bash
# Supervisor for one use-computer virtual desktop: an isolated headless GNOME Shell.
#
# Isolation constraints, learned the hard way (see docs/development-log.md):
# - Own session bus (dbus-run-session): Mutter's RemoteDesktop/ScreenCast names are
#   per-bus, so a client pointed at this bus talks to this shell and no other.
# - Own XDG_RUNTIME_DIR: at-spi-bus-launcher binds $XDG_RUNTIME_DIR/at-spi/bus, so
#   sharing the real one would clobber the desktop's accessibility bus. D-Bus
#   activation can't be used either: it hands off to the real systemd user instance,
#   where the unit already runs ("unit failed").
# - PipeWire sockets are symlinked in, because screencast frames flow through the
#   real PipeWire daemon -- which is also why a viewer running on the real session
#   can consume this shell's stream.
#
# Runs in the foreground and blocks until the shell exits. The caller starts it with
# setsid and kills the whole process group to stop it.
set -euo pipefail

envfile=${UC_VD_ENVFILE:?UC_VD_ENVFILE is required}
rt=${UC_VD_RUNTIME:?UC_VD_RUNTIME is required}
size=${UC_VD_SIZE:-1920x1080}
wl=${UC_VD_DISPLAY:-uc-vd-0}
real_rt=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}

# at-spi helper locations differ by distro; both must be started directly rather
# than activated, so the registry is up before the shell registers with it.
find_helper() {
  local name=$1 p
  for p in /usr/lib/"$name" /usr/libexec/"$name" /usr/lib/at-spi2-core/"$name" \
           /usr/lib/*/at-spi2-core/"$name"; do
    [ -x "$p" ] && { echo "$p"; return 0; }
  done
  return 1
}
launcher=$(find_helper at-spi-bus-launcher) || { echo "at-spi-bus-launcher not found" >&2; exit 1; }
registry=$(find_helper at-spi2-registryd) || { echo "at-spi2-registryd not found" >&2; exit 1; }

mkdir -p "$rt"
chmod 700 "$rt"
for s in pipewire-0 pipewire-0-manager; do
  if [ -e "$real_rt/$s" ] && [ ! -e "$rt/$s" ]; then ln -s "$real_rt/$s" "$rt/$s"; fi
done

unset DISPLAY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS AT_SPI_BUS_ADDRESS
export XDG_RUNTIME_DIR=$rt XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=GNOME
export UC_VD_ENVFILE=$envfile UC_VD_SIZE=$size UC_VD_DISPLAY=$wl
export UC_LAUNCHER=$launcher UC_REGISTRY=$registry

exec dbus-run-session -- bash -c '
  "$UC_LAUNCHER" --launch-immediately &
  for _ in $(seq 50); do [ -S "$XDG_RUNTIME_DIR/at-spi/bus" ] && break; sleep 0.1; done
  "$UC_REGISTRY" &
  sleep 0.5
  gnome-shell --headless --wayland --no-x11 \
      --virtual-monitor "$UC_VD_SIZE" --wayland-display "$UC_VD_DISPLAY" &
  shell=$!
  {
    echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"
    echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
    echo "WAYLAND_DISPLAY=$UC_VD_DISPLAY"
    echo "GDK_BACKEND=wayland"
  } > "$UC_VD_ENVFILE.tmp" && mv "$UC_VD_ENVFILE.tmp" "$UC_VD_ENVFILE"
  wait $shell
'
