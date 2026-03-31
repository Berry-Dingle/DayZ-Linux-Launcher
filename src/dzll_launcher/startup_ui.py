#!/usr/bin/env python3
# startup_ui.py
#
# Startup overlay UI extracted from main.py with ZERO behavior change.

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk


def build_startup_overlay(window, overlay: Gtk.Overlay) -> Gtk.Widget:
    """
    Builds the startup dimmer/band overlay and assigns:
      - window.startup_dimmer
      - window.startup_spinner
      - window.startup_label
    Returns startup_dimmer.
    """
    window.startup_dimmer = Gtk.Box()
    window.startup_dimmer.set_hexpand(True)
    window.startup_dimmer.set_vexpand(True)
    window.startup_dimmer.add_css_class("startup-dim")
    window.startup_dimmer.set_visible(False)
    window.startup_dimmer.set_can_target(True)

    center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    center.set_hexpand(True)
    center.set_vexpand(True)
    center.set_halign(Gtk.Align.FILL)
    center.set_valign(Gtk.Align.CENTER)

    band = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    band.add_css_class("startup-band")
    band.set_size_request(-1, 240)
    band.set_hexpand(True)
    band.set_halign(Gtk.Align.FILL)
    band.set_valign(Gtk.Align.CENTER)

    spacer_l = Gtk.Box()
    spacer_l.set_hexpand(True)
    spacer_r = Gtk.Box()
    spacer_r.set_hexpand(True)

    band_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
    band_inner.set_halign(Gtk.Align.CENTER)
    band_inner.set_valign(Gtk.Align.CENTER)
    band_inner.set_hexpand(False)

    window.startup_spinner = Gtk.Spinner()
    window.startup_spinner.set_spinning(True)
    band_inner.append(window.startup_spinner)

    window.startup_label = Gtk.Label(label="Updating The Server Database, Please Wait…")
    window.startup_label.add_css_class("startup-label")
    window.startup_label.set_xalign(0.5)
    window.startup_label.set_halign(Gtk.Align.CENTER)
    band_inner.append(window.startup_label)

    band.append(spacer_l)
    band.append(band_inner)
    band.append(spacer_r)

    center.append(band)
    window.startup_dimmer.append(center)
    overlay.add_overlay(window.startup_dimmer)

    return window.startup_dimmer