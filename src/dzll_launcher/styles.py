#!/usr/bin/env python3
# styles.py
#
# Canonical UI CSS for DZLL.
# Pure extraction from main.py (zero behavior change).

from __future__ import annotations


def is_ubuntu_platform(os_release_path: str = "/etc/os-release") -> bool:
    try:
        with open(os_release_path, "r", encoding="utf-8") as handle:
            values = {}
            for raw_line in handle:
                key, separator, raw_value = raw_line.partition("=")
                if separator:
                    values[key.strip()] = raw_value.strip().strip('"').strip("'")
    except OSError:
        return False

    distro_id = values.get("ID", "").casefold()
    distro_like = values.get("ID_LIKE", "").casefold().split()
    return distro_id == "ubuntu" or "ubuntu" in distro_like


def add_platform_css_classes(widget, os_release_path: str = "/etc/os-release") -> bool:
    ubuntu = is_ubuntu_platform(os_release_path)
    if ubuntu:
        widget.add_css_class("dzll-platform-ubuntu")
    return ubuntu


def get_app_css(
    DIVIDER_COLOR: str,
    SIDEBAR_WIDTH: int,
    DISCLAIMER_COLOR: str,
) -> bytes:
    css = f"""
        /* ---------- DZLL semantic dark palette ---------- */
        @define-color dzll_surface_panel #202326;
        @define-color dzll_surface_control #2f3438;
        @define-color dzll_surface_content #141618;
        @define-color dzll_surface_settings #181818;
        @define-color dzll_accent #0d686c;
        @define-color dzll_accent_hover #118084;
        @define-color dzll_accent_pressed #0a5558;

        @define-color dzll_text_primary #f1f3f4;
        @define-color dzll_text_secondary #c4c9cd;
        @define-color dzll_text_muted #8e979e;
        @define-color dzll_mod_metadata #e2e5e7;
        @define-color dzll_text_disabled #858d94;
        @define-color dzll_text_on_accent #ffffff;
        @define-color dzll_text_disclaimer #969da3;
        @define-color dzll_border #565f66;
        @define-color dzll_hover_border #68727a;
        @define-color dzll_divider #3b4248;
        @define-color dzll_focus #79aeb0;
        @define-color dzll_entry_focus #b6bdc2;
        @define-color dzll_mod_focus #4fb8b2;
        @define-color dzll_control_hover #3a4045;
        @define-color dzll_control_pressed #454c52;
        @define-color dzll_control_disabled #272b2f;
        @define-color dzll_selection #0d686c;
        @define-color dzll_scrim rgba(0, 0, 0, 0.55);
        @define-color dzll_popover_shadow rgba(0, 0, 0, 0.72);
        @define-color dzll_link #6ab0ff;
        @define-color dzll_link_hover #9dccff;
        @define-color dzll_warning #d6a94f;
        @define-color dzll_error #e04b4b;
        @define-color dzll_success #4fbf67;

        @define-color dzll_favourite_on #f5c542;
        @define-color dzll_favourite_off #7a7a7a;
        @define-color dzll_ping_good #37c871;
        @define-color dzll_ping_greeny #9ad43a;
        @define-color dzll_ping_yellow #e3c84a;
        @define-color dzll_ping_orange #e19a3a;
        @define-color dzll_ping_bad #e04b4b;
        @define-color dzll_monitor_idle #4aa3ff;
        @define-color dzll_monitor_active #ff4d4d;
        @define-color dzll_badge_1p #5aa832;
        @define-color dzll_badge_3p #1f7ad6;
        @define-color dzll_badge_subscribed #7b3ff2;
        @define-color dzll_badge_installed #d96b18;
        @define-color dzll_queue #f1f3f4;
        @define-color dzll_scrollbar_border #3fa9a5;
        @define-color dzll_switch_knob_on #d5dadd;
        @define-color dzll_scale_knob #c4c9cd;
        @define-color dzll_destructive #872626;
        @define-color dzll_destructive_hover #a53030;
        @define-color dzll_destructive_pressed #b93737;
        @define-color dzll_destructive_border #ff7878;
        @define-color dzll_stop #468cff;
        @define-color dzll_mod_status_online #4fbf67;
        @define-color dzll_mod_status_offline #d85b5b;
        @define-color dzll_mod_status_checking #d6a94f;
        @define-color dzll_mod_status_issue #d9784f;
        @define-color dzll_restart_learning #bfe84a;
        @define-color dzll_restart_confidence_high #37c871;
        @define-color dzll_restart_confidence_learning #9ad43a;
        @define-color dzll_startup_overlay rgba(0, 0, 0, 0.55);
        @define-color dzll_startup_band rgba(0, 0, 0, 0.75);
        @define-color dzll_steamcmd_log #33ff66;

        /* ---------- Explicit application-owned surfaces ---------- */
        .dzll-app-root,
        .dzll-app-root:backdrop {{
          background: @dzll_surface_panel;
          color: @dzll_text_primary;
        }}

        .dzll-app-root .sidebar-frame,
        .dzll-app-root .dzll-sidebar-toolbar,
        .dzll-app-root .dzll-search-area,
        .server-companion-panel,
        .server-companion-panel:backdrop {{
          background: @dzll_surface_panel;
          color: @dzll_text_primary;
        }}

        .dzll-app-root .dzll-browser-surface,
        .dzll-app-root .dzll-browser-surface > viewport,
        .dzll-app-root columnview.dzll-column-view,
        .dzll-app-root columnview.dzll-column-view > listview,
        .dzll-app-root columnview.dzll-column-view > listview > row,
        .dzll-app-root columnview.dzll-column-view listitem {{
          background: @dzll_surface_content;
          color: @dzll_text_primary;
        }}

        .dzll-app-root columnview.dzll-column-view > listview > row:hover,
        .dzll-app-root columnview.dzll-column-view listitem:hover {{
          background: @dzll_surface_content;
          background-color: @dzll_surface_content;
          background-image: none;
          color: @dzll_text_primary;
          border-bottom-color: @dzll_divider;
          box-shadow: none;
          outline: none;
          opacity: 1;
        }}

        .dzll-app-root columnview.dzll-column-view > listview > row:hover > listitem,
        .dzll-app-root columnview.dzll-column-view listitem:hover {{
          background: @dzll_surface_content;
          background-color: @dzll_surface_content;
        }}

        .dzll-app-root columnview.dzll-column-view > listview > row:hover > listitem > *,
        .dzll-app-root columnview.dzll-column-view listitem:hover > * {{
          background: transparent;
          background-color: transparent;
        }}

        .dzll-app-root label,
        .dzll-app-root label:backdrop,
        .server-companion-panel label,
        .server-companion-panel label:backdrop {{
          color: @dzll_text_primary;
        }}

        .dzll-app-root button.flat,
        .dzll-app-root button.flat image,
        .dzll-app-root button.flat:backdrop,
        .dzll-app-root button.flat:backdrop image,
        .server-companion-panel button.flat,
        .server-companion-panel button.flat image,
        .server-companion-panel button.flat:backdrop,
        .server-companion-panel button.flat:backdrop image {{
          color: @dzll_text_secondary;
        }}

        .dzll-app-root .dim-label,
        .dzll-app-root .dim-label:backdrop,
        .server-companion-panel .dim-label,
        .server-companion-panel .dim-label:backdrop {{
          color: @dzll_text_muted;
        }}

        .dzll-app-root .dimmed-entry {{
          color: @dzll_text_muted;
        }}

        /* ---------- Scoped native controls and transient surfaces ---------- */
        .dzll-app-root entry,
        .dzll-app-root searchentry,
        .dzll-app-root spinbutton {{
          background: @dzll_surface_content;
          color: @dzll_text_primary;
          border-color: @dzll_border;
          caret-color: @dzll_text_primary;
        }}

        .dzll-app-root entry:hover,
        .dzll-app-root searchentry:hover,
        .dzll-app-root spinbutton:hover {{
          border-color: @dzll_hover_border;
        }}

        .dzll-app-root entry:focus,
        .dzll-app-root entry:focus-within,
        .dzll-app-root searchentry:focus,
        .dzll-app-root searchentry:focus-within,
        .dzll-app-root spinbutton:focus-within {{
          border-color: @dzll_entry_focus;
          outline-color: @dzll_entry_focus;
        }}

        .dzll-app-root entry:disabled,
        .dzll-app-root searchentry:disabled,
        .dzll-app-root spinbutton:disabled {{
          background: @dzll_control_disabled;
          color: @dzll_text_disabled;
          border-color: @dzll_divider;
        }}

        .dzll-app-root button:not(.flat),
        .server-companion-panel button:not(.flat) {{
          background: @dzll_surface_control;
          color: @dzll_text_primary;
          border-color: @dzll_border;
          box-shadow: none;
        }}

        .dzll-app-root button:not(.flat):hover,
        .server-companion-panel button:not(.flat):hover {{
          background: @dzll_control_hover;
          background-image: none;
          border-color: @dzll_hover_border;
        }}

        .dzll-app-root button:not(.flat):active,
        .server-companion-panel button:not(.flat):active {{
          background: @dzll_control_pressed;
          background-image: none;
          border-color: @dzll_hover_border;
        }}

        .dzll-app-root button:focus-visible,
        .server-companion-panel button:focus-visible {{
          outline: 2px solid @dzll_focus;
          outline-offset: 1px;
        }}

        .dzll-app-root button:disabled,
        .server-companion-panel button:disabled {{
          background: @dzll_control_disabled;
          color: @dzll_text_disabled;
          border-color: @dzll_divider;
        }}

        .dzll-app-root button.suggested-action,
        .server-companion-panel button.suggested-action {{
          background: @dzll_accent;
          color: @dzll_text_on_accent;
          border-color: @dzll_hover_border;
        }}

        .dzll-app-root button.suggested-action:hover,
        .server-companion-panel button.suggested-action:hover {{
          background: @dzll_accent_hover;
        }}

        .dzll-app-root button.suggested-action:active,
        .server-companion-panel button.suggested-action:active {{
          background: @dzll_accent_pressed;
        }}

        .dzll-app-root dropdown > button,
        .dzll-app-root menubutton > button,
        .dzll-app-root dropdown.dzll-dropdown > button {{
          background: @dzll_surface_control;
          background-color: @dzll_surface_control;
          background-image: none;
          color: @dzll_text_primary;
          border-color: @dzll_border;
          box-shadow: none;
        }}

        .dzll-app-root dropdown > button:hover,
        .dzll-app-root dropdown:hover > button,
        .dzll-app-root menubutton > button:hover,
        .dzll-app-root dropdown.dzll-dropdown > button:hover,
        .dzll-app-root dropdown.dzll-dropdown:focus > button,
        .dzll-app-root dropdown.dzll-dropdown:focus-within > button {{
          background: @dzll_control_hover;
          background-color: @dzll_control_hover;
          background-image: none;
          color: @dzll_text_primary;
          border-color: @dzll_hover_border;
          box-shadow: none;
        }}

        .dzll-app-root dropdown > button:active,
        .dzll-app-root dropdown:active > button,
        .dzll-app-root dropdown:checked > button,
        .dzll-app-root menubutton > button:active,
        .dzll-app-root dropdown.dzll-dropdown > button:active,
        .dzll-app-root dropdown.dzll-dropdown > button:checked,
        .dzll-app-root dropdown.dzll-dropdown > button:selected {{
          background: @dzll_control_pressed;
          background-color: @dzll_control_pressed;
          background-image: none;
          color: @dzll_text_primary;
          border-color: @dzll_hover_border;
          box-shadow: none;
        }}

        /* The closed Gtk.DropDown selection is a row inside the button stack. */
        .dzll-app-root dropdown.dzll-dropdown > button stack,
        .dzll-app-root dropdown.dzll-dropdown > button:hover stack,
        .dzll-app-root dropdown.dzll-dropdown > button:active stack,
        .dzll-app-root dropdown.dzll-dropdown > button:checked stack,
        .dzll-app-root dropdown.dzll-dropdown > button:focus stack,
        .dzll-app-root dropdown.dzll-dropdown > button stack > row,
        .dzll-app-root dropdown.dzll-dropdown > button stack > row:hover,
        .dzll-app-root dropdown.dzll-dropdown > button stack > row:active,
        .dzll-app-root dropdown.dzll-dropdown > button stack > row:checked,
        .dzll-app-root dropdown.dzll-dropdown > button stack > row:selected,
        .dzll-app-root dropdown.dzll-dropdown > button stack > row:focus,
        .dzll-app-root dropdown.dzll-dropdown > button stack > row > box,
        .dzll-app-root dropdown.dzll-dropdown > button stack > row:hover > box,
        .dzll-app-root dropdown.dzll-dropdown > button stack > row > box > label,
        .dzll-app-root dropdown.dzll-dropdown > button stack > row:hover > box > label {{
          background: transparent;
          background-color: transparent;
          background-image: none;
          color: @dzll_text_primary;
          border-color: transparent;
          box-shadow: none;
          outline: none;
        }}

        .dzll-app-root popover > contents,
        .required-mods-popover > contents,
        .companion-sound-popover,
        .companion-sound-popover > contents {{
          background: @dzll_surface_content;
          color: @dzll_text_primary;
          border-color: @dzll_border;
          box-shadow: 0 8px 24px @dzll_popover_shadow;
        }}

        .dzll-app-root popover list,
        .dzll-app-root popover listview,
        .dzll-app-root popover viewport,
        .required-mods-popover viewport {{
          background: @dzll_surface_content;
          color: @dzll_text_primary;
        }}

        .dzll-app-root popover row:hover,
        .dzll-app-root popover listitem:hover,
        .dzll-dropdown popover row:hover,
        .dzll-dropdown popover listitem:hover {{
          background: @dzll_control_hover;
          color: @dzll_text_primary;
        }}

        .dzll-app-root popover row:hover label,
        .dzll-app-root popover listitem:hover label,
        .dzll-dropdown popover row:hover label,
        .dzll-dropdown popover listitem:hover label {{
          color: @dzll_text_primary;
        }}

        .dzll-app-root popover row:selected,
        .dzll-app-root popover listitem:selected,
        .dzll-dropdown popover row:selected,
        .dzll-dropdown popover listitem:selected {{
          background: @dzll_selection;
          color: @dzll_text_on_accent;
        }}

        .dzll-app-root popover row:selected label,
        .dzll-app-root popover listitem:selected label,
        .dzll-dropdown popover row:selected label,
        .dzll-dropdown popover listitem:selected label {{
          color: @dzll_text_on_accent;
        }}

        .dzll-app-root popover row:selected:hover,
        .dzll-app-root popover listitem:selected:hover,
        .dzll-dropdown popover row:selected:hover,
        .dzll-dropdown popover listitem:selected:hover {{
          background: @dzll_accent_hover;
          color: @dzll_text_on_accent;
        }}

        .dzll-app-root switch,
        .server-companion-panel switch {{
          background: @dzll_surface_control;
          border-color: @dzll_border;
        }}

        .dzll-app-root switch:checked,
        .server-companion-panel switch:checked {{
          background: @dzll_accent;
          border-color: @dzll_hover_border;
        }}

        .dzll-app-root switch slider,
        .server-companion-panel switch slider {{
          background: @dzll_text_secondary;
          border-color: transparent;
          outline: none;
          box-shadow: none;
          opacity: 1;
        }}

        .dzll-app-root switch:checked slider,
        .server-companion-panel switch:checked slider {{
          background: @dzll_switch_knob_on;
        }}

        .dzll-app-root switch:focus-visible,
        .server-companion-panel switch:focus-visible {{
          outline: 2px solid @dzll_focus;
          outline-offset: 1px;
        }}

        .dzll-app-root switch:disabled,
        .server-companion-panel switch:disabled {{
          background: @dzll_control_disabled;
          color: @dzll_text_disabled;
        }}

        .dzll-app-root switch:disabled slider,
        .server-companion-panel switch:disabled slider {{
          background: @dzll_text_disabled;
          border-color: transparent;
          outline: none;
          box-shadow: none;
        }}

        .dzll-app-root checkbutton,
        .server-companion-panel checkbutton {{
          color: @dzll_text_primary;
        }}

        .dzll-app-root checkbutton check,
        .server-companion-panel checkbutton check {{
          background: @dzll_surface_control;
          color: @dzll_text_on_accent;
          border-color: @dzll_border;
        }}

        .dzll-app-root checkbutton:checked check,
        .server-companion-panel checkbutton:checked check {{
          background: @dzll_accent;
          border-color: @dzll_focus;
        }}

        .dzll-app-root checkbutton:disabled,
        .server-companion-panel checkbutton:disabled {{
          color: @dzll_text_disabled;
        }}

        .dzll-app-root scrollbar,
        .dzll-app-root scrollbar trough,
        .required-mods-popover scrollbar,
        .required-mods-popover scrollbar trough {{
          background: @dzll_surface_content;
          border-color: transparent;
          outline: none;
          box-shadow: none;
        }}

        .dzll-app-root scrollbar.vertical,
        .required-mods-popover scrollbar.vertical,
        .dzll-dropdown popover scrollbar.vertical,
        .server-companion-panel scrollbar.vertical {{
          min-width: 6px;
        }}

        .dzll-app-root scrollbar.horizontal,
        .required-mods-popover scrollbar.horizontal,
        .dzll-dropdown popover scrollbar.horizontal,
        .server-companion-panel scrollbar.horizontal {{
          min-height: 6px;
        }}

        .dzll-app-root scrollbar slider,
        .required-mods-popover scrollbar slider,
        .dzll-dropdown popover scrollbar slider,
        .server-companion-panel scrollbar slider {{
          background: @dzll_surface_control;
          border: 1px solid @dzll_scrollbar_border;
          outline: none;
          box-shadow: none;
        }}

        .dzll-app-root scrollbar.vertical slider,
        .required-mods-popover scrollbar.vertical slider,
        .dzll-dropdown popover scrollbar.vertical slider,
        .server-companion-panel scrollbar.vertical slider {{
          margin-left: 0;
          margin-right: 0;
        }}

        .dzll-app-root scrollbar.horizontal slider,
        .required-mods-popover scrollbar.horizontal slider,
        .dzll-dropdown popover scrollbar.horizontal slider,
        .server-companion-panel scrollbar.horizontal slider {{
          margin-top: 0;
          margin-bottom: 0;
        }}

        .dzll-app-root scrollbar slider:hover,
        .required-mods-popover scrollbar slider:hover,
        .dzll-dropdown popover scrollbar slider:hover,
        .server-companion-panel scrollbar slider:hover {{
          background: @dzll_control_hover;
        }}

        .dzll-app-root scrollbar slider:active,
        .required-mods-popover scrollbar slider:active,
        .dzll-dropdown popover scrollbar slider:active,
        .server-companion-panel scrollbar slider:active {{
          background: @dzll_control_pressed;
        }}

        .dzll-app-root .dzll-browser-surface scrollbar,
        .dzll-app-root .dzll-browser-surface scrollbar trough {{
          background: transparent;
          border-color: transparent;
          outline: none;
          box-shadow: none;
        }}

        .dzll-app-root .dzll-browser-surface scrollbar.vertical {{
          margin-top: 3px;
        }}

        .dzll-platform-ubuntu .dzll-browser-surface scrollbar.vertical > range > trough > slider,
        .dzll-platform-ubuntu .mods-card .mods-list scrollbar.vertical > range > trough > slider {{
          margin-top: 2px;
          margin-bottom: 2px;
        }}

        .dzll-platform-ubuntu .dzll-browser-surface scrollbar.vertical.overlay-indicator.hovering > range > trough > slider,
        .dzll-platform-ubuntu .dzll-browser-surface scrollbar.vertical.overlay-indicator.dragging > range > trough > slider,
        .dzll-platform-ubuntu .dzll-browser-surface scrollbar.vertical > range > trough > slider:hover,
        .dzll-platform-ubuntu .dzll-browser-surface scrollbar.vertical > range > trough > slider:active,
        .dzll-platform-ubuntu .mods-card .mods-list scrollbar.vertical.overlay-indicator.hovering > range > trough > slider,
        .dzll-platform-ubuntu .mods-card .mods-list scrollbar.vertical.overlay-indicator.dragging > range > trough > slider,
        .dzll-platform-ubuntu .mods-card .mods-list scrollbar.vertical > range > trough > slider:hover,
        .dzll-platform-ubuntu .mods-card .mods-list scrollbar.vertical > range > trough > slider:active {{
          margin-top: 1px;
          margin-bottom: 3px;
        }}

        .dzll-app-root .mods-card .mods-list scrollbar,
        .dzll-app-root .mods-card .mods-list scrollbar trough {{
          background: transparent;
          border-color: transparent;
          outline: none;
          box-shadow: none;
        }}

        .dzll-app-root .mods-card .mods-list scrollbar.vertical {{
          margin-top: 3px;
        }}

        .dzll-dropdown popover scrollbar,
        .dzll-dropdown popover scrollbar trough {{
          background: transparent;
          border-color: transparent;
          outline: none;
          box-shadow: none;
        }}

        .dzll-app-root spinner,
        .server-companion-panel spinner {{
          color: @dzll_accent;
        }}

        .dzll-app-root progressbar trough,
        .server-companion-panel progressbar trough {{
          background: @dzll_surface_control;
        }}

        .dzll-app-root progressbar progress,
        .server-companion-panel progressbar progress {{
          background: @dzll_accent;
        }}

        .dzll-app-root scale trough,
        .server-companion-panel scale trough {{
          background: @dzll_surface_control;
        }}

        .dzll-app-root scale highlight,
        .server-companion-panel scale highlight {{
          background: @dzll_accent;
        }}

        .dzll-app-root scale slider,
        .server-companion-panel scale slider {{
          background: @dzll_scale_knob;
          border-color: @dzll_border;
          outline: none;
          box-shadow: none;
          opacity: 1;
        }}

        .server-companion-panel scale.horizontal slider {{
          background: @dzll_scale_knob;
          background-color: @dzll_scale_knob;
          background-image: none;
          border-color: transparent;
          border-image: none;
          box-shadow: none;
          outline: none;
          text-shadow: none;
          opacity: 1;
          filter: none;
        }}

        .dzll-app-root separator,
        .server-companion-panel separator {{
          background: @dzll_divider;
          color: @dzll_divider;
        }}

        tooltip.background,
        tooltip.background > box {{
          background: @dzll_surface_content;
          color: @dzll_text_primary;
          border-color: @dzll_border;
        }}

        /* ---------- List rows (keep transparent) ---------- */
        .dzll-app-root listview row {{ background: transparent; }}
        .dzll-app-root listview row:hover {{ background: transparent; }}
        .dzll-app-root listview row:selected {{ background: transparent; }}
        
        /* ---------- Favorites star ---------- */
        /* ON star stays yellow even when unfocused */
        button.fav-star {{ font-size: 1.8em; padding: 0; }}
        button.fav-star.fav-on,
        button.fav-star.fav-on * {{
          color: @dzll_favourite_on;
        }}
        button.fav-star.fav-on:backdrop,
        button.fav-star.fav-on:backdrop * {{
          color: @dzll_favourite_on;
        }}
        
        /* OFF star stays grey even when unfocused */
        button.fav-star.fav-off,
        button.fav-star.fav-off * {{
          color: @dzll_favourite_off;
        }}
        button.fav-star.fav-off:backdrop,
        button.fav-star.fav-off:backdrop * {{
          color: @dzll_favourite_off;
        }}
        
        /* ---------- Flat buttons (icons etc.) ---------- */
        .dzll-app-root button.flat,
        .dzll-app-root button.flat:hover,
        .dzll-app-root button.flat:active,
        .server-companion-panel button.flat,
        .server-companion-panel button.flat:hover,
        .server-companion-panel button.flat:active {{
          background: transparent;
          box-shadow: none;
          border: 0;
          outline: none;
        }}

        button.monitor-btn,
        button.monitor-btn image,
        button.monitor-btn:backdrop,
        button.monitor-btn:backdrop image {{
          color: @dzll_monitor_idle;
          opacity: 1;
          filter: none;
        }}

        button.flat.monitor-btn.monitor-btn-active,
        button.flat.monitor-btn.monitor-btn-active image,
        button.flat.monitor-btn.monitor-btn-active > image,
        button.flat.monitor-btn.monitor-btn-active:backdrop,
        button.flat.monitor-btn.monitor-btn-active:backdrop image,
        button.flat.monitor-btn.monitor-btn-active:backdrop > image {{
          color: @dzll_monitor_active;
          opacity: 1;
          filter: none;
        }}

        image.monitor-eye-active,
        button.monitor-btn image.monitor-eye-active,
        button.flat.monitor-btn image.monitor-eye-active,
        button.monitor-btn:backdrop image.monitor-eye-active,
        button.flat.monitor-btn:backdrop image.monitor-eye-active {{
          color: @dzll_monitor_active;
          opacity: 1;
          filter: none;
        }}

        .dzll-app-root button.monitor-btn,
        .dzll-app-root button.monitor-btn image {{
          color: @dzll_monitor_idle;
        }}

        .dzll-app-root button.monitor-btn.monitor-btn-active,
        .dzll-app-root button.monitor-btn.monitor-btn-active image,
        .dzll-app-root image.monitor-eye-active {{
          color: @dzll_monitor_active;
        }}

        .dzll-app-root .dzll-column-view button.monitor-btn:backdrop image.monitor-eye-idle {{
          color: @dzll_monitor_idle;
          -gtk-icon-filter: none;
          opacity: 1;
          filter: none;
        }}

        .players-cell {{
          padding: 0 3px;
        }}

        entry.top-search-entry {{
          padding-top: 2px;
          padding-bottom: 2px;
        }}

        entry.top-search-entry.mod-search-entry-active {{
          border-color: @dzll_mod_focus;
        }}

        entry.top-search-entry.mod-search-entry-active:focus,
        entry.top-search-entry.mod-search-entry-active:focus-within {{
          border-color: @dzll_mod_focus;
          outline-color: @dzll_mod_focus;
        }}

        .mod-search-control {{
          background: transparent;
        }}

        button.mod-search-toggle-badge {{
          background: @dzll_surface_control;
          color: @dzll_text_secondary;
          border: 1px solid @dzll_border;
          border-radius: 4px;
          padding: 0;
          font-weight: 700;
          min-height: 0;
          min-width: 18px;
        }}

        button.mod-search-toggle-badge .mod-search-toggle-stack {{
          border: 0;
          padding: 0;
          margin: 0;
        }}

        button.mod-search-toggle-badge .mod-search-toggle-letter {{
          font-size: 0.56em;
          line-height: 0.72;
          padding: 0;
          margin: 0;
        }}

        button.mod-search-toggle-badge:hover {{
          background: @dzll_control_hover;
          color: @dzll_text_primary;
        }}

        button.mod-search-toggle-badge:active {{
          background: @dzll_control_pressed;
        }}

        button.mod-search-toggle-badge.mod-search-toggle-badge-active,
        button.mod-search-toggle-badge:checked {{
          background: @dzll_accent;
          color: @dzll_text_on_accent;
          border-color: @dzll_focus;
        }}

        button.mod-search-toggle-badge.mod-search-toggle-badge-active:hover,
        button.mod-search-toggle-badge:checked:hover {{
          background: @dzll_accent_hover;
          color: @dzll_text_on_accent;
        }}

        button.mod-search-toggle-badge.mod-search-toggle-badge-active:active,
        button.mod-search-toggle-badge:checked:active {{
          background: @dzll_accent_pressed;
        }}

        button.mod-search-toggle-badge:disabled {{
          background: @dzll_control_disabled;
          color: @dzll_text_disabled;
          border-color: @dzll_divider;
        }}

        .mod-search-chip-row {{
          background: transparent;
        }}

        .mod-search-chip {{
          background: @dzll_surface_control;
          border: 1px solid @dzll_border;
          border-radius: 999px;
          padding: 0;
          min-width: 0;
          min-height: 0;
        }}

        .mod-search-chip label {{
          font-size: 0.84em;
        }}

        .mod-search-chip-name {{
          padding: 2px 7px 2px 8px;
        }}

        .mod-search-chip-close {{
          border-left: 1px solid @dzll_divider;
          border-radius: 0 999px 999px 0;
          padding: 2px 3px 2px 5px;
          min-width: 11px;
          min-height: 0;
        }}

        .mod-search-chip-close:hover {{
          background: alpha(@dzll_destructive, 0.76);
        }}

        .mod-search-chip-x {{
          color: @dzll_text_secondary;
          opacity: 0.66;
          font-weight: 600;
        }}

        .mod-search-chip-close:hover .mod-search-chip-x {{
          opacity: 0.95;
        }}

        .mod-search-chip-scroller scrollbar.horizontal,
        .mod-search-chip-scroller scrollbar.horizontal trough {{
          background: transparent;
          border: 0;
          box-shadow: none;
        }}

        .mod-search-chip-scroller scrollbar.horizontal {{
          border-top: 0;
        }}

        .perspective-badge {{
          color: @dzll_text_on_accent;
          border-radius: 3px;
          padding: 0px 3px;
          font-size: 0.72em;
          font-weight: 700;
          line-height: 1.0;
          min-height: 0;
          min-width: 0;
        }}

        .perspective-badge-1pp {{
          background: @dzll_badge_1p;
          color: @dzll_text_on_accent;
        }}

        .perspective-badge-3pp {{
          background: @dzll_badge_3p;
          color: @dzll_text_on_accent;
        }}

        .mod-state-badge {{
          border-radius: 3px;
          padding: 1px 3px;
          font-size: 12px;
          font-weight: 600;
          line-height: 1.0;
          min-width: 16px;
          min-height: 15px;
        }}

        .mod-state-badge-s {{
          background: @dzll_badge_subscribed;
          color: @dzll_text_on_accent;
        }}

        .mod-state-badge-i {{
          background: @dzll_badge_installed;
          color: @dzll_text_on_accent;
        }}

        .companion-flat-menu,
        .companion-flat-menu:hover,
        .companion-flat-menu:active,
        .companion-flat-menu > button,
        .companion-flat-menu > button:hover,
        .companion-flat-menu > button:active,
        .companion-flat-menu-option,
        .companion-flat-menu-option:hover,
        .companion-flat-menu-option:active {{
          background: transparent;
          box-shadow: none;
          border: 0;
          outline: none;
          padding: 4px 6px;
        }}

        .companion-sound-popover {{
          margin: 10px;
        }}

        .required-mods-popover {{
          background: transparent;
          color: @dzll_text_primary;
          border: none;
          box-shadow: none;
          padding: 0;
        }}

        .required-mods-popover > contents {{
          background: @dzll_surface_content;
          color: @dzll_text_primary;
          border: 1px solid @dzll_border;
          box-shadow: 0 8px 24px @dzll_popover_shadow;
        }}

        .required-mods-popover > arrow {{
          background: @dzll_surface_content;
          color: @dzll_surface_content;
          border-color: @dzll_border;
        }}

        .required-mods-popover-content {{
          background: @dzll_surface_content;
          color: @dzll_text_primary;
          padding: 10px 12px;
        }}

        .required-mods-popover-scroller,
        .required-mods-popover-scroller viewport {{
          background: @dzll_surface_content;
        }}

        .required-mods-popover-item {{
          font-size: 0.92em;
          color: @dzll_text_primary;
        }}

        .browser-toast {{
          background: @dzll_surface_control;
          border: 1px solid @dzll_border;
          border-radius: 999px;
          box-shadow: none;
          padding: 5px 10px;
          color: @dzll_text_primary;
          font-weight: 600;
        }}

        button.required-mods-popover-target {{
          background: @dzll_surface_control;
          color: @dzll_text_secondary;
          border: 1px solid @dzll_border;
          border-radius: 999px;
          box-shadow: none;
          outline: none;
          padding: 0 7px;
          min-height: 18px;
          min-width: 0;
          font-size: 0.82em;
        }}

        button.required-mods-popover-target:hover {{
          background: @dzll_control_hover;
          color: @dzll_text_primary;
          border-color: @dzll_hover_border;
        }}

        button.required-mods-popover-target:active {{
          background: @dzll_control_pressed;
        }}

        button.required-mods-popover-target:focus-visible {{
          outline: 2px solid @dzll_focus;
          outline-offset: 1px;
        }}

        button.required-mods-popover-target:disabled {{
          background: @dzll_control_disabled;
          color: @dzll_text_disabled;
          border-color: @dzll_divider;
        }}

        /* ---------- Entry placeholder (dims hint text only) ---------- */
        .dzll-app-root entry placeholder,
        .dzll-app-root searchentry placeholder {{
          color: @dzll_text_muted;
        }}

        .mod-suggestion-panel {{
          background: @dzll_surface_content;
          color: @dzll_text_primary;
          border: 1px solid @dzll_border;
          border-radius: 4px;
          padding: 2px;
        }}

        .mod-suggestion-list {{
          background: transparent;
          border: 0;
        }}

        .mod-suggestion-scroller scrollbar,
        .mod-suggestion-scroller scrollbar trough {{
          background: transparent;
          border: 0;
          box-shadow: none;
        }}

        .mod-suggestion-scroller scrollbar.vertical {{
          border-left: 0;
        }}

        .mod-suggestion-scroller scrollbar.vertical slider {{
          background: @dzll_surface_control;
          border: 1px solid @dzll_scrollbar_border;
          outline: none;
          box-shadow: none;
          border-radius: 999px;
          margin-left: 0;
          margin-right: 0;
          min-width: 6px;
        }}

        /* ---------- Typography ---------- */
        .server-name {{ font-weight: 400; font-size: 1.0em; }}
        .colhdr {{ opacity: 0.80; font-weight: 600; font-size: 0.90em; }}
        .timewarp {{ font-size: 0.85em; opacity: 0.75; }}
        
        /* ---------- Dividers / layout grid ---------- */
        .hr {{ background-color: @dzll_divider; }}
        
        .fav-hdr {{
          border-right: 1px solid @dzll_divider;
          padding-right: 12px;
        }}
        
        .rightblock {{
          border-left: 1px solid @dzll_divider;
          padding-left: 0px;
          margin-left: 0px;
        }}

        /* ---------- ColumnView experiment ---------- */
        columnview.dzll-column-view > header,
        columnview.dzll-column-view > header:hover,
        columnview.dzll-column-view > header.dzll-column-header-flat,
        columnview.dzll-column-view > header.dzll-column-header-flat:hover,
        columnview.dzll-column-view > header.activatable:hover,
        columnview.dzll-column-view > header > button,
        columnview.dzll-column-view > header > button:hover,
        columnview.dzll-column-view > header > button:active,
        columnview.dzll-column-view > header > button:checked,
        columnview.dzll-column-view > header > button:focus,
        columnview.dzll-column-view > header > button:focus-visible,
        columnview.dzll-column-view > header > button.dzll-column-title-flat,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:hover,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:active,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:checked,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:focus,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:focus-visible,
        .dzll-column-view .dzll-column-header-flat,
        .dzll-column-view .dzll-column-header-flat:hover,
        .dzll-column-view .dzll-column-header-flat.activatable:hover,
        .dzll-column-view .dzll-column-title-flat,
        .dzll-column-view .dzll-column-title-flat:hover,
        .dzll-column-view .dzll-column-title-flat:active,
        .dzll-column-view .dzll-column-title-flat:checked,
        .dzll-column-view .dzll-column-title-flat:focus,
        .dzll-column-view .dzll-column-title-flat:focus-visible {{
          background: @dzll_surface_panel;
          background-color: @dzll_surface_panel;
          background-image: none;
          border-radius: 0;
          border-top: 0;
          border-left: 0;
          border-image: none;
          box-shadow: none;
          outline: none;
          text-shadow: none;
          -gtk-icon-shadow: none;
        }}

        columnview.dzll-column-view > header,
        columnview.dzll-column-view > header:hover,
        columnview.dzll-column-view > header.dzll-column-header-flat,
        columnview.dzll-column-view > header.dzll-column-header-flat:hover {{
          border-color: @dzll_divider;
        }}

        columnview.dzll-column-view > header > button,
        columnview.dzll-column-view > header > button:hover,
        columnview.dzll-column-view > header > button:active,
        columnview.dzll-column-view > header > button.dzll-column-title-flat,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:hover,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:active {{
          border-color: @dzll_border;
        }}

        .dzll-platform-ubuntu columnview.dzll-column-view > header > button {{
          min-height: 38px;
        }}

        .dzll-platform-ubuntu columnview.dzll-column-view > header > button:nth-child(-n+7),
        .dzll-platform-ubuntu columnview.dzll-column-view > header > button:nth-child(-n+7):hover,
        .dzll-platform-ubuntu columnview.dzll-column-view > header > button:nth-child(-n+7):active,
        .dzll-platform-ubuntu columnview.dzll-column-view > header > button:nth-child(-n+7):checked,
        .dzll-platform-ubuntu columnview.dzll-column-view > header > button:nth-child(-n+7):focus,
        .dzll-platform-ubuntu columnview.dzll-column-view > header > button:nth-child(-n+7):focus-visible {{
          border-right: 1px solid @dzll_divider;
        }}

        columnview.dzll-column-view > header > button > box.horizontal,
        columnview.dzll-column-view > header > button:hover > box.horizontal,
        columnview.dzll-column-view > header > button:active > box.horizontal,
        columnview.dzll-column-view > header > button:checked > box.horizontal,
        columnview.dzll-column-view > header > button:focus > box.horizontal,
        columnview.dzll-column-view > header > button:focus-visible > box.horizontal,
        .dzll-column-view .dzll-column-title-flat > box.horizontal,
        .dzll-column-view .dzll-column-title-flat:hover > box.horizontal,
        .dzll-column-view .dzll-column-title-flat:active > box.horizontal,
        .dzll-column-view .dzll-column-title-flat:checked > box.horizontal,
        .dzll-column-view .dzll-column-title-flat:focus > box.horizontal,
        .dzll-column-view .dzll-column-title-flat:focus-visible > box.horizontal,
        columnview.dzll-column-view > header > button > box.horizontal > label,
        columnview.dzll-column-view > header > button:hover > box.horizontal > label,
        columnview.dzll-column-view > header > button:active > box.horizontal > label,
        columnview.dzll-column-view > header > button:checked > box.horizontal > label,
        columnview.dzll-column-view > header > button:focus > box.horizontal > label,
        columnview.dzll-column-view > header > button:focus-visible > box.horizontal > label,
        .dzll-column-view .dzll-column-title-flat > box.horizontal > label,
        .dzll-column-view .dzll-column-title-flat:hover > box.horizontal > label,
        .dzll-column-view .dzll-column-title-flat:active > box.horizontal > label,
        .dzll-column-view .dzll-column-title-flat:checked > box.horizontal > label,
        .dzll-column-view .dzll-column-title-flat:focus > box.horizontal > label,
        .dzll-column-view .dzll-column-title-flat:focus-visible > box.horizontal > label {{
          background: transparent;
          background-color: transparent;
          background-image: none;
          border-radius: 0;
          box-shadow: none;
          outline: none;
          text-shadow: none;
          -gtk-icon-shadow: none;
        }}

        columnview.dzll-column-view > header > button > box.horizontal > label,
        columnview.dzll-column-view .dzll-column-header-label {{
          color: @dzll_text_secondary;
          font-weight: 600;
          font-size: 0.90em;
          opacity: 0.82;
          padding: 4px 4px;
        }}

        columnview.dzll-column-view > header > button:nth-child(2) > box.horizontal > label,
        columnview.dzll-column-view .dzll-column-header-name {{
          padding-left: 20px;
        }}

        columnview.dzll-column-view > header > button:nth-child(8),
        columnview.dzll-column-view > header > button:nth-child(9),
        columnview.dzll-column-view .dzll-column-header-action {{
          border-right: 0;
        }}

        columnview.dzll-column-view > header > button:focus,
        columnview.dzll-column-view > header > button:focus-visible,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:focus,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:focus-visible {{
          border-color: @dzll_divider;
          border-top-color: @dzll_divider;
          border-right-color: @dzll_divider;
          border-bottom-color: @dzll_divider;
          border-left-color: @dzll_divider;
          outline-color: transparent;
          box-shadow: none;
        }}

        columnview.dzll-column-view > header.server-list-header-with-top-border,
        columnview.dzll-column-view > header.server-list-header-with-top-border:hover {{
          border-top: 1px solid @dzll_divider;
        }}

        columnview.dzll-column-view > header > button:checked,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:checked {{
          color: @dzll_focus;
          border-bottom-color: @dzll_focus;
        }}

        columnview.dzll-column-view > header > button:focus-visible,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:focus-visible {{
          outline: 2px solid @dzll_focus;
          outline-offset: -2px;
        }}

        .dzll-column-view row,
        .dzll-column-view listitem {{
          padding-top: 4px;
          padding-bottom: 5px;
          border-bottom: 1px solid @dzll_divider;
        }}

        .dzll-platform-ubuntu columnview.dzll-column-view > listview > row {{
          padding-top: 2px;
          padding-bottom: 3px;
        }}

        .dzll-platform-ubuntu columnview.dzll-column-view > listview > row > cell {{
          padding-left: 0;
          padding-right: 0;
        }}

        .dzll-column-view .dzll-column-cell-right-border {{
          border-right: 1px solid @dzll_divider;
        }}

        .dzll-column-view button.dzll-column-fav-button {{
          padding: 0;
          min-height: 0;
        }}

        .dzll-column-view .dzll-column-fav-star {{
          font-size: 1.72em;
          line-height: 1.0;
        }}

        .dzll-column-view label {{
          color: @dzll_text_primary;
        }}

        .dzll-column-view .dim-label {{
          color: @dzll_text_muted;
        }}

        .dzll-column-view .dzll-queue {{
          color: @dzll_queue;
        }}

        .dzll-column-view button.dzll-join-button,
        .dzll-column-view button.dzll-join-button image {{
          color: @dzll_text_primary;
        }}

        .dzll-column-view button.dzll-join-button:hover,
        .dzll-column-view button.dzll-join-button:hover image {{
          color: @dzll_focus;
        }}

        .dzll-column-view button.dzll-join-button:active,
        .dzll-column-view button.dzll-join-button:active image {{
          color: @dzll_accent_hover;
        }}

        .dzll-column-view button.dzll-join-button.dzll-join-blocked,
        .dzll-column-view button.dzll-join-button.dzll-join-blocked image {{
          color: #ff5c5c;
        }}

        .server-companion-panel {{
          min-width: 280px;
        }}

        .server-companion-header,
        .server-companion-content {{
          background: @dzll_surface_panel;
          color: @dzll_text_primary;
        }}

        .server-companion-panel .companion-detail-label {{
          color: @dzll_text_secondary;
        }}

        .server-companion-panel .companion-detail-value {{
          color: @dzll_text_primary;
        }}

        .server-companion-panel .companion-queue {{
          color: @dzll_text_primary;
        }}

        .server-companion-panel .companion-restart-learning-value {{
          color: @dzll_text_primary;
        }}

        .server-companion-panel .companion-alert-info-box {{
          color: @dzll_warning;
        }}

        /* Companion is a persistent second-screen surface; keep its palette in backdrop. */
        .server-companion-panel:backdrop,
        .server-companion-panel:backdrop .server-companion-header,
        .server-companion-panel:backdrop .server-companion-content {{
          background: @dzll_surface_panel;
          color: @dzll_text_primary;
          opacity: 1;
          filter: none;
        }}

        .server-companion-panel:backdrop label {{
          color: @dzll_text_primary;
          opacity: 1;
          filter: none;
        }}

        .server-companion-panel:backdrop .companion-detail-label,
        .server-companion-panel:backdrop .dim-label {{
          color: @dzll_text_secondary;
        }}

        .server-companion-panel:backdrop .companion-queue {{
          color: @dzll_text_primary;
        }}

        .server-companion-panel:backdrop .companion-restart-learning-value {{
          color: @dzll_text_primary;
          opacity: 1;
        }}

        .server-companion-panel:backdrop button,
        .server-companion-panel:backdrop button image {{
          color: @dzll_text_primary;
          opacity: 1;
          filter: none;
        }}

        .server-companion-panel:backdrop button:not(.flat) {{
          background: @dzll_surface_control;
          background-image: none;
          border-color: @dzll_border;
          box-shadow: none;
        }}

        .server-companion-panel:backdrop button.flat,
        .server-companion-panel:backdrop button.flat image {{
          background: transparent;
          color: @dzll_text_secondary;
          box-shadow: none;
        }}

        .server-companion-panel:backdrop button.suggested-action {{
          background: @dzll_accent;
          color: @dzll_text_on_accent;
          border-color: @dzll_hover_border;
        }}

        .server-companion-panel:backdrop switch {{
          background: @dzll_surface_control;
          border-color: @dzll_border;
          opacity: 1;
          filter: none;
        }}

        .server-companion-panel:backdrop switch:checked {{
          background: @dzll_accent;
          border-color: @dzll_hover_border;
        }}

        .server-companion-panel:backdrop switch slider {{
          background: @dzll_text_secondary;
          border-color: transparent;
          outline: none;
          box-shadow: none;
          opacity: 1;
        }}

        .server-companion-panel:backdrop switch:checked slider {{
          background: @dzll_switch_knob_on;
        }}

        .server-companion-panel:backdrop separator {{
          background: @dzll_divider;
          color: @dzll_divider;
          opacity: 1;
        }}

        .server-companion-panel:backdrop scale trough {{
          background: @dzll_surface_control;
          opacity: 1;
        }}

        .server-companion-panel:backdrop scale highlight {{
          background: @dzll_accent;
          opacity: 1;
        }}

        .server-companion-panel:backdrop scale.horizontal slider {{
          background: @dzll_scale_knob;
          background-color: @dzll_scale_knob;
          background-image: none;
          border-color: transparent;
          border-image: none;
          outline: none;
          box-shadow: none;
          text-shadow: none;
          opacity: 1;
          filter: none;
        }}

        .server-companion-panel:backdrop .ping-good {{ color: @dzll_ping_good; opacity: 1; }}
        .server-companion-panel:backdrop .ping-greeny {{ color: @dzll_ping_greeny; opacity: 1; }}
        .server-companion-panel:backdrop .ping-yellow {{ color: @dzll_ping_yellow; opacity: 1; }}
        .server-companion-panel:backdrop .ping-orange {{ color: @dzll_ping_orange; opacity: 1; }}
        .server-companion-panel:backdrop .ping-bad,
        .server-companion-panel:backdrop .ping-offline {{ color: @dzll_ping_bad; opacity: 1; }}

        .server-companion-panel:backdrop button.server-companion-power-on-button,
        .server-companion-panel:backdrop button.server-companion-power-on-button image {{
          background: transparent;
          color: @dzll_restart_learning;
          opacity: 1;
          filter: none;
        }}

        .server-companion-panel:backdrop button.server-companion-power-off-button,
        .server-companion-panel:backdrop button.server-companion-power-off-button image {{
          background: transparent;
          color: @dzll_error;
          opacity: 1;
          filter: none;
        }}

        .server-companion-panel-docked {{
          border-left: 1px solid @dzll_divider;
        }}

        button.server-companion-power-on-button {{
          background: transparent;
          color: @dzll_restart_learning;
          padding: 0;
          min-height: 0;
          min-width: 0;
          border: none;
          box-shadow: none;
        }}

        button.server-companion-power-on-button:hover,
        button.server-companion-power-on-button:active,
        button.server-companion-power-on-button:focus,
        button.server-companion-power-on-button:focus-visible {{
          background: transparent;
          color: @dzll_restart_learning;
          box-shadow: none;
        }}

        button.server-companion-power-on-button image {{
          color: @dzll_restart_learning;
        }}

        button.server-companion-power-off-button {{
          background: transparent;
          color: @dzll_error;
          padding: 0;
          min-height: 0;
          min-width: 0;
          border: none;
          box-shadow: none;
          outline: none;
        }}

        button.server-companion-power-off-button:hover,
        button.server-companion-power-off-button:active,
        button.server-companion-power-off-button:focus,
        button.server-companion-power-off-button:focus-visible {{
          background: transparent;
          color: @dzll_error;
          border: none;
          box-shadow: none;
          outline: none;
        }}

        button.server-companion-power-off-button .server-companion-power-off-icon {{
          color: @dzll_error;
        }}

        .server-companion-panel button.server-companion-power-on-button,
        .server-companion-panel button.server-companion-power-on-button image {{
          color: @dzll_restart_learning;
        }}

        .dzll-app-root button.server-companion-power-off-button,
        .dzll-app-root button.server-companion-power-off-button image {{
          color: @dzll_error;
        }}

        .companion-restart-learning {{
          padding: 2px 0 0 0;
        }}

        .companion-restart-learning-value {{
          font-weight: 600;
          opacity: 0.92;
        }}

        .cell {{
          padding: 0 5px;
          border-left: 1px solid @dzll_divider;
        }}
        .cell-first {{ border-left: none; }}
        .cell-noborder-left {{ border-left: none; }}
        
        /* ---------- Sidebar sizing ---------- */
        .sidebar-frame {{
          min-width: {SIDEBAR_WIDTH}px;
        }}

        .dzll-app-root entry.sidebar-compact-entry {{
          padding-top: 2px;
          padding-bottom: 2px;
          padding-left: 6px;
          padding-right: 8px;
          min-height: 0;
        }}

        .dzll-app-root button.sidebar-mini-toggle,
        .dzll-app-root button.sidebar-mini-toggle:hover,
        .dzll-app-root button.sidebar-mini-toggle:active,
        .dzll-app-root button.sidebar-mini-toggle:checked {{
          background: transparent;
          border: 0;
          box-shadow: none;
          outline: none;
          padding: 0;
          min-height: 0;
          min-width: 0;
        }}

        .dzll-app-root box.sidebar-mini-switch {{
          background: @dzll_surface_control;
          border: 1px solid @dzll_border;
          border-radius: 999px;
          padding: 2px;
          min-height: 0;
          min-width: 0;
        }}

        .dzll-app-root box.sidebar-mini-switch:hover {{
          background: @dzll_control_hover;
        }}

        .dzll-app-root box.sidebar-mini-switch.sidebar-mini-switch-on {{
          background: @dzll_accent;
          border-color: @dzll_focus;
        }}

        .dzll-app-root box.sidebar-mini-switch.sidebar-mini-switch-on:hover {{
          background: @dzll_accent_hover;
        }}

        .dzll-app-root box.sidebar-mini-switch-knob {{
          background: @dzll_text_secondary;
          border-radius: 999px;
          min-height: 0;
          min-width: 0;
        }}

        .dzll-app-root box.sidebar-mini-switch.sidebar-mini-switch-on box.sidebar-mini-switch-knob {{
          background: @dzll_text_on_accent;
        }}
        
        /* ---------- Sidebar disclaimer ---------- */
        .dzll-app-root .disclaimer,
        .dzll-app-root .disclaimer:backdrop {{
          color: @dzll_text_disclaimer;
          font-style: italic;
          font-size: 0.88em;
        }}
        
        /* ---------- Startup dimmer + band ---------- */
        .startup-dim {{
          background: @dzll_startup_overlay;
        }}
        .startup-band {{
          background: @dzll_startup_band;
          border-top: 1px solid @dzll_divider;
          border-bottom: 1px solid @dzll_divider;
        }}
        .startup-label {{
          color: @dzll_text_primary;
          font-weight: 900;
          font-size: 2.2em;
        }}

        .startup-spinner {{
          min-width: 30px;
          min-height: 30px;
        }}
        
        /* ---------- Ping colors ---------- */
        .ping-good    {{ color: @dzll_ping_good; }}
        .ping-greeny  {{ color: @dzll_ping_greeny; }}
        .ping-yellow  {{ color: @dzll_ping_yellow; }}
        .ping-orange  {{ color: @dzll_ping_orange; }}
        .ping-bad     {{ color: @dzll_ping_bad; }}
        .ping-offline {{ color: @dzll_ping_bad; }}
        
        .ping-good:backdrop    {{ color: @dzll_ping_good; }}
        .ping-greeny:backdrop  {{ color: @dzll_ping_greeny; }}
        .ping-yellow:backdrop  {{ color: @dzll_ping_yellow; }}
        .ping-orange:backdrop  {{ color: @dzll_ping_orange; }}
        .ping-bad:backdrop     {{ color: @dzll_ping_bad; }}
        .ping-offline:backdrop {{ color: @dzll_ping_bad; }}

        .dzll-app-root .ping-good,
        .server-companion-panel .ping-good {{ color: @dzll_ping_good; }}
        .dzll-app-root .ping-greeny,
        .server-companion-panel .ping-greeny {{ color: @dzll_ping_greeny; }}
        .dzll-app-root .ping-yellow,
        .server-companion-panel .ping-yellow {{ color: @dzll_ping_yellow; }}
        .dzll-app-root .ping-orange,
        .server-companion-panel .ping-orange {{ color: @dzll_ping_orange; }}
        .dzll-app-root .ping-bad,
        .dzll-app-root .ping-offline,
        .server-companion-panel .ping-bad,
        .server-companion-panel .ping-offline {{ color: @dzll_ping_bad; }}

        .server-companion-panel .companion-restart-confidence-value.ping-good {{
          color: @dzll_restart_confidence_high;
        }}
        .server-companion-panel .companion-restart-confidence-value.ping-greeny {{
          color: @dzll_restart_confidence_learning;
        }}
        .server-companion-panel:backdrop .companion-restart-confidence-value.ping-good {{
          color: @dzll_restart_confidence_high;
          opacity: 1;
        }}
        .server-companion-panel:backdrop .companion-restart-confidence-value.ping-greeny {{
          color: @dzll_restart_confidence_learning;
          opacity: 1;
        }}
        
        /* ---------- Settings scrim / panel ---------- */
        .settings-scrim {{
          background: @dzll_scrim;
        }}
        
        .settings-panel {{
          background: alpha(@dzll_surface_settings, 0.97);
          color: @dzll_text_primary;
          border: 1px solid @dzll_border;
          border-radius: 10px;
        }}

        .settings-panel .settings-content,
        .settings-panel .settings-content scrolledwindow,
        .settings-panel .settings-content viewport {{
          background: @dzll_surface_content;
          color: @dzll_text_primary;
        }}

        .settings-panel label {{
          color: @dzll_text_primary;
        }}

        .settings-panel .dim-label,
        .settings-panel .dimmed-entry {{
          color: @dzll_text_muted;
        }}
        
        .settings-section-title {{
          font-weight: 700;
          font-size: 1.05em;
        }}

        .dzll-app-root .settings-warning-label {{
          color: @dzll_warning;
        }}
        
        /* Settings nav: match panel background + remove right border line */
        .settings-nav {{
          background: alpha(@dzll_surface_settings, 0.97);
          color: @dzll_text_primary;
          border-right: 0;
          border: 0;
          box-shadow: none;
          outline: none;
        }}
        .settings-nav list,
        .settings-nav listview,
        .settings-nav scrolledwindow,
        .settings-nav viewport {{
          background: alpha(@dzll_surface_settings, 0.97);
          color: @dzll_text_primary;
          border-right: 0;
          border: 0;
          box-shadow: none;
          outline: none;
        }}
        /* Settings nav row background */
        .settings-nav row {{
          background: alpha(@dzll_surface_settings, 0.97);
          color: @dzll_text_secondary;
        }}
        .settings-nav row:hover {{
          background: @dzll_control_hover;
          border-radius: 7px;
        }}
        .settings-nav row:hover > * {{
          background: transparent;
          border-radius: 7px;
        }}
        .settings-nav row:selected {{
          background: @dzll_accent;
          color: @dzll_text_on_accent;
          border-radius: 7px;
        }}
        .settings-nav row:selected:hover,
        .settings-nav row:selected:backdrop {{
          background: @dzll_accent;
          color: @dzll_text_on_accent;
          border-radius: 7px;
        }}
        .settings-nav row:selected > * {{
          background: transparent;
          border-radius: 7px;
        }}
        .settings-nav row:selected label {{
          color: @dzll_text_on_accent;
        }}
        
        /* ---------- Update card ---------- */
        .update-card {{
          background: alpha(@dzll_surface_settings, 0.97);
          color: @dzll_text_primary;
          border: 1px solid @dzll_border;
          border-radius: 10px;
          padding: 50px 20px;
        }}
        .update-title {{
          color: @dzll_text_primary;
          font-weight: 700;
          font-size: 1.05em;
        }}

        .restart-learning-notice-card.restart-notice-reset .update-title {{
          color: @dzll_restart_learning;
        }}

        .restart-learning-notice-card.restart-notice-recovery .update-title {{
          color: @dzll_warning;
        }}

        .restart-learning-notice-card.restart-notice-error .update-title {{
          color: @dzll_error;
        }}

        .restart-learning-notice-card .dim-label {{
          color: @dzll_text_muted;
        }}
        .update-subtitle {{
          color: @dzll_text_secondary;
          opacity: 1;
        }}
        
        .issues-emoji {{ font-size: 24px; }}
        
        /* ---------- SteamCMD auth overlay ---------- */
        .steamcmd-auth-card {{
          background: alpha(@dzll_surface_settings, 0.97);
          color: @dzll_text_primary;
          border: 1px solid @dzll_border;
          border-radius: 14px;
          padding: 80px;
        }}
        .steamcmd-heading {{
          font-size: 18px;
          font-weight: 700;
        }}
        .steamcmd-log {{
          font-size: 12px;
          color: @dzll_steamcmd_log;
        }}
        .steamcmd-hr {{
          margin-top: 6px;
          margin-bottom: 10px;
          opacity: 0.9;
        }}
        
        /* ---------- Warning / confirm overlay ---------- */
        .warning-card {{
          background: alpha(@dzll_surface_settings, 0.97);
          color: @dzll_text_primary;
          border-radius: 8px;
          border: 1px solid @dzll_border;
          font-size: 14px;
          padding: 40px;
        }}
        .dzll-app-root .warning-title {{
          color: @dzll_warning;
          font-weight: 800;
          font-size: 28px;
        }}
        .dzll-app-root .warning-icon {{
          color: @dzll_warning;
          font-size: 48px;
        }}
        .warning-btn {{
          min-width: 160px;
        }}
        
        /* Keep progressbar visible when unfocused */
        progressbar:backdrop {{
          opacity: 1;
          filter: none;
        }}
        
        /* ---------- Mods overlay ---------- */
        .mods-card {{
          background: @dzll_surface_settings;
          color: @dzll_text_primary;
          border: 1px solid @dzll_border;
          border-radius: 4px;
          padding: 56px;
        }}

        .mods-card .mods-header,
        .mods-card .mods-column-header {{
          background: @dzll_surface_settings;
          color: @dzll_text_primary;
        }}

        .mods-card .mods-search {{
          background: @dzll_surface_content;
          color: @dzll_text_primary;
          border-color: @dzll_border;
        }}

        .mods-card .mods-name {{
          color: @dzll_text_primary;
        }}

        .mods-card .dim-label {{
          color: @dzll_text_muted;
        }}

        .mods-card .mods-row .mods-metadata {{
          color: @dzll_mod_metadata;
        }}

        .mods-card .mods-row:disabled .mods-metadata,
        .mods-card .mods-row .mods-metadata:disabled {{
          color: @dzll_text_disabled;
        }}

        .mods-card .mods-operation-status {{
          color: @dzll_text_secondary;
        }}

        .warning-card .confirmation-title {{
          color: @dzll_text_primary;
        }}

        .warning-card .confirmation-body {{
          color: @dzll_text_secondary;
        }}

        .mods-card .steam-status-pill {{
          border: 1px solid @dzll_border;
          border-radius: 999px;
          padding: 6px 8px;
          background: @dzll_surface_control;
        }}

        .mods-card .steam-status-dot {{
          font-size: 0.85em;
        }}

        .mods-card .steam-status-text {{
          font-size: 0.82em;
          font-weight: 600;
          color: @dzll_text_secondary;
        }}

        .mods-card .steam-status-online {{
          color: @dzll_mod_status_online;
        }}

        .mods-card .steam-status-offline {{
          color: @dzll_mod_status_offline;
        }}

        .mods-card .steam-status-checking {{
          color: @dzll_mod_status_checking;
        }}

        .mods-card .steam-status-issue {{
          color: @dzll_mod_status_issue;
        }}

        .mods-card button.mods-danger-action {{
          background: alpha(@dzll_destructive, 0.78);
          color: @dzll_text_on_accent;
          border: 1px solid alpha(@dzll_destructive_border, 0.62);
        }}

        .mods-card button.mods-danger-action:hover {{
          background: alpha(@dzll_destructive_hover, 0.88);
          border-color: @dzll_destructive_border;
        }}

        .mods-card button.mods-danger-action:active {{
          background: @dzll_destructive_pressed;
          border-color: @dzll_destructive_border;
        }}

        .mods-card button.mods-danger-action:disabled {{
          opacity: 0.55;
        }}

        .mods-card button.mods-stop-action {{
          background: alpha(@dzll_stop, 0.18);
          color: @dzll_text_primary;
          border: 1px solid alpha(@dzll_stop, 0.72);
        }}

        .mods-card button.mods-stop-action:hover {{
          background: alpha(@dzll_stop, 0.28);
          border-color: @dzll_link_hover;
        }}

        .mods-card button.mods-stop-action:active {{
          background: alpha(@dzll_stop, 0.36);
          border-color: @dzll_link_hover;
        }}

        .mods-card button:disabled,
        .mods-card button:disabled:hover,
        .mods-card button:disabled:active,
        .mods-card button.mods-danger-action:disabled,
        .mods-card button.mods-danger-action:disabled:hover,
        .mods-card button.mods-stop-action:disabled,
        .mods-card button.mods-stop-action:disabled:hover {{
          background: @dzll_control_disabled;
          background-image: none;
          color: @dzll_text_disabled;
          border-color: @dzll_divider;
          box-shadow: none;
          opacity: 1;
        }}

        .mods-card button:disabled label,
        .mods-card button:disabled image {{
          color: @dzll_text_disabled;
          opacity: 1;
        }}

        .mods-card .mods-clear-selection-link {{
          color: @dzll_link;
          font-size: 0.9em;
          font-weight: 600;
        }}

        .mods-card .mods-clear-selection-link:hover {{
          color: @dzll_link_hover;
          text-decoration: underline;
        }}

        .mods-card .mods-column-separator {{
          background: @dzll_divider;
        }}
        
        .dzll-app-root .mods-card button.flat.mod-workshop-link-btn,
        .dzll-app-root .mods-card button.flat.mod-workshop-link-btn image {{
          background: transparent;
          background-image: none;
          color: @dzll_link;
        }}

        .dzll-app-root .mods-card button.flat.mod-workshop-link-btn:hover,
        .dzll-app-root .mods-card button.flat.mod-workshop-link-btn:hover image,
        .dzll-app-root .mods-card button.flat.mod-workshop-link-btn:active,
        .dzll-app-root .mods-card button.flat.mod-workshop-link-btn:active image {{
          background: transparent;
          background-image: none;
          color: @dzll_link_hover;
        }}

        .dzll-app-root .mods-card button.flat.mod-workshop-link-btn:backdrop,
        .dzll-app-root .mods-card button.flat.mod-workshop-link-btn:backdrop image {{
          background: transparent;
          background-image: none;
          color: @dzll_link;
          -gtk-icon-filter: none;
          opacity: 1;
          filter: none;
        }}

        .dzll-app-root .mods-card button.flat.mod-workshop-link-btn:disabled,
        .dzll-app-root .mods-card button.flat.mod-workshop-link-btn:disabled image {{
          background: transparent;
          background-image: none;
          color: @dzll_text_disabled;
          opacity: 1;
        }}

        .mods-empty-state {{
          color: @dzll_text_muted;
          font-size: 15px;
          font-weight: 500;
        }}
        
        /* Make the mods list area inherit the card background */
        .mods-card scrolledwindow,
        .mods-card viewport,
        .mods-card list,
        .mods-card listview {{
          background: transparent;
        }}

        .mods-card .mods-list,
        .mods-card .mods-list viewport,
        .mods-card .mods-list list,
        .mods-card .mods-list listview {{
          background: @dzll_surface_content;
          color: @dzll_text_primary;
        }}
        
        /* Force each row background to match the card */
        .mods-card row,
        .mods-card row > * {{
          background: @dzll_surface_content;
          color: @dzll_text_primary;
        }}

        .dzll-app-root .mods-card row.mods-row:hover,
        .dzll-app-root .mods-card row.mods-row:hover > * {{
          background: @dzll_surface_content;
        }}
        
        /* Keep the separator line visible */
        .mods-card row separator {{
          background: @dzll_divider;
        }}
        
        .mods-card .mods-list {{
          background: @dzll_surface_content;
          border: 1px solid @dzll_border;
          border-radius: 4px;
        }}
        
        /* Ensure inner viewport doesn't paint over the rounded corners */
        .mods-card .mods-list viewport,
        .mods-card .mods-list list,
        .mods-card .mods-list listview {{
          background: @dzll_surface_content;
        }}
        """.encode("utf-8")

    return css
