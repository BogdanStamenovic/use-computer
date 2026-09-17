# Development log

What was tried while building use-computer, in order, including the dead ends. Machine:
Arch Linux, GNOME Shell 50.4 on Wayland, mutter 50.4, Python 3.14, i7-1265U, 1920x1080.

## 1. Picking the input and capture route

- `xdotool`, `wtype`, `ydotool`, `grim`: not installed, and the first two don't work on
  GNOME Wayland anyway (xdotool only reaches XWayland windows, wtype needs the wlroots
  virtual-keyboard protocol, grim needs wlr-screencopy). `/dev/uinput` is root-only.
- `org.gnome.Shell.Screenshot.Screenshot`: **AccessDenied** ("Screenshot is not allowed"),
  as expected since GNOME 41 allowlists callers.
- `org.gnome.Shell.Introspect.GetWindows`: **AccessDenied** too. No window list or geometry
  API for outside callers.
- `org.gnome.Mutter.RemoteDesktop` + `org.gnome.Mutter.ScreenCast`: work with no dialog.
  Spike: session → `RecordMonitor("")` → `PipeWireStreamAdded` in 22 ms → first frame through
  `pipewiresrc` 41 ms later, and `NotifyPointerMotionAbsolute` put the cursor exactly at the
  center. Chosen.

## 2. Accessibility coordinates on Wayland

- GTK4 reports every element's SCREEN extents at (0, 0); WINDOW extents are correct.
- GNOME Shell's own AT-SPI tree lists `Wayland window` actors with real screen rectangles,
  but those include client-side shadows. For a 600x400 GTK4 window the actor was 628x429.
- Clicked a known point in a test window and read the app's logged local coordinate to get
  ground truth: shadow is 14 px left, 12 px top (and so 17 px bottom). Asymmetric, so it
  can't be derived from sizes. → `locate.py` edge-strength search.
- `Atspi.Component.grab_focus()` on a GTK4 entry: `atspi_error (1)`. GTK4 doesn't implement
  it, so focusing a field means clicking it.

## 3. Typing non-ASCII

- `NotifyKeyboardKeysym` with Latin-1 / Unicode keysyms for `šđčćž` and Cyrillic: silently
  dropped. ASCII worked. Mutter only maps keysyms present in the active layout.
- Legacy Latin-2 keysyms (`0x1a9` = š …): also dropped.
- `SetKeymapLayoutIndex(2)` to switch to the user's `rs+latin` layout: **"Invalid current
  keymap (0)"**. It only works on a keymap the session installed via `SetKeymap`, and that
  would replace the seat's keymap for the physical keyboard too. Rejected. It also wouldn't
  cover Cyrillic, which isn't configured.
- `wl-copy` + Ctrl+V: rejected as the primary path because on GNOME wl-copy briefly maps its
  own surface to get focus, which can steal focus from the field being typed into.
- **Chosen:** Mutter's own selection API (`SetSelection` / `SelectionTransfer` /
  `SelectionWrite`) + Ctrl+V (Ctrl+Shift+V in terminals), previous clipboard restored,
  handed to `wl-copy` when the session ends so it survives. Verified:
  `Hi šđčćž Ћирилица + done` arrives intact.

## 4. Test isolation: a headless GNOME Shell — and an incident

- Goal: develop without moving the user's real mouse while they work.
- First attempt: `dbus-run-session -- gnome-shell --headless --virtual-monitor 1920x1080`
  with the real `XDG_RUNTIME_DIR`. RemoteDesktop/ScreenCast worked on the private bus.
- **Incident:** AT-SPI registry activation from the private bus failed ("unit failed",
  handed to the real systemd user instance). Worse, that run spawned a second
  `at-spi-bus-launcher` that **overwrote the real `/run/user/1000/at-spi/bus` socket**, plus
  17 orphaned GNOME services (gvfsd, portals, dconf-service, evolution, goa). The orphaned
  `xdg-document-portal` held the only `/run/user/1000/doc` FUSE mount. New apps on the real
  desktop lost accessibility.
- Verified in the headless shell first that GNOME Shell survives losing its a11y bus, then
  repaired: killed the orphans (identified by `DBUS_SESSION_BUS_ADDRESS=/tmp/dbus-…` in their
  environment), `systemctl --user restart at-spi-dbus-bus.service` (new socket `bus_0`;
  gnome-shell and GTK apps re-registered), removed the dead socket file, re-activated the
  real document portal via `GetMountPoint`.
- Fix: private `XDG_RUNTIME_DIR` with only `pipewire-0` symlinked in, `at-spi-bus-launcher`
  and `at-spi2-registryd` started directly, registry before the shell.
- Twice `pkill -f PATTERN` / `pgrep -f PATTERN` matched the shell running the command and
  killed it (exit 144). The stop script matches on process environment instead.
- Remaining limitation: the headless GNOME Shell never registers itself with AT-SPI, so
  window-origin resolution can't be exercised headless. It also logs "Will monitor session N"
  for the real login session, so runs are kept short.

## 5. Bugs found by end-to-end runs

- Clicks "missed" in the headless shell: they didn't. The shell starts in the Activities
  overview and an earlier click had opened the calendar popup. Tests now activate the window
  first.
- `Atspi.Action.get_name(i)` returns `""`; the deprecated `get_action_name(i)` works.
- GTK4 labels expose `link.open` (noise); GTK4 check boxes expose no action at all.
  `form_input` falls back to a real click.
- `verified` was always `null`: `acc.get_text_iface().get_text(0, n)` resolves to the
  deprecated `Accessible.get_text()` (0 args) in PyGObject. Interface methods are now called
  unbound, `Atspi.Text.get_text(acc, 0, n)`. This also fixed field values in the tree.
- Suspected a stale libatspi cache in the long-lived daemon. Tested names and focus states
  after changes: both fresh. Not the cause, no change made.

## 6. OCR tuning (1920x1080 GNOME frame)

| upscale | psm | time | result |
|---|---|---|---|
| 1.0 | 11 | 0.46 s | "Press:", "41", "En:" (unusable) |
| 1.5 | 11 | 0.65 s | mostly right |
| 2.0 | 11 | 0.68 s | all labels right |
| 3.0 | 11 | 1.10 s | same as 2.0 |

Chose 2x (3x for regions ≤ 600 px), sparse-text mode.

## 7. Whisper

- faster-whisper 1.2.1 / CTranslate2 4.8.2 install fine on Python 3.14.
- large-v3-turbo CTranslate2 download is **1.6 GB** (fp16 on disk; int8 at load), not the
  800 MB first estimated. RAM ~2.1 GB during transcription.
- espeak-ng English, 6.9 s: word-perfect, 14.9 s. Beam 1 vs 5 makes no difference; 4 → 12
  threads: 23 s → 16 s. The floor comes from Whisper's fixed 30 s window.
- 62 s of speech: 62 s to transcribe, 9/9 sentences correct → ~1x real time on long audio.
- espeak-ng Serbian: auto-detected as Italian; with `language="sr"` mostly right. espeak's
  Serbian voice is barely intelligible, so this says little about real Serbian speech.
- `pw-record -P '{ stream.capture.sink=true }'` captures the default output's monitor
  (what the speakers play) without a microphone.
