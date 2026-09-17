"""GTK4 test window that logs every input it receives as JSON lines.

usage: python harness.py LOGFILE
The log is the ground truth for e2e tests: it records what the app actually got,
independently of what use-computer claims it sent.
"""

import json
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk

LOG = sys.argv[1]


def log(**kw):
    kw["t"] = round(time.time(), 3)
    with open(LOG, "a") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\n")


def activate(app):
    win = Gtk.ApplicationWindow(application=app, title="uc-harness")
    win.set_default_size(600, 400)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=40,
                  margin_start=40, margin_end=40)
    entry = Gtk.Entry(placeholder_text="type here")
    entry.connect("changed", lambda e: log(ev="text", value=e.get_text()))
    btn = Gtk.Button(label="Press me")
    counter = {"n": 0}

    def pressed(_b):
        counter["n"] += 1
        btn.set_label(f"Pressed {counter['n']}")
        log(ev="button", n=counter["n"])

    btn.connect("clicked", pressed)
    check = Gtk.CheckButton(label="Enable thing")
    check.connect("toggled", lambda c: log(ev="check", active=c.get_active()))
    area = Gtk.DrawingArea(content_height=150)
    click = Gtk.GestureClick(button=0)
    click.connect("pressed", lambda g, n, x, y: log(ev="area_click", button=g.get_current_button(),
                                                    n=n, x=x, y=y))
    area.add_controller(click)
    drag = Gtk.GestureDrag()
    drag.connect("drag-begin", lambda g, x, y: log(ev="drag_begin", x=x, y=y))
    drag.connect("drag-end", lambda g, dx, dy: log(ev="drag_end", dx=dx, dy=dy))
    area.add_controller(drag)
    scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
    scroll.connect("scroll", lambda c, dx, dy: log(ev="scroll", dx=dx, dy=dy) or True)
    area.add_controller(scroll)
    keys = Gtk.EventControllerKey()
    keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    keys.connect("key-pressed", lambda c, kv, code, state: log(
        ev="key", name=Gdk.keyval_name(kv), ctrl=bool(state & Gdk.ModifierType.CONTROL_MASK),
        shift=bool(state & Gdk.ModifierType.SHIFT_MASK)) and False)
    win.add_controller(keys)
    for w in (entry, btn, check, Gtk.Label(label="click area below"), area):
        box.append(w)
    win.set_child(box)
    win.present()
    log(ev="ready")


app = Gtk.Application(application_id="dev.bogdan.UcHarness")
app.connect("activate", activate)
app.run([])
