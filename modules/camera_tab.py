"""
camera_tab.py -- Camera UI for the NBA Bounce Mod Manager.

Self-contained, like slider_tab.py and team_tab.py: imports nothing from
app.py and never calls apply_single_mod(). All byte work lives in
camera_manager.py; this file is only tkinter.

    from camera_tab import CameraTab
    CameraTab(parent, game_data_path, host=app, theme={...}).pack(fill="both", expand=True)

or standalone for testing:

    python camera_tab.py [game_data_path]

What the user can do
--------------------
  * pick a built-in camera (Default, Broadcast, 2K Drive, Zoom, Close-Up,
    Overhead) or one of their own saved presets
  * fine-tune it with sliders: position, tilt, lens, tracking, advanced
  * save it as their own preset, and import / export .bouncecam files to share
  * apply it to the game, and ALWAYS get back to the original camera with
    "Revert to Default", which needs no backup file: the Default preset holds
    the game's original values exactly.

The tab also remembers the last camera applied (user/camera_active.json). If
something else puts level2 back to stock -- Restore Game Files, the sliders'
Restore Stock, a Steam file check -- the tab notices and offers to re-apply it.
"""

from __future__ import annotations

import copy
import json
import math
import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import camera_manager as cmgr

try:
    import app_paths
    USER_PRESET_DIR = app_paths.user("camera_presets")
    ACTIVE_FILE = app_paths.user("camera_active.json")
except Exception:                                   # standalone without app_paths
    _here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    USER_PRESET_DIR = os.path.join(_here, "user", "camera_presets")
    ACTIVE_FILE = os.path.join(_here, "user", "camera_active.json")

DEFAULT_THEME = {
    "bg": "#1a1a2e", "panel": "#16213e", "accent": "#0f3460",
    "text": "#eaeaea", "muted": "#a0a0b0", "entry": "#0d1b2a",
    "gold": "#FDB927", "red": "#e94560",
}
OK_GREEN = "#4ade80"
NEAR_COL = "#4aa3ff"
FAR_COL = "#FDB927"

# (key, label, source, min, max, step, decimals, tooltip)
#   source "rig"    -> regenerates the six camera points
#   source "tuning" -> written straight into GameplayCamera
#   source "far"    -> the render camera's far clip
SLIDER_GROUPS = [
    ("Isometric: fixed angle", "iso", [
        ("iso_yaw", "Diagonal angle", "rig", -80, 80, 0.5, 1,
         "How far the view is turned toward one basket. Positive faces the right "
         "basket and negative faces the left. The angle is the same everywhere on "
         "the floor, so the camera never turns. Classic Live Iso uses 44.9."),
        ("iso_pitch", "Tilt down", "rig", 5, 85, 0.5, 1,
         "How steeply the camera looks down. Classic Live Iso uses 28.8."),
        ("iso_distance", "Camera distance", "rig", 5, 150, 0.5, 1,
         "How far the camera sits from the spot it looks at. Far away with a small "
         "field of view gives the flat, near-isometric 16-bit look."),
        ("iso_fov", "Field of view", "rig", 3, 80, 0.1, 1,
         "Lens angle. Lower is flatter and more isometric. Classic Live Iso uses 8.7."),
    ]),
    ("Isometric: follow", "iso", [
        ("iso_pan", "Pan toward the baskets", "rig", 0, 16, 0.1, 1,
         "How far the view slides toward a basket when the ball is near it. The "
         "rims are 12.45 from mid-court."),
        ("iso_depth", "Pan toward near/far side", "rig", 0, 7, 0.1, 1,
         "How far the view slides toward the sideline the ball is nearest."),
    ]),
    ("Behind the play: near a basket", "end", [
        ("end_back", "Distance back from the rim", "rig", 3, 40, 0.1, 1,
         "How far the camera sits back from the rim toward half court. The 2K "
         "Half-Court preset uses 14.6, which puts it just short of half court."),
        ("end_height", "Height", "rig", 1, 40, 0.1, 1,
         "Camera height in metres. 2K Half-Court uses 6.9."),
        ("end_pitch", "Tilt down", "rig", 0, 80, 0.5, 1,
         "How steeply the camera looks down at the floor, in degrees. 2K Half-Court uses 25."),
        ("end_fov", "Field of view", "rig", 5, 80, 0.5, 1,
         "Lens angle. Low numbers are a long, zoomed lens like a TV camera, high "
         "numbers are wide. The game adds Extra FOV (Advanced) on top. 2K "
         "Half-Court uses 39.5."),
        ("end_side", "Sideways offset", "rig", -7, 7, 0.1, 1,
         "Moves the camera toward the near sideline (positive) or the far sideline "
         "(negative). 0 keeps it lined up with the rim."),
        ("end_follow", "Slide toward the ball's side", "rig", 0, 7, 0.05, 2,
         "How far the camera slides sideways to follow the ball when it moves to "
         "one side of the court."),
    ]),
    ("Behind the play: mid-court", "end", [
        ("mid_distance", "Distance back from the sideline", "rig", 4, 60, 0.5, 1,
         "Around half court the camera swings out to a sideline view. This sets how "
         "far back it sits from the middle of the court."),
        ("mid_height", "Height", "rig", 1, 40, 0.1, 1,
         "Height of the mid-court sideline view."),
        ("mid_pitch", "Tilt down", "rig", 0, 80, 0.5, 1,
         "Tilt of the mid-court sideline view."),
        ("mid_fov", "Field of view", "rig", 5, 80, 0.5, 1,
         "Lens angle of the mid-court sideline view."),
    ]),
    ("Position", "sideline", [
        ("near_distance", "Distance back — near side", "rig", 4, 90, 0.5, 1,
         "How far the camera sits back from the middle of the court when the ball is "
         "on the near side. Default 36."),
        ("far_distance", "Distance back — far side", "rig", 4, 90, 0.5, 1,
         "The same for when the ball is on the far side. Set it lower than the near "
         "value to have the camera move in on far-side play. Default 36."),
        ("near_height", "Height — near side", "rig", 0.5, 70, 0.1, 1,
         "Camera height when the ball is on the near side. Default 9.5."),
        ("far_height", "Height — far side", "rig", 0.5, 70, 0.1, 1,
         "Camera height when the ball is on the far side. Default 10.2."),
        ("pan_range", "Pan range (side to side)", "rig", 0, 20, 0.1, 1,
         "How far the camera slides toward each basket. A higher value follows the "
         "ball down the court more closely. Default 8.6."),
    ]),
    ("Angle & Lens", "sideline", [
        ("near_pitch", "Tilt down — near side", "rig", -10, 89, 0.5, 1,
         "How steeply the camera looks down, in degrees. Default 14."),
        ("far_pitch", "Tilt down — far side", "rig", -10, 89, 0.5, 1,
         "Tilt used when the ball is on the far side. Default 14."),
        ("toe_in", "Turn in at the ends", "rig", 0, 30, 0.5, 1,
         "Rotates the camera toward the middle of the court when it pans out to an "
         "end, like a TV camera following the play. Default 0."),
        ("fov", "Field of view", "rig", 2, 60, 0.1, 1,
         "Lens angle in degrees. Lower numbers zoom in and higher numbers zoom out. The "
         "game adds the Extra FOV value (Advanced) on top. Default 11.3."),
    ]),
    ("Tracking", None, [
        ("follow_smooth", "Follow smoothing (seconds)", "tuning", 0, 3, 0.01, 2,
         "How long the camera takes to catch up with the ball. Lower is snappier and "
         "higher is smoother. Default 0.25."),
        ("possession_lead", "Lead toward the attacking basket", "tuning", -10, 10, 0.1, 1,
         "How far the camera leans ahead toward the basket the team with the ball is "
         "attacking. Default 1."),
        ("velocity_lead", "Lead on loose balls", "tuning", 0, 3, 0.01, 2,
         "While nobody has the ball, how far the camera looks ahead in the direction the "
         "ball is moving. Default 0.2."),
        ("height_follow", "Rise with high balls", "tuning", 0, 40, 0.5, 1,
         "How strongly the camera lifts to keep a high ball, like a lob or a big "
         "rebound, in frame. Default 14."),
        ("deadzone_y", "Start rising when ball is above", "tuning", 0, 1, 0.01, 2,
         "The ball's height on screen before the camera starts to rise. 0 is the "
         "bottom of the screen and 1 is the top. Default 0.70."),
        ("height_smooth", "Vertical smoothing (seconds)", "tuning", 0, 3, 0.01, 2,
         "How gently the camera rises and falls. Default 0.40."),
    ]),
    ("Advanced", None, [
        ("added_fov", "Extra FOV (added everywhere)", "tuning", -10, 40, 0.1, 1,
         "A fixed amount the game adds to the field of view. Default 3."),
        ("possession_smooth", "Lead smoothing (seconds)", "tuning", 0, 5, 0.05, 2,
         "How gradually the lead shifts when possession changes. Default 1."),
        ("velocity_smooth", "Loose-ball smoothing (seconds)", "tuning", 0, 5, 0.05, 2,
         "Smoothing on the loose-ball lead. Default 2."),
        ("max_viewport_height", "Rise limit", "tuning", 0, 3, 0.05, 2,
         "Caps how high on screen the ball is counted when the camera rises. Default "
         "1.2."),
        ("far_clip", "Draw distance", "far", 10, 2000, 5, 0,
         "How far the camera renders. Raise it if a high or distant camera cuts off "
         "the arena. Default 100."),
    ]),
]

INFO_TEXT = (
    "Close NBA Bounce before applying. The game loads the camera once at startup, "
    "so quit to the desktop and relaunch to see a change."
)
AFTER_TEXT = (
    "Start NBA Bounce to see it. If the game was already running, quit to the "
    "desktop and relaunch: the camera is only read at startup, and the intro and "
    "jump ball keep their own animated shots."
)


def _is_light(color):
    try:
        c = color.lstrip("#")
        r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
        return (0.299 * r + 0.587 * g + 0.114 * b) > 150
    except Exception:
        return False


def _clip(x0, y0, x1, y1, w, h):
    """Liang-Barsky clip of a segment to the 0..w, 0..h box."""
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0), (dx, w - x0), (-dy, y0), (dy, h - y0)):
        if p == 0:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    return x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy


# Starting rig when the user flips the style radio: the game's own camera for
# "sideline", the 2K Half-Court preset for "end".
_STYLE_TEMPLATE = {
    "sideline": cmgr.rig_from_points(cmgr.builtin(cmgr.DEFAULT_KEY)["points"]),
    "end": dict(cmgr.BUILTIN_PRESETS["2k_halfcourt"]["rig"]),
    "iso": dict(cmgr.BUILTIN_PRESETS["live_iso"]["rig"]),
    None: {},
}


class CameraTab(tk.Frame):
    def __init__(self, master, game_data_path: str, host=None,
                 theme: dict | None = None, **kw):
        self.th = dict(DEFAULT_THEME)
        if theme:
            self.th.update(theme)
        super().__init__(master, bg=self.th["bg"], **kw)
        self.host = host
        self.game_data_path = game_data_path or ""
        self.cm = None
        self.library = cmgr.PresetLibrary(USER_PRESET_DIR)
        self.entries = []            # [(kind, key/path, preset)] matching listbox rows
        self.edit = None             # validated preset in the editor
        self.edit_rig = None
        self.base_label = ""
        self.dirty = False
        self.live = None             # preset read from level2
        self._busy = False
        self._loading_vars = False
        self._load_started = False
        self.vars: dict[str, tk.DoubleVar] = {}
        # bright green is unreadable on the light theme's white background
        self.ok = "#15803d" if _is_light(self.th["bg"]) else OK_GREEN
        self._init_styles()
        self._build()
        self._refresh_list()

    # ------------------------------------------------------------------ styles
    def _init_styles(self):
        t = self.th
        s = ttk.Style(self)
        s.configure("Cam.TFrame", background=t["bg"])
        s.configure("Cam.TLabelframe", background=t["bg"], bordercolor=t["accent"],
                    relief="solid", borderwidth=1)
        s.configure("Cam.TLabelframe.Label", background=t["bg"], foreground=t["gold"],
                    font=("Segoe UI", 10, "bold"))
        # accent is a fixed navy in both themes, so text on it is always white
        s.configure("Cam.TButton", background=t["accent"], foreground="#FFFFFF",
                    font=("Segoe UI", 10), relief="flat", padding=6)
        s.map("Cam.TButton", background=[("active", "#1a4a80"), ("disabled", "#555")],
              foreground=[("disabled", "#bbbbbb")])
        s.configure("CamAccent.TButton", background=t["red"], foreground="#FFFFFF",
                    font=("Segoe UI", 10, "bold"), relief="flat", padding=6)
        s.map("CamAccent.TButton", background=[("active", "#c73652"), ("disabled", "#555")],
              foreground=[("disabled", "#bbbbbb")])
        s.configure("Cam.Horizontal.TScale", background=t["bg"], troughcolor=t["entry"],
                    borderwidth=0)
        s.configure("Cam.Vertical.TScrollbar", background=t["accent"],
                    troughcolor=t["panel"], arrowcolor="#FFFFFF", borderwidth=0)

    # ---------------------------------------------------------------------- UI
    def _build(self):
        t = self.th
        head = tk.Frame(self, bg=t["bg"])
        head.pack(fill="x", padx=12, pady=(12, 2))
        tk.Label(head, text="🎥  Camera", bg=t["bg"], fg=t["red"],
                 font=("Segoe UI", 13, "bold")).pack(side="left")
        self.live_lbl = tk.Label(head, text="", bg=t["bg"], fg=t["muted"],
                                 font=("Segoe UI", 10, "bold"))
        self.live_lbl.pack(side="right")
        tk.Label(self, text=INFO_TEXT, bg=t["bg"], fg=t["muted"], font=("Segoe UI", 9),
                 anchor="w", justify="left", wraplength=1100).pack(fill="x", padx=12)

        # Re-apply banner (hidden until needed)
        self.banner = tk.Frame(self, bg=t["gold"])
        self.banner_lbl = tk.Label(self.banner, text="", bg=t["gold"], fg="#111111",
                                   font=("Segoe UI", 9, "bold"), anchor="w",
                                   justify="left")
        self.banner_lbl.pack(side="left", padx=10, pady=6, fill="x", expand=True)
        self.banner_btn = ttk.Button(self.banner, text="Re-apply", style="Cam.TButton",
                                     command=self._reapply_active)
        self.banner_btn.pack(side="right", padx=8, pady=4)

        # Runtime camera plugin warning (hidden until needed)
        self.plugin_banner = tk.Frame(self, bg=t["red"])
        self.plugin_lbl = tk.Label(self.plugin_banner, text="", bg=t["red"], fg="#FFFFFF",
                                   font=("Segoe UI", 9, "bold"), anchor="w",
                                   justify="left", wraplength=900)
        self.plugin_lbl.pack(side="left", padx=10, pady=6, fill="x", expand=True)
        self.plugin_btn = ttk.Button(self.plugin_banner, text="Turn the plugin off",
                                     style="Cam.TButton", command=self._disable_plugin)
        self.plugin_btn.pack(side="right", padx=8, pady=4)

        body = tk.Frame(self, bg=t["bg"])
        body.pack(fill="both", expand=True, padx=12, pady=6)
        self._body = body

        # -- left: presets -------------------------------------------------
        left = ttk.Labelframe(body, text=" Presets ", style="Cam.TLabelframe")
        left.pack(side="left", fill="y", padx=(0, 8))
        self.listbox = tk.Listbox(left, width=26, height=16, activestyle="none",
                                  bg=t["panel"], fg=t["text"], relief="flat",
                                  highlightthickness=0, selectbackground=t["accent"],
                                  selectforeground="#FFFFFF", exportselection=False,
                                  font=("Segoe UI", 10))
        self.listbox.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.listbox.bind("<<ListboxSelect>>", self._on_pick)
        self.desc_lbl = tk.Label(left, text="", bg=t["bg"], fg=t["muted"],
                                 font=("Segoe UI", 9), wraplength=200, justify="left",
                                 anchor="nw", height=7)
        self.desc_lbl.pack(fill="x", padx=8)
        row = tk.Frame(left, bg=t["bg"])
        row.pack(fill="x", padx=8, pady=(4, 8))
        self.import_btn = ttk.Button(row, text="📂 Import…", style="Cam.TButton",
                                     command=self._import)
        self.import_btn.pack(side="left", fill="x", expand=True)
        self.export_btn = ttk.Button(row, text="📤 Export…", style="Cam.TButton",
                                     command=self._export)
        self.export_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.delete_btn = ttk.Button(left, text="🗑 Delete my preset", style="Cam.TButton",
                                     command=self._delete)
        self.delete_btn.pack(fill="x", padx=8, pady=(0, 8))

        # -- right: preview + sliders -------------------------------------
        right = tk.Frame(body, bg=t["bg"])
        right.pack(side="left", fill="both", expand=True)
        top = tk.Frame(right, bg=t["bg"])
        top.pack(fill="x")
        self.editing_lbl = tk.Label(top, text="", bg=t["bg"], fg=t["text"],
                                    font=("Segoe UI", 11, "bold"), anchor="w")
        self.editing_lbl.pack(side="left")
        self.view_var = tk.StringVar(value="basket")
        for val, txt in (("mid", "ball at mid-court"), ("basket", "ball near a basket")):
            tk.Radiobutton(top, text=txt, value=val, variable=self.view_var,
                           command=self._draw_preview, bg=t["bg"], fg=t["text"],
                           selectcolor=t["entry"], activebackground=t["bg"],
                           activeforeground=t["text"], font=("Segoe UI", 9),
                           highlightthickness=0).pack(side="right")
        tk.Label(top, text="Camera view with", bg=t["bg"], fg=t["muted"],
                 font=("Segoe UI", 9)).pack(side="right")
        self.preview = tk.Canvas(right, height=230, bg=t["panel"], highlightthickness=0)
        self.preview.pack(fill="x", pady=(4, 6))
        self.preview.bind("<Configure>", lambda e: self._draw_preview())

        srow = tk.Frame(right, bg=t["bg"])
        srow.pack(fill="x", pady=(0, 4))
        tk.Label(srow, text="Camera style:", bg=t["bg"], fg=t["gold"],
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self.style_var = tk.StringVar(value="sideline")
        self.style_btns = []
        for val, txt in (("sideline", "Sideline (NBA Bounce)"),
                         ("end", "Behind the play (NBA 2K)"),
                         ("iso", "Isometric (classic NBA Live)")):
            b = tk.Radiobutton(srow, text=txt, value=val, variable=self.style_var,
                               command=self._on_style, bg=t["bg"], fg=t["text"],
                               selectcolor=t["entry"], activebackground=t["bg"],
                               activeforeground=t["text"], font=("Segoe UI", 10),
                               highlightthickness=0)
            b.pack(side="left", padx=(8, 0))
            self.style_btns.append(b)

        wrap = tk.Frame(right, bg=t["bg"])
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, highlightthickness=0, bg=t["bg"], bd=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview,
                           style="Cam.Vertical.TScrollbar")
        self.inner = tk.Frame(self.canvas, bg=t["bg"])
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
            w.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        self._build_sliders()

        # -- footer ---------------------------------------------------------
        foot = tk.Frame(self, bg=t["bg"])
        foot.pack(fill="x", padx=12, pady=(4, 12))
        self.apply_btn = ttk.Button(foot, text="▶  Apply to Game", style="CamAccent.TButton",
                                    command=self._apply)
        self.apply_btn.pack(side="left")
        self.revert_btn = ttk.Button(foot, text="↺  Revert to Default Camera",
                                     style="Cam.TButton", command=self._revert)
        self.revert_btn.pack(side="left", padx=6)
        self.save_btn = ttk.Button(foot, text="💾  Save as My Preset…", style="Cam.TButton",
                                   command=self._save_as)
        self.save_btn.pack(side="left")
        self.reset_btn = ttk.Button(foot, text="Undo my edits", style="Cam.TButton",
                                    command=self._undo_edits)
        self.reset_btn.pack(side="left", padx=6)
        self.status = tk.Label(foot, text="", bg=t["bg"], fg=t["muted"],
                               font=("Segoe UI", 9), anchor="e")
        self.status.pack(side="right")
        self._set_enabled(False)

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def _build_sliders(self):
        t = self.th
        self.inner.columnconfigure(0, weight=1, uniform="c")
        self.inner.columnconfigure(1, weight=1, uniform="c")
        self.spec = {}
        self.style_boxes = {"sideline": [], "end": [], "iso": []}
        cols = {"sideline": 0, "end": 0, "iso": 0, None: 0}
        for title, style, rows in SLIDER_GROUPS:
            box = ttk.Labelframe(self.inner, text=f" {title} ", style="Cam.TLabelframe")
            # style-specific groups share row 0; the shared ones sit in row 1
            box.grid(row=0 if style else 1, column=cols[style], sticky="nsew",
                     padx=3, pady=3)
            cols[style] += 1
            if style:
                self.style_boxes[style].append(box)
            for key, label, src, lo, hi, step, dec, tip in rows:
                self.spec[key] = (src, lo, hi, step, dec, style)
                r = tk.Frame(box, bg=t["bg"])
                r.pack(fill="x", padx=8, pady=2)
                lbl = tk.Label(r, text=label + "  ⓘ", bg=t["bg"], fg=t["text"],
                               font=("Segoe UI", 9), anchor="w", width=30)
                lbl.pack(anchor="w")
                self._tooltip(lbl, tip)
                line = tk.Frame(r, bg=t["bg"])
                line.pack(fill="x")
                var = tk.DoubleVar(value=lo)
                self.vars[key] = var
                ttk.Scale(line, from_=lo, to=hi, variable=var, orient="horizontal",
                          style="Cam.Horizontal.TScale").pack(side="left", fill="x",
                                                              expand=True)
                fmt = "%." + str(dec) + "f"
                sp = tk.Spinbox(line, from_=lo, to=hi, increment=step, textvariable=var,
                                width=7, format=fmt, bg=t["entry"], fg=t["text"],
                                buttonbackground=t["accent"], relief="flat",
                                insertbackground=t["text"], justify="right",
                                font=("Segoe UI", 9))
                sp.pack(side="left", padx=(6, 0))
                var.trace_add("write", lambda *_a, k=key: self._on_slider(k))

    def _tooltip(self, widget, text):
        state = {"win": None}

        def enter(_e):
            if state["win"]:
                return
            w = tk.Toplevel(widget)
            w.wm_overrideredirect(True)
            w.wm_geometry("+%d+%d" % (widget.winfo_rootx() + 20,
                                      widget.winfo_rooty() + widget.winfo_height() + 2))
            tk.Label(w, text=text, justify="left", wraplength=380, bg=self.th["entry"],
                     fg=self.th["text"], relief="solid", borderwidth=1,
                     font=("Segoe UI", 9)).pack(ipadx=6, ipady=4)
            state["win"] = w

        def leave(_e):
            if state["win"]:
                state["win"].destroy()
                state["win"] = None
        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)

    def _set_enabled(self, on: bool):
        for b in (self.apply_btn, self.save_btn, self.reset_btn, self.export_btn):
            b.state(["!disabled"] if on else ["disabled"])
        # Revert is the one control that must stay reachable: it only needs
        # the camera to have been located, not a valid editor state.
        self.revert_btn.state(["!disabled"] if (self.cm and self.cm.loaded
                                                and not self._busy) else ["disabled"])

    # ------------------------------------------------------------ preset list
    def _refresh_list(self, select=None):
        self.entries = []
        self.listbox.delete(0, "end")
        self.listbox.insert("end", "BUILT-IN")
        self.entries.append(("header", None, None))
        for key in cmgr.BUILTIN_PRESETS:
            p = cmgr.builtin(key)
            self.listbox.insert("end", "   " + p["name"] + ("   (original)" if key == cmgr.DEFAULT_KEY else ""))
            self.entries.append(("builtin", key, p))
        self.listbox.insert("end", "")
        self.entries.append(("header", None, None))
        self.listbox.insert("end", "MY PRESETS")
        self.entries.append(("header", None, None))
        mine = self.library.list()
        if not mine:
            self.listbox.insert("end", "   (none yet)")
            self.entries.append(("header", None, None))
        for path, p in mine:
            self.listbox.insert("end", "   " + p["name"])
            self.entries.append(("user", path, p))
        for i, (kind, _, _) in enumerate(self.entries):
            if kind == "header":
                self.listbox.itemconfigure(i, fg=self.th["gold"],
                                           selectbackground=self.th["panel"],
                                           selectforeground=self.th["gold"])
        if getattr(self.library, "errors", None):
            self._say("Skipped unreadable preset(s): " + "; ".join(self.library.errors[:3]),
                      self.th["red"])
        if select is not None:
            self._select_where(select)

    def _select_where(self, pred):
        for i, e in enumerate(self.entries):
            if e[0] != "header" and pred(e):
                self.listbox.selection_clear(0, "end")
                self.listbox.selection_set(i)
                self.listbox.see(i)
                self._update_list_buttons()
                return True
        return False

    def _selected(self):
        sel = self.listbox.curselection()
        if not sel:
            return None
        e = self.entries[sel[0]]
        return None if e[0] == "header" else e

    def _update_list_buttons(self):
        e = self._selected()
        self.delete_btn.state(["!disabled"] if e and e[0] == "user" else ["disabled"])
        self.desc_lbl.configure(text=(e[2].get("description") or "") if e else "")

    def _on_pick(self, _evt=None):
        e = self._selected()
        if e is None:
            # clicked a heading: keep the editor where it was
            self._update_list_buttons()
            return
        if self.dirty and not messagebox.askyesno(
                "Camera", "Discard your unsaved slider changes?", parent=self):
            self._update_list_buttons()
            return
        self._load_into_editor(e[2], e[2]["name"])
        self._update_list_buttons()

    # --------------------------------------------------------------- editor
    def _load_into_editor(self, preset, label):
        self.edit = cmgr.validate_preset(copy.deepcopy(preset))
        self.edit_rig = dict(self.edit.get("rig") or cmgr.rig_from_points(self.edit["points"]))
        self.base_label = label
        self._show_style(cmgr.rig_style(self.edit_rig))
        self.dirty = False
        self._loading_vars = True
        try:
            for key, (src, lo, hi, step, dec, _st) in self.spec.items():
                if src == "rig":
                    # sliders of the style not in use show that style's template
                    v = self.edit_rig.get(key, _STYLE_TEMPLATE[_st].get(key, lo))
                elif src == "tuning":
                    v = self.edit["tuning"][key]
                else:
                    v = self.edit["far_clip"]
                self.vars[key].set(round(min(max(v, lo), hi), dec))
        finally:
            self._loading_vars = False
        self._update_editing_label()
        self._draw_preview()

    def _on_slider(self, key):
        if self._loading_vars or self.edit is None:
            return
        src, lo, hi, step, dec, style = self.spec[key]
        if style and style != cmgr.rig_style(self.edit_rig):
            return                                  # hidden slider of the other style
        try:
            v = float(self.vars[key].get())
        except (tk.TclError, ValueError):
            return                                  # half-typed number in a spinbox
        v = round(min(max(v, lo), hi), dec)
        if abs(float(self.vars[key].get()) - v) > 1e-9:
            # snap the slider/spinbox to the step so the box shows e.g. 12.5,
            # not 12.4837...
            self._loading_vars = True
            try:
                self.vars[key].set(v)
            finally:
                self._loading_vars = False
        if src == "rig":
            self.edit_rig[key] = v
            if key == "fov":
                self.edit_rig.pop("far_fov", None)
            self.edit["points"] = cmgr.build_points_from_rig(self.edit_rig)
            self.edit["rig"] = dict(self.edit_rig)
        elif src == "tuning":
            self.edit["tuning"][key] = v
        else:
            self.edit["far_clip"] = v
        if not self.dirty:
            self.dirty = True
            self._update_editing_label()
        self._draw_preview()

    def _show_style(self, style):
        self.style_var.set(style)
        for st, boxes in self.style_boxes.items():
            for b in boxes:
                if st == style:
                    b.grid()
                else:
                    b.grid_remove()

    def _on_style(self):
        """Switch between the sideline and behind-the-play layouts. The rig is
        replaced by that style's template; tracking and advanced values stay."""
        if self.edit is None:
            return
        style = self.style_var.get()
        if style == cmgr.rig_style(self.edit_rig):
            return
        self.edit_rig = dict(_STYLE_TEMPLATE[style])
        self.edit["rig"] = dict(self.edit_rig)
        self.edit["points"] = cmgr.build_points_from_rig(self.edit_rig)
        self._show_style(style)
        self._loading_vars = True
        try:
            for key, (src, lo, hi, step, dec, st) in self.spec.items():
                if src == "rig" and st == style:
                    self.vars[key].set(round(min(max(self.edit_rig.get(key, lo), lo), hi), dec))
        finally:
            self._loading_vars = False
        self.dirty = True
        self._update_editing_label()
        self._draw_preview()

    def _update_editing_label(self):
        if self.edit is None:
            self.editing_lbl.configure(text="")
            return
        txt = f"Editing: {self.base_label}"
        if self.dirty:
            txt += "  •  modified (not saved)"
        self.editing_lbl.configure(text=txt,
                                   fg=self.th["gold"] if self.dirty else self.th["text"])

    def _undo_edits(self):
        e = self._selected()
        if e:
            self._load_into_editor(e[2], e[2]["name"])
        elif self.live is not None:
            self._load_into_editor(self.live, "Camera in game now")

    def _current_preset(self):
        p = cmgr.validate_preset(copy.deepcopy(self.edit))
        if self.dirty:
            p["name"] = f"Custom (from {self.base_label})"[:60]
        return p

    # -------------------------------------------------------------- preview
    def _draw_preview(self):
        """Three panels: side profile, top view, and a wireframe of what the
        camera actually sees (court lines, rims and backboards)."""
        c = self.preview
        c.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if W < 120 or self.edit is None:
            return
        t = self.th
        pts = self.edit["points"]
        extra = self.edit["tuning"]["added_fov"]
        end_style = cmgr.rig_style(self.edit_rig) == "end"
        view_key = "BR" if self.view_var.get() == "basket" else "BC"

        # camera view gets a 16:9 box at the right; the rest splits in two
        vh = H - 34
        vw = int(vh * 16 / 9)
        if vw > W * 0.46:
            vw = int(W * 0.46)
            vh = int(vw * 9 / 16)
        vx0 = W - vw - 10
        split = int(vx0 * 0.48)
        for x in (split, vx0 - 6):
            c.create_line(x, 8, x, H - 8, fill=t["bg"])
        c.create_text(10, 8, text="SIDE VIEW", anchor="nw", fill=t["muted"],
                      font=("Segoe UI", 8, "bold"))
        c.create_text(split + 8, 8, text="TOP VIEW", anchor="nw", fill=t["muted"],
                      font=("Segoe UI", 8, "bold"))
        c.create_text(vx0, 8, anchor="nw", fill=t["muted"], font=("Segoe UI", 8, "bold"),
                      text="CAMERA VIEW  (" + ("ball near a basket" if view_key == "BR"
                                                else "ball at mid-court") + ")")
        first = c.create_text(10, H - 10, text="● ball on near side", anchor="w",
                              fill=NEAR_COL, font=("Segoe UI", 8))
        c.create_text(c.bbox(first)[2] + 10, H - 10, text="● ball on far side",
                      anchor="w", fill=FAR_COL, font=("Segoe UI", 8))

        self._draw_profile(c, pts, extra, cmgr.rig_style(self.edit_rig), 12, 26,
                           split - 12, H - 22)
        self._draw_top(c, pts, extra, view_key, split + 10, 26, vx0 - 16, H - 22)

        # ---- wireframe camera view
        vy0 = 24 + max(0, (H - 30 - vh) // 2)
        c.create_rectangle(vx0, vy0, vx0 + vw, vy0 + vh, fill="#0b0f1a", outline=t["accent"])
        for x0, y0, x1, y1 in cmgr.court_view_segments(pts[view_key], extra, vw, vh):
            seg = _clip(x0, y0, x1, y1, vw, vh)
            if seg:
                a, b, cc, d = seg
                c.create_line(vx0 + a, vy0 + b, vx0 + cc, vy0 + d, fill=FAR_COL)
        c.create_text(vx0 + vw, H - 10, anchor="e", fill=t["muted"], font=("Segoe UI", 8),
                      text="court lines only")

    def _draw_profile(self, c, pts, extra, style, x0, y0, x1, y1):
        t = self.th
        # Profile along the camera's own horizontal facing: across the court
        # for sideline, down the court at the right basket for behind-the-play,
        # along the fixed diagonal for isometric.
        end_style = style == "end"
        keys = ("BR", "TR") if end_style else ("BC", "TC")
        _, yaw = cmgr.quat_to_euler(pts[keys[0]]["rot"])
        ux, uz = math.sin(math.radians(yaw)), math.cos(math.radians(yaw))

        def along(p3):
            return p3[0] * ux + p3[2] * uz
        corners = [along((sx * cmgr.COURT_HALF_X, 0, sz * cmgr.COURT_HALF_Z))
                   for sx in (-1, 1) for sz in (-1, 1)]
        lo_c, hi_c = min(corners), max(corners)
        hs = [along(pts[k]["pos"]) for k in keys]
        ys = [pts[k]["pos"][1] for k in keys]
        amin, amax = min(hs + [lo_c]) - 2, max(hs + [hi_c]) + 2
        ymax = max(ys + [4.5]) * 1.15 + 1
        s = min((x1 - x0) / (amax - amin), (y1 - y0) / ymax)
        ox = x0 + ((x1 - x0) - s * (amax - amin)) / 2

        def P(a, y):
            return ox + (a - amin) * s, y1 - y * s
        c.create_line(*P(amin, 0), *P(amax, 0), fill=t["muted"])
        c.create_line(*P(lo_c, 0), *P(hi_c, 0), fill=t["gold"], width=4)
        if end_style:
            hx, hy = P(cmgr.HOOP_X, cmgr.RIM_HEIGHT)
            c.create_line(*P(cmgr.HOOP_X, cmgr.RIM_HEIGHT), *P(cmgr.COURT_HALF_X - 1.22,
                          cmgr.RIM_HEIGHT), fill=t["gold"], width=2)
            c.create_line(*P(cmgr.COURT_HALF_X - 1.22, 2.9), *P(cmgr.COURT_HALF_X - 1.22, 3.97),
                          fill=t["gold"], width=2)
            c.create_text(hx, hy - 4, text="rim", anchor="s", fill=t["muted"],
                          font=("Segoe UI", 7))
        for k, col in zip(keys, (NEAR_COL, FAR_COL)):
            p = pts[k]
            a, y = along(p["pos"]), p["pos"][1]
            pitch, _ = cmgr.quat_to_euler(p["rot"])
            vf = p["fov"] + extra
            for ang, dash in ((pitch, None), (pitch - vf / 2, (3, 3)), (pitch + vf / 2, (3, 3))):
                r = math.radians(ang)
                if r > 0.01:
                    ea, ey = a + y / math.tan(r), 0.0
                else:
                    ea, ey = amax, y - (amax - a) * math.tan(r)
                if ea > amax:
                    ey = y - (amax - a) * math.tan(r)
                    ea = amax
                c.create_line(*P(a, y), *P(ea, max(0.0, min(ey, ymax))), fill=col,
                              dash=dash, width=1 if dash else 2)
            cx, cy = P(a, y)
            c.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=col, outline="")

    def _draw_top(self, c, pts, extra, view_key, x0, y0, x1, y1):
        t = self.th
        HX, HZ = cmgr.COURT_HALF_X, cmgr.COURT_HALF_Z
        xs = [-HX, HX] + [pts[k]["pos"][0] for k in pts]
        zs = [-HZ, HZ] + [pts[k]["pos"][2] for k in pts]
        xmin, xmax, zmin, zmax = min(xs) - 2, max(xs) + 2, min(zs) - 2, max(zs) + 2
        s = min((x1 - x0) / (xmax - xmin), (y1 - y0) / (zmax - zmin))
        ox = x0 + ((x1 - x0) - s * (xmax - xmin)) / 2
        oy = y0 + ((y1 - y0) - s * (zmax - zmin)) / 2

        def P(x, z):                                # far sideline at the top
            return ox + (x - xmin) * s, oy + (zmax - z) * s
        c.create_rectangle(*P(-HX, HZ), *P(HX, -HZ), outline=t["gold"], width=2)
        c.create_line(*P(0, HZ), *P(0, -HZ), fill=t["gold"])
        for sgn in (-1, 1):
            c.create_rectangle(*P(sgn * HX, 2.44), *P(sgn * (HX - 5.79), -2.44), outline=t["gold"])
            hx, hy = P(sgn * cmgr.HOOP_X, 0)
            c.create_oval(hx - 3, hy - 3, hx + 3, hy + 3, outline=t["gold"])
        # viewing wedge of the camera shown in the camera view
        p = pts[view_key]
        pitch, yaw = cmgr.quat_to_euler(p["rot"])
        hf = math.degrees(math.atan(math.tan(math.radians((p["fov"] + extra) / 2)) * 16 / 9))
        L = 60.0
        for a in (yaw - hf, yaw + hf):
            r = math.radians(a)
            ax_, ay_ = P(p["pos"][0], p["pos"][2])
            bx_, by_ = P(p["pos"][0] + L * math.sin(r), p["pos"][2] + L * math.cos(r))
            seg = _clip(ax_ - x0, ay_ - y0, bx_ - x0, by_ - y0, x1 - x0, y1 - y0)
            if seg:
                c.create_line(seg[0] + x0, seg[1] + y0, seg[2] + x0, seg[3] + y0,
                              fill=NEAR_COL, dash=(3, 3))
        for row, col in (("B", NEAR_COL), ("T", FAR_COL)):
            for k in ("L", "C", "R"):
                q = pts[row + k]
                x, z = q["pos"][0], q["pos"][2]
                _, yw = cmgr.quat_to_euler(q["rot"])
                r = math.radians(yw)
                c.create_line(*P(x, z), *P(x + 2.5 * math.sin(r), z + 2.5 * math.cos(r)),
                              fill=col, width=2)
                cx, cy = P(x, z)
                c.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill=col, outline="")

    # --------------------------------------------------------------- loading
    def load(self, silent=False):
        """Called when the tab is selected. Loads once, then just re-checks
        what is in the game (another tool may have restored level2)."""
        if not self.game_data_path or not os.path.isdir(self.game_data_path):
            self._say("Set your game data folder in ⚙ Settings to use the Camera tab.",
                      self.th["red"])
            return
        if self._busy:
            return
        if self.cm is not None and self.cm.loaded:
            self._run_bg("Checking the camera in the game…", self._bg_refresh,
                         self._after_refresh)
            return
        self._load_started = True
        self.cm = cmgr.CameraManager(self.game_data_path)
        self._run_bg("Reading the game camera…", self._bg_load,
                     self._after_refresh)

    def _bg_load(self):
        self.cm.load()
        return self.cm.read_current()

    def _bg_refresh(self):
        self.cm._ensure_loaded()
        return self.cm.read_current()

    def _run_bg(self, msg, work, done):
        self._busy = True
        self._set_enabled(False)
        self._say(msg, self.th["gold"])

        def run():
            try:
                res = work()
                self.after(0, lambda: self._bg_done(done, res, None))
            except Exception as exc:
                self.after(0, lambda e=exc: self._bg_done(done, None, e))
        threading.Thread(target=run, daemon=True).start()

    def _bg_done(self, done, res, err):
        self._busy = False
        if err is not None:
            self._set_enabled(self.edit is not None and self.cm is not None and self.cm.loaded)
            self._say("Camera unavailable", self.th["red"])
            self.live_lbl.configure(text="Camera: unavailable", fg=self.th["red"])
            messagebox.showerror("Camera", str(err), parent=self)
            return
        done(res)

    def _after_refresh(self, live):
        self.live = live
        name = self._identify(live)
        self._show_live(name)
        self._check_banner(live, name)
        self._check_plugin()
        if self.edit is None or not self.dirty:
            if not self._select_where(lambda e: cmgr.same_camera(e[2], live)):
                self.listbox.selection_clear(0, "end")
                self._update_list_buttons()
            sel = self._selected()
            if sel:
                self._load_into_editor(sel[2], sel[2]["name"])
            else:
                self._load_into_editor(live, "Camera in game now")
        self._set_enabled(True)
        self._say("Ready", self.ok)

    def _identify(self, live):
        for kind, key, p in self.entries:
            if kind != "header" and cmgr.same_camera(p, live):
                return p["name"]
        return None

    def _show_live(self, name):
        if name == "Default":
            self.live_lbl.configure(text="In game now: Default (original camera)",
                                    fg=self.th["muted"])
        elif name:
            self.live_lbl.configure(text=f"In game now: {name}  ✓", fg=self.ok)
        else:
            self.live_lbl.configure(text="In game now: Custom camera", fg=self.ok)
        if self.host is not None and hasattr(self.host, "status_var"):
            try:
                self.host.status_var.set("🎥 " + self.live_lbl.cget("text"))
            except Exception:
                pass

    # --------------------------------------------------- remembered camera
    def _read_active(self):
        try:
            with open(ACTIVE_FILE, "r", encoding="utf-8") as fh:
                return cmgr.validate_preset(json.load(fh))
        except Exception:
            return None

    def _write_active(self, preset):
        try:
            os.makedirs(os.path.dirname(ACTIVE_FILE), exist_ok=True)
            if preset is None or cmgr.same_camera(preset, cmgr.default_preset()):
                if os.path.exists(ACTIVE_FILE):
                    os.remove(ACTIVE_FILE)
                return
            with open(ACTIVE_FILE, "w", encoding="utf-8") as fh:
                json.dump(preset, fh, indent=1, ensure_ascii=False)
        except Exception:
            pass

    def _check_plugin(self):
        """Warn when the Bounce Camera BepInEx plugin is on: it replaces the
        match camera at runtime, so nothing written here shows in game."""
        try:
            info = cmgr.camera_plugin(self.game_data_path)
        except Exception:
            info = None
        if info and info["enabled"]:
            self.plugin_lbl.configure(
                text="The Bounce Camera plugin (BepInEx) is on. It replaces the match "
                     "camera while the game runs, so the camera chosen here won't show. "
                     "Turn it off here (or press F8 in a match) to use these cameras.")
            self.plugin_banner.pack(fill="x", padx=12, pady=(6, 0), before=self._body)
        else:
            self.plugin_banner.pack_forget()

    def _disable_plugin(self):
        try:
            cmgr.set_camera_plugin_enabled(self.game_data_path, False)
        except Exception as e:
            messagebox.showerror("Camera", str(e), parent=self)
            return
        self._check_plugin()
        messagebox.showinfo("Camera", "The Bounce Camera plugin is off. Restart NBA Bounce "
                                      "for it to take effect. Its settings are kept; set "
                                      "Enabled = true in BepInEx/config/"
                                      f"{cmgr.PLUGIN_CFG} to turn it back on.", parent=self)

    def _check_banner(self, live, name):
        active = self._read_active()
        if active and not cmgr.same_camera(active, live) and name == "Default":
            self.banner_lbl.configure(
                text=f"Your “{active['name']}” camera is no longer in the game files. "
                     "A restore or a Steam file check probably put the original back.")
            self.banner.pack(fill="x", padx=12, pady=(6, 0), before=self._body)
        else:
            self.banner.pack_forget()

    def _reapply_active(self):
        active = self._read_active()
        if active:
            self._do_apply(active, f"Re-applied “{active['name']}”.")

    # --------------------------------------------------------------- actions
    def _say(self, text, color=None):
        self.status.configure(text=text, fg=color or self.th["muted"])

    def _apply(self):
        if self.edit is None or self._busy:
            return
        try:
            preset = self._current_preset()
        except cmgr.PresetError as e:
            messagebox.showerror("Camera", f"These settings can't be applied:\n\n{e}",
                                 parent=self)
            return
        self._do_apply(preset, f"Applied “{preset['name']}”.")

    def _do_apply(self, preset, done_msg):
        def work():
            rep = self.cm.apply(preset)
            return rep, self.cm.read_current()

        def done(res):
            rep, live = res
            self._write_active(preset)
            self._after_refresh(live)
            self._say(f"{rep['verified']} of {rep['written']} values verified", self.ok)
            scene = os.path.basename(self.cm.scene)
            extra = (f"\n\nA one-time safety copy of {scene} was saved as "
                     f"{scene}.camera_backup." if rep["backed_up"] else "")
            messagebox.showinfo("Camera", f"{done_msg}\n\n{AFTER_TEXT}{extra}", parent=self)
        self._run_bg("Writing the camera to level2…", work, done)

    def _revert(self):
        if self._busy or self.cm is None or not self.cm.loaded:
            return
        if not messagebox.askyesno(
                "Revert to Default Camera",
                "Put NBA Bounce's original camera back?\n\nOnly the camera values are "
                "written. Your other mods stay as they are, and your saved presets are "
                "kept.", parent=self):
            return

        def work():
            rep = self.cm.revert_default()
            return rep, self.cm.read_current()

        def done(res):
            rep, live = res
            self._write_active(None)
            self.dirty = False
            self._after_refresh(live)
            self._select_where(lambda e: e[0] == "builtin" and e[1] == cmgr.DEFAULT_KEY)
            self._load_into_editor(cmgr.default_preset(), "Default")
            self._say(f"Original camera restored ({rep['verified']} values verified)",
                      self.ok)
            messagebox.showinfo("Camera", "The original camera is back.\n\n" + AFTER_TEXT,
                                parent=self)
        self._run_bg("Restoring the original camera…", work, done)

    def _ask_name(self, initial):
        name = simpledialog.askstring("Save camera preset", "Name for this camera:",
                                      initialvalue=initial, parent=self)
        if name is None:
            return None
        name = name.strip()[:60]
        if not name:
            return None
        if any(p["name"].lower() == name.lower() for p in
               (cmgr.BUILTIN_PRESETS[k] for k in cmgr.BUILTIN_PRESETS)):
            messagebox.showerror("Camera", f"“{name}” is a built-in preset. Pick another "
                                           "name.", parent=self)
            return None
        return name

    def _save_as(self):
        if self.edit is None:
            return
        base = self.base_label if not self.base_label.startswith("Camera in game") else "My Camera"
        e = self._selected()
        initial = e[2]["name"] if e and e[0] == "user" else f"My {base}"
        name = self._ask_name(initial)
        if not name:
            return
        path = self.library.path_for(name)
        if os.path.exists(path) and not messagebox.askyesno(
                "Camera", f"Replace your existing preset “{name}”?", parent=self):
            return
        p = cmgr.validate_preset(copy.deepcopy(self.edit))
        p["name"] = name
        p["author"] = p.get("author") if e and e[0] == "user" else ""
        p["description"] = f"Based on {self.base_label}."[:400] if self.dirty else p.get("description", "")
        try:
            saved = self.library.save(p)
        except Exception as ex:
            messagebox.showerror("Camera", f"Could not save the preset:\n\n{ex}", parent=self)
            return
        self.dirty = False
        self._refresh_list(select=lambda en: en[0] == "user" and
                           os.path.abspath(en[1]) == os.path.abspath(saved))
        self._load_into_editor(p, name)
        self._update_list_buttons()
        self._say(f"Saved “{name}”", self.ok)

    def _import(self):
        path = filedialog.askopenfilename(
            parent=self, title="Import camera preset",
            filetypes=[("Camera preset", "*" + cmgr.PRESET_EXT), ("JSON", "*.json"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            p = cmgr.load_preset_file(path)
        except Exception as e:
            messagebox.showerror("Import camera preset",
                                 f"That file can't be used:\n\n{e}", parent=self)
            return
        name, n = p["name"], 2
        builtin_names = {b["name"].lower() for b in cmgr.BUILTIN_PRESETS.values()}
        while name.lower() in builtin_names or os.path.exists(self.library.path_for(name)):
            name = f"{p['name']} ({n})"[:60]
            n += 1
        p["name"] = name
        saved = self.library.save(p)
        self._refresh_list(select=lambda en: en[0] == "user" and
                           os.path.abspath(en[1]) == os.path.abspath(saved))
        self.dirty = False
        self._load_into_editor(p, name)
        self._say(f"Imported “{name}”. Click Apply to Game to use it.", self.ok)

    def _export(self):
        if self.edit is None:
            return
        try:
            p = self._current_preset()
        except cmgr.PresetError as e:
            messagebox.showerror("Camera", str(e), parent=self)
            return
        if self.dirty or self.base_label.startswith("Camera in game"):
            name = self._ask_name("My Camera")
            if not name:
                return
            p["name"] = name
        path = filedialog.asksaveasfilename(
            parent=self, title="Export camera preset",
            initialfile=cmgr.safe_filename(p["name"]) + cmgr.PRESET_EXT,
            defaultextension=cmgr.PRESET_EXT,
            filetypes=[("Camera preset", "*" + cmgr.PRESET_EXT)])
        if not path:
            return
        try:
            cmgr.save_preset_file(p, path)
        except Exception as e:
            messagebox.showerror("Camera", f"Could not export:\n\n{e}", parent=self)
            return
        self._say(f"Exported {os.path.basename(path)}", self.ok)

    def _delete(self):
        e = self._selected()
        if not e or e[0] != "user":
            return
        if not messagebox.askyesno("Camera", f"Delete your preset “{e[2]['name']}”?\n\n"
                                             "This doesn't change the game.", parent=self):
            return
        try:
            self.library.delete(e[1])
        except Exception as ex:
            messagebox.showerror("Camera", str(ex), parent=self)
            return
        self._refresh_list()
        self._update_list_buttons()
        self._say(f"Deleted “{e[2]['name']}”", self.ok)

    # ------------------------------------------------------------ host hooks
    def set_game_path(self, path):
        if path == self.game_data_path:
            return
        self.game_data_path = path or ""
        self.cm = None
        self.live = None
        self.edit = None
        self._set_enabled(False)
        if self._load_started:
            self.load(silent=True)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    if not path:
        try:
            import app_paths
            with open(app_paths.user("config.json"), encoding="utf-8") as fh:
                path = json.load(fh).get("game_data_path", "")
        except Exception:
            pass
    root = tk.Tk()
    root.title("NBA Bounce — Camera")
    root.geometry("1150x780")
    root.configure(bg=DEFAULT_THEME["bg"])
    ttk.Style(root).theme_use("clam")
    tab = CameraTab(root, path)
    tab.pack(fill="both", expand=True)
    root.after(100, tab.load)
    root.mainloop()
