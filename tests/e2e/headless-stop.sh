#!/usr/bin/env bash
# Stops everything started by headless-shell.sh. Matches processes by their
# XDG_RUNTIME_DIR environment, never by command line: `pkill -f PATTERN` also
# matches the shell running it.
set -u
uid=$(id -u)
match() {
  local p
  for p in $(pgrep -u "$uid"); do
    [ "$p" = "$$" ] && continue
    { tr '\0' '\n' < "/proc/$p/environ"; } 2>/dev/null \
      | grep -q '^XDG_RUNTIME_DIR=/tmp/uc-headless\.' && echo "$p"
  done
}
pids=$(match); [ -n "$pids" ] && kill $pids 2>/dev/null
sleep 1
pids=$(match); [ -n "$pids" ] && kill -9 $pids 2>/dev/null
rm -rf /tmp/uc-headless.* 2>/dev/null
echo stopped
