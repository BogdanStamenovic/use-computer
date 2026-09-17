# use-computer

Lets an agent (Claude, via MCP) see and operate a GNOME desktop on Wayland the way a person
does: screenshots, mouse, keyboard, the accessibility tree with clickable refs, OCR, the
clipboard, and listening to whatever the computer is playing. Think Claude in Chrome, but for
the whole desktop instead of one browser tab.

It ships as three things that share one daemon: an MCP server (`use-computer-mcp`), a CLI
(`use-computer`), and a Claude skill (`skills/use-computer`) that teaches the workflow.

**Real limitations, up front:**

- **GNOME Shell on Wayland only.** It uses Mutter's private RemoteDesktop/ScreenCast D-Bus
  API. KDE, Sway, Hyprland and X11 sessions are not supported and nothing here pretends to be.
- **Single monitor** (the primary one) today.
- **It drives your real session.** The pointer moves; typing goes to the focused window. Don't
  use the machine at the same time.
- Accessibility coverage depends on the app. GTK/GNOME apps and Firefox are good;
  Chromium/Electron apps expose little unless started with `--force-renderer-accessibility`;
  games and canvases are pixels only.
- Audio transcription is CPU-only here: about real time on long audio, ~15 s minimum per call.

## What works today

| Capability | How | Measured on GNOME 50 / i7-1265U |
|---|---|---|
| Screenshot | Mutter ScreenCast → PipeWire → GStreamer, downscaled to 1280 px wide WebP | ~0.3 s first, ~15 KB |
| Zoom | full-resolution crop of a region | ~0.06 s |
| Click / double / triple / right / middle, hover, drag, scroll | Mutter RemoteDesktop absolute pointer events | verified against a GTK4 test app's own event log |
| Key combos | X keysyms (`ctrl+shift+t`, `Tab Return`, `F5`) | verified, incl. `shift+Tab` → `ISO_Left_Tab` |
| Typing any text | ASCII as keys; everything else pasted via Mutter's clipboard, old clipboard restored | `Hi šđčćž Ћирилица + done` arrives intact |
| Typed-text verification | reads the focused field back through AT-SPI | `verified: true/false/null` |
| Accessibility tree + refs | AT-SPI; refs clickable via the element's own action | 10–20 ms for a small window |
| Screen positions for refs | window-relative extents + window origin (see below) | exact for X11 apps; matched for Wayland apps |
| `form_input` | EditableText / Value / toggle action, mouse fallback | |
| OCR | tesseract, 2× upscale, sparse-text mode | ~0.7 s full 1080p screen |
| Wait for text / element / idle screen | OCR polling, AT-SPI polling, damage-driven frame timing | |
| Open app | Activities search (Super, type, Enter) | |
| Clipboard get/set | Mutter selection API | round-trips Unicode |
| Listen / transcribe | `pw-record` of the default output's monitor + faster-whisper large-v3-turbo int8 | 62 s speech in 62 s, 2.1 GB RAM |
| Kill switch | stopping screen sharing from the top bar revokes control until resumed | |
| Multi-agent safety | one daemon, a 30 s control lease per client | |

## What does not exist yet

- Multi-monitor support.
- Window management beyond "open/switch app via search" (no move/resize/list-geometry API —
  GNOME blocks `Shell.Introspect` for outside callers).
- A way to see the mouse cursor in screenshots (it's hidden on purpose).
- Non-GNOME compositors.

## Install

With [ownbox](https://github.com/BogdanStamenovic/ownbox):

```bash
ownbox install use-computer
```

Setup (`ownbox.sh setup`) does the following, and `ownbox uninstall` undoes the non-package parts:

1. Installs missing system packages with pacman (asks sudo): `python-gobject`,
   `gst-plugin-pipewire`, `at-spi2-core`, `wl-clipboard`, `tesseract`, `tesseract-data-eng`,
   `tesseract-data-osd`. More OCR languages: `USE_COMPUTER_TESSERACT_LANGS="eng osd srp srp_latn"`.
2. Creates `.venv` **with system site-packages** (PyGObject comes from the distro).
3. Sets `org.gnome.desktop.interface toolkit-accessibility true` (restored on uninstall if it
   was false). Apps started *after* this expose their accessibility tree.
4. Registers the MCP server in `~/.claude.json` (user scope, the same entry
   `claude mcp add --scope user` would write). Start a new Claude session afterwards.
5. Runs `use-computer doctor`.

Audio is opt-in because the model is 1.6 GB:

```bash
use-computer audio setup
```

Manual install:

```bash
git clone https://github.com/BogdanStamenovic/use-computer && cd use-computer
./ownbox.sh setup
```

## Usage

From Claude: just ask for something that needs the desktop; the skill loads and the
`use-computer` MCP tools do the work.

From a shell:

```bash
use-computer doctor
use-computer screenshot                 # prints a PNG path
use-computer tree                       # accessibility tree of the focused window
use-computer find "Save"
use-computer click --ref ref_12
use-computer click --at 640,360         # screenshot pixels; add --screen for real pixels
use-computer type "Zdravo, svete"
use-computer key "ctrl+s"
use-computer ocr --region 0,0,640,360
use-computer audio listen 20            # what the computer plays, for 20 s
use-computer stop                       # end the session, indicator disappears
```

| Command | What it does |
|---|---|
| `screenshot`, `zoom X0,Y0,X1,Y1` | capture to a file |
| `click`, `double-click`, `triple-click`, `right-click`, `middle-click` | `--at X,Y` or `--ref`; `--modifiers ctrl`; `--mouse` forces a real click on refs |
| `move`, `drag FROM TO`, `scroll [--at] DIR [N]` | pointer |
| `key KEYS [--repeat N]`, `type TEXT [--ref]` | keyboard |
| `tree [--scope all\|APP] [--all] [--ref]`, `find QUERY`, `form-input REF VALUE`, `action REF NAME` | accessibility |
| `ocr [--region] [--lang]`, `wait-for --text/--name/--idle [--gone]` | reading and waiting |
| `windows`, `open-app NAME`, `clipboard get\|set` | desktop |
| `batch FILE\|-` | JSON list of `{"op", "args"}` |
| `audio setup\|status\|listen SECONDS\|transcribe PATH` | hearing |
| `status`, `stop`, `resume`, `shutdown`, `daemon`, `doctor`, `mcp-register`, `mcp-unregister` | plumbing |

`--json` prints raw results. Exit codes: 0 ok, 1 failed, 2 usage error, 3 control revoked by the user.

## How it works

```
Claude ──stdio──► use-computer-mcp ─┐
Bash   ─────────► use-computer (CLI)├─ unix socket ─► daemon (one per D-Bus session)
                                    ┘                   │
          ┌─────────────────────────────────────────────┼──────────────────────────┐
          │ Mutter RemoteDesktop session: pointer, keysyms, clipboard              │
          │ Mutter ScreenCast stream ─► PipeWire ─► GStreamer appsink (latest frame)│
          │ AT-SPI (accessibility bus): tree, refs, actions, text                  │
          │ tesseract (subprocess)                                                 │
          └────────────────────────────────────────────────────────────────────────┘
listen/transcribe run in the calling process (pw-record + faster-whisper), not the daemon.
```

**Why a daemon.** A Mutter session dies with the D-Bus connection that created it, CLI calls
are separate processes, and several Claude sessions must not fight over one pointer. The daemon
is single-threaded (it pumps GLib between requests), holds the session and the newest frame,
stops the session after 90 s idle (the sharing indicator disappears) and exits after 30 min.

**Why Mutter's API and not the portal.** No consent dialog, no restore tokens, absolute
coordinates. The cost is GNOME-only.

**Why typing pastes non-ASCII.** Mutter turns a keysym into a key press only if the active
layout has it, and drops it silently otherwise. Switching layouts from a remote session is
refused unless the session installed the keymap itself. Pasting is the only path that works
for all text; the clipboard is restored afterwards, and handed to `wl-copy` when the session
ends so it isn't lost.

**Why screen positions need a trick.** Wayland apps don't know where their window is, so
AT-SPI gives window-relative boxes and bogus screen boxes. GNOME Shell's own accessibility tree
does expose every window actor's screen rectangle, but that includes client-side shadows of
unknown per-side size. `locate.py` finds the content rectangle inside the actor by scoring every
possible offset on edge strength along its perimeter (shadows are smooth, window borders are
sharp). X11 apps report real screen coordinates and skip this.

**Screenshots settle.** The screencast is damage-driven, so "no new frame for 120 ms" means the
screen stopped changing; screenshots wait for that (max 1 s).

## Development

```bash
.venv/bin/pytest -q                    # unit tests, no desktop needed
```

End-to-end tests run against a **separate headless GNOME Shell** so they never touch your
desktop:

```bash
tests/e2e/headless-shell.sh /tmp/uc.env &      # isolated shell, bus, runtime dir
. /tmp/uc.env
python tests/e2e/harness.py /tmp/h.log &       # GTK4 app that logs every event it receives
python tests/e2e/mcp_smoke.py /tmp             # drives the MCP server over stdio
tests/e2e/headless-stop.sh
```

Read the comments in `headless-shell.sh` before changing it. Running a second shell with the
real `XDG_RUNTIME_DIR` clobbers the desktop's accessibility bus socket and document portal
mount (this happened; see `docs/development-log.md`). The headless shell also doesn't register
itself with AT-SPI, so window-position resolution can only be tested on a real session.

## License

MIT
