#!/usr/bin/env python3
# styles.py
#
# Canonical UI CSS for DZLL.
# Pure extraction from main.py (zero behavior change).

from __future__ import annotations


def get_app_css(
    DIVIDER_COLOR: str,
    SIDEBAR_WIDTH: int,
    DISCLAIMER_COLOR: str,
) -> bytes:
    css = f"""
        /* ---------- List rows (keep transparent) ---------- */
        listview row {{ background: transparent; }}
        listview row:hover {{ background: transparent; }}
        listview row:selected {{ background: transparent; }}
        
        /* ---------- Favorites star ---------- */
        /* ON star stays yellow even when unfocused */
        button.fav-star {{ font-size: 1.8em; padding: 0; }}
        button.fav-star.fav-on,
        button.fav-star.fav-on * {{
          color: #f5c542;
        }}
        button.fav-star.fav-on:backdrop,
        button.fav-star.fav-on:backdrop * {{
          color: #f5c542;
        }}
        
        /* OFF star stays grey even when unfocused */
        button.fav-star.fav-off,
        button.fav-star.fav-off * {{
          color: #7a7a7a;
        }}
        button.fav-star.fav-off:backdrop,
        button.fav-star.fav-off:backdrop * {{
          color: #7a7a7a;
        }}
        
        /* ---------- Flat buttons (icons etc.) ---------- */
        button.flat,
        button.flat:hover,
        button.flat:active {{
          background: transparent;
          box-shadow: none;
          border: 0;
          outline: none;
        }}

        button.monitor-btn,
        button.monitor-btn image,
        button.monitor-btn:backdrop,
        button.monitor-btn:backdrop image {{
          color: #4aa3ff;
        }}

        button.flat.monitor-btn.monitor-btn-active,
        button.flat.monitor-btn.monitor-btn-active image,
        button.flat.monitor-btn.monitor-btn-active > image,
        button.flat.monitor-btn.monitor-btn-active:backdrop,
        button.flat.monitor-btn.monitor-btn-active:backdrop image,
        button.flat.monitor-btn.monitor-btn-active:backdrop > image {{
          color: #ff4d4d;
        }}

        image.monitor-eye-active,
        button.monitor-btn image.monitor-eye-active,
        button.flat.monitor-btn image.monitor-eye-active,
        button.monitor-btn:backdrop image.monitor-eye-active,
        button.flat.monitor-btn:backdrop image.monitor-eye-active {{
          color: #ff4d4d;
        }}

        .players-cell {{
          padding: 0 3px;
        }}

        entry.top-search-entry {{
          padding-top: 2px;
          padding-bottom: 2px;
        }}

        entry.top-search-entry.mod-search-entry-active {{
          border-color: #5f9295;
        }}

        entry.top-search-entry.mod-search-entry-active:focus,
        entry.top-search-entry.mod-search-entry-active:focus-within {{
          border-color: #79aeb0;
        }}

        .mod-search-control {{
          background: transparent;
        }}

        button.mod-search-toggle-badge {{
          background: #2f3438;
          color: #b6bec6;
          border: 1px solid {DIVIDER_COLOR};
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
          background: #3a4045;
          color: #d7dde3;
        }}

        button.mod-search-toggle-badge.mod-search-toggle-badge-active,
        button.mod-search-toggle-badge:checked {{
          background: #0d686c;
          color: #ffffff;
          border-color: #79aeb0;
        }}

        button.mod-search-toggle-badge.mod-search-toggle-badge-active:hover,
        button.mod-search-toggle-badge:checked:hover {{
          background: #17a2a5;
          color: #ffffff;
        }}

        .mod-search-chip-row {{
          background: transparent;
        }}

        .mod-search-chip {{
          background: rgba(255, 255, 255, 0.055);
          border: 1px solid {DIVIDER_COLOR};
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
          border-left: 1px solid rgba(255, 255, 255, 0.16);
          border-radius: 0 999px 999px 0;
          padding: 2px 3px 2px 5px;
          min-width: 11px;
          min-height: 0;
        }}

        .mod-search-chip-close:hover {{
          background: rgba(120, 50, 55, 0.65);
        }}

        .mod-search-chip-x {{
          color: #d8d8d8;
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
          color: #ffffff;
          border-radius: 3px;
          padding: 0px 3px;
          font-size: 0.72em;
          font-weight: 700;
          line-height: 1.0;
          min-height: 0;
          min-width: 0;
        }}

        .perspective-badge-1pp {{
          background: #5aa832;
          color: #ffffff;
        }}

        .perspective-badge-3pp {{
          background: #1f7ad6;
          color: #ffffff;
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
          background: #7b3ff2;
          color: #ffffff;
        }}

        .mod-state-badge-i {{
          background: #d96b18;
          color: #ffffff;
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
          padding: 0;
        }}

        .required-mods-popover-content {{
          padding: 10px 12px;
        }}

        .required-mods-popover-scroller,
        .required-mods-popover-scroller viewport {{
          background: transparent;
        }}

        .required-mods-popover-item {{
          font-size: 0.92em;
          color: @theme_text_color;
        }}

        .browser-toast {{
          background: #2f3438;
          border: 1px solid {DIVIDER_COLOR};
          border-radius: 999px;
          box-shadow: none;
          padding: 5px 10px;
          color: @theme_text_color;
          font-weight: 600;
        }}

        button.required-mods-popover-target {{
          background: rgba(255, 255, 255, 0.055);
          color: alpha(@theme_text_color, 0.82);
          border: 1px solid {DIVIDER_COLOR};
          border-radius: 999px;
          box-shadow: none;
          outline: none;
          padding: 0 7px;
          min-height: 18px;
          min-width: 0;
          font-size: 0.82em;
        }}

        button.required-mods-popover-target:hover {{
          background: rgba(255, 255, 255, 0.095);
          color: @theme_text_color;
          border-color: alpha(@theme_text_color, 0.28);
        }}

        /* ---------- Entry placeholder (dims hint text only) ---------- */
        entry placeholder {{
          color: alpha(@theme_text_color, 0.45);
        }}

        .mod-suggestion-panel {{
          background: @theme_base_color;
          border: 1px solid {DIVIDER_COLOR};
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
          background: alpha(@theme_text_color, 0.24);
          border: 0;
          border-radius: 999px;
          margin-left: 2px;
          margin-right: 2px;
          min-width: 8px;
        }}

        /* ---------- Typography ---------- */
        .server-name {{ font-weight: 400; font-size: 1.0em; }}
        .colhdr {{ opacity: 0.80; font-weight: 600; font-size: 0.90em; }}
        .timewarp {{ font-size: 0.85em; opacity: 0.75; }}
        
        /* ---------- Dividers / layout grid ---------- */
        .hr {{ background-color: {DIVIDER_COLOR}; }}
        
        .fav-hdr {{
          border-right: 1px solid {DIVIDER_COLOR};
          padding-right: 12px;
        }}
        
        .rightblock {{
          border-left: 1px solid {DIVIDER_COLOR};
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
          background: #202326;
          background-color: #202326;
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

        columnview.dzll-column-view > header:hover,
        columnview.dzll-column-view > header > button:hover,
        columnview.dzll-column-view > header > button:active,
        columnview.dzll-column-view > header > button:focus,
        columnview.dzll-column-view > header > button:focus-visible,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:hover,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:active,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:focus,
        columnview.dzll-column-view > header > button.dzll-column-title-flat:focus-visible {{
          border-color: {DIVIDER_COLOR};
          border-top-color: {DIVIDER_COLOR};
          border-right-color: {DIVIDER_COLOR};
          border-bottom-color: {DIVIDER_COLOR};
          border-left-color: {DIVIDER_COLOR};
          outline-color: transparent;
          box-shadow: none;
        }}

        columnview.dzll-column-view > header.server-list-header-with-top-border,
        columnview.dzll-column-view > header.server-list-header-with-top-border:hover {{
          border-top: 1px solid {DIVIDER_COLOR};
        }}

        .dzll-column-view row,
        .dzll-column-view listitem {{
          padding-top: 4px;
          padding-bottom: 5px;
          border-bottom: 1px solid {DIVIDER_COLOR};
        }}

        .dzll-column-view .dzll-column-cell-right-border {{
          border-right: 1px solid {DIVIDER_COLOR};
        }}

        .dzll-column-view button.dzll-column-fav-button {{
          padding: 0;
          min-height: 0;
        }}

        .dzll-column-view .dzll-column-fav-star {{
          font-size: 1.72em;
          line-height: 1.0;
        }}

        .server-companion-panel {{
          min-width: 280px;
          max-width: 280px;
        }}

        .server-companion-panel-docked {{
          border-left: 1px solid {DIVIDER_COLOR};
        }}

        button.server-companion-power-on-button {{
          background: transparent;
          color: #bfe84a;
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
          color: #bfe84a;
          box-shadow: none;
        }}

        button.server-companion-power-on-button image {{
          color: #bfe84a;
        }}

        button.server-companion-power-off-button {{
          background: transparent;
          color: #ff3333;
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
          color: #ff3333;
          border: none;
          box-shadow: none;
          outline: none;
        }}

        button.server-companion-power-off-button .server-companion-power-off-icon {{
          color: #ff3333;
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
          border-left: 1px solid {DIVIDER_COLOR};
        }}
        .cell-first {{ border-left: none; }}
        .cell-noborder-left {{ border-left: none; }}
        
        /* ---------- Sidebar sizing ---------- */
        .sidebar-frame {{
          min-width: {SIDEBAR_WIDTH}px;
          max-width: {SIDEBAR_WIDTH}px;
        }}
        
        /* ---------- Sidebar disclaimer ---------- */
        .disclaimer {{
          color: {DISCLAIMER_COLOR};
          font-style: italic;
          font-size: 0.88em;
        }}
        
        /* ---------- Startup dimmer + band ---------- */
        .startup-dim {{
          background: rgba(0,0,0,0.55);
        }}
        .startup-band {{
          background: rgba(0,0,0,0.75);
          border-top: 1px solid {DIVIDER_COLOR};
          border-bottom: 1px solid {DIVIDER_COLOR};
        }}
        .startup-label {{
          color: #e6e6e6;
          font-weight: 900;
          font-size: 2.2em;
        }}
        
        /* ---------- Ping colors ---------- */
        .ping-good    {{ color: #37c871; }}
        .ping-greeny  {{ color: #9ad43a; }}
        .ping-yellow  {{ color: #e3c84a; }}
        .ping-orange  {{ color: #e19a3a; }}
        .ping-bad     {{ color: #e04b4b; }}
        .ping-offline {{ color: #e04b4b; }}
        
        .ping-good:backdrop    {{ color: #37c871; }}
        .ping-greeny:backdrop  {{ color: #9ad43a; }}
        .ping-yellow:backdrop  {{ color: #e3c84a; }}
        .ping-orange:backdrop  {{ color: #e19a3a; }}
        .ping-bad:backdrop     {{ color: #e04b4b; }}
        .ping-offline:backdrop {{ color: #e04b4b; }}
        
        /* ---------- Settings scrim / panel ---------- */
        .settings-scrim {{
          background: rgba(0,0,0,0.35);
        }}
        
        .settings-panel {{
          background: rgba(24,24,24,0.97);
          border: 1px solid {DIVIDER_COLOR};
          border-radius: 10px;
        }}
        
        .settings-section-title {{
          font-weight: 700;
          font-size: 1.05em;
        }}

        .settings-warning-label {{
          color: #d89452;
        }}
        
        /* Settings nav: match panel background + remove right border line */
        .settings-nav {{
          background: rgba(24,24,24,0.97);
          border-right: 0;
          border: 0;
          box-shadow: none;
          outline: none;
        }}
        .settings-nav list,
        .settings-nav listview,
        .settings-nav scrolledwindow,
        .settings-nav viewport {{
          background: rgba(24,24,24,0.97);
          border-right: 0;
          border: 0;
          box-shadow: none;
          outline: none;
        }}
        /* Settings nav row background */
        .settings-nav row {{
          background: rgba(24,24,24,0.97);
        }}
        .settings-nav row:hover {{
          background: rgba(24,24,24,0.97);
        }}
        .settings-nav row:selected {{
          background: rgba(115,115,115,1);
        }}
        
        /* ---------- Update card ---------- */
        .update-card {{
          background: rgba(24,24,24,0.97);
          border: 1px solid {DIVIDER_COLOR};
          border-radius: 10px;
          padding: 50px 20px;
        }}
        .update-title {{
          font-weight: 700;
          font-size: 1.05em;
        }}
        .update-subtitle {{
          opacity: 0.85;
        }}
        
        .issues-emoji {{ font-size: 24px; }}
        
        /* ---------- SteamCMD auth overlay ---------- */
        .steamcmd-auth-card {{
          background: rgba(24, 24, 24, 0.97);
          border: 1px solid rgba(255, 255, 255, 0.12);
          border-radius: 14px;
          padding: 80px;
        }}
        .steamcmd-heading {{
          font-size: 18px;
          font-weight: 700;
        }}
        .steamcmd-log {{
          font-size: 12px;
          color: #33ff66;
        }}
        .steamcmd-hr {{
          margin-top: 6px;
          margin-bottom: 10px;
          opacity: 0.9;
        }}
        
        /* ---------- Warning / confirm overlay ---------- */
        .warning-card {{
          background: rgba(24,24,24,0.97);
          border-radius: 8px;
          border: 1px solid rgba(255,255,255,0.10);
          font-size: 14px;
          padding: 40px;
        }}
        .warning-title {{
          font-weight: 800;
          font-size: 28px;
        }}
        .warning-icon {{
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
          background: #181818;
          border: 1px solid rgba(255,255,255,0.10);
          border-radius: 4px;
          padding: 56px;
        }}

        .mods-card .steam-status-pill {{
          border: 1px solid rgba(255,255,255,0.18);
          border-radius: 999px;
          padding: 6px 8px;
          background: rgba(255,255,255,0.035);
        }}

        .mods-card .steam-status-dot {{
          font-size: 0.85em;
        }}

        .mods-card .steam-status-text {{
          font-size: 0.82em;
          font-weight: 600;
          color: rgba(255,255,255,0.82);
        }}

        .mods-card .steam-status-online {{
          color: #4fbf67;
        }}

        .mods-card .steam-status-offline {{
          color: #d85b5b;
        }}

        .mods-card .steam-status-checking {{
          color: #d6a94f;
        }}

        .mods-card .steam-status-issue {{
          color: #d9784f;
        }}

        .mods-card button.mods-danger-action {{
          background: rgba(135, 38, 38, 0.52);
          color: #ffffff;
          border: 1px solid rgba(255, 120, 120, 0.46);
        }}

        .mods-card button.mods-danger-action:hover {{
          background: rgba(165, 48, 48, 0.68);
          border-color: rgba(255, 145, 145, 0.68);
        }}

        .mods-card button.mods-danger-action:active {{
          background: rgba(185, 55, 55, 0.78);
          border-color: rgba(255, 160, 160, 0.78);
        }}

        .mods-card button.mods-danger-action:disabled {{
          opacity: 0.55;
        }}

        .mods-card button.mods-stop-action {{
          background: rgba(70, 140, 255, 0.18);
          color: #dfeaff;
          border: 1px solid rgba(110, 170, 255, 0.50);
        }}

        .mods-card button.mods-stop-action:hover {{
          background: rgba(70, 140, 255, 0.28);
          border-color: rgba(140, 190, 255, 0.70);
        }}

        .mods-card button.mods-stop-action:active {{
          background: rgba(70, 140, 255, 0.36);
          border-color: rgba(140, 190, 255, 0.78);
        }}

        .mods-card .mods-clear-selection-link {{
          color: #6ab0ff;
          font-size: 0.9em;
          font-weight: 600;
        }}

        .mods-card .mods-clear-selection-link:hover {{
          color: #9dccff;
          text-decoration: underline;
        }}

        .mods-card .mods-column-separator {{
          background: rgba(255,255,255,0.10);
        }}
        
        .mod-workshop-link-btn image {{ color: #2f9bff; }}
        .mod-workshop-link-btn:backdrop image {{ color: #2f9bff; }}

        .mods-empty-state {{
          color: rgba(255,255,255,0.70);
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
        
        /* Force each row background to match the card */
        .mods-card row,
        .mods-card row > * {{
          background: #181818;
        }}
        
        /* Keep the separator line visible */
        .mods-card row separator {{
          background: rgba(255,255,255,0.15);
        }}
        
        .mods-card .mods-list {{
          background: #181818;
          border: 1px solid rgba(255,255,255,0.20);
          border-radius: 4px;
        }}
        
        /* Ensure inner viewport doesn't paint over the rounded corners */
        .mods-card .mods-list viewport,
        .mods-card .mods-list list,
        .mods-card .mods-list listview {{
          background: transparent;
        }}
        """.encode("utf-8")

    return css
