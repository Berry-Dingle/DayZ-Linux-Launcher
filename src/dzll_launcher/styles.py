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
        
        /* ---------- Entry placeholder (dims hint text only) ---------- */
        entry placeholder {{
          color: alpha(@theme_text_color, 0.45);
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
        }}
        
        /* Delete icon red */
        .mod-del-btn image {{ color: #ff4d4d; }}
        .mod-del-btn:backdrop image {{ color: #ff4d4d; }}
        
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