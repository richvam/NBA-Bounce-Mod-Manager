"""
slider_tab.py -- Gameplay Sliders UI for the NBA Bounce Mod Manager.

Self-contained, exactly like floor_patterns.py: imports nothing from app.py or
audio_manager.py, and never calls apply_single_mod(). Entry point mirrors
open_floor_patterns():

    from slider_tab import open_gameplay_sliders
    open_gameplay_sliders(parent, game_data_path, theme={...})

Can also be embedded as a frame:

    SliderTab(parent, game_data_path, theme=...).pack(fill="both", expand=True)

or run standalone for testing:

    python slider_tab.py
"""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

DEFAULT_THEME = {
    "bg": "#1a1a2e", "panel": "#16213e", "accent": "#0f3460",
    "text": "#eaeaea", "muted": "#a0a0b0", "entry": "#0d1b2a",
    "gold": "#FDB927", "red": "#e94560",
}
SIDE_BADGE = {"User": "#0f3460", "CPU": "#e94560", "Both": "#5a5a6e"}
OK_GREEN = "#4ade80"

# ---------------------------------------------------------------------------
# In-app explanation of how the in-game difficulty menu interacts with sliders.
# Users will otherwise assume the in-game menu overrides the app.
# ---------------------------------------------------------------------------
DIFFICULTY_EXPLAINER = """Which difficulty should I pick in-game?

It doesn't matter for almost everything.

NBA Bounce gives you three difficulty options in its own menu:

    Rookie        (easiest)
    Advanced      (middle)
    Pro           (hardest)

Those are three CPU skill sets, and the game blends between them based on which
one you pick. This app writes your slider values into all three at once, so every
blend produces exactly the same result. Pick whichever you like -- the CPU plays
the way your sliders say, not the way the menu says.

That also covers GOAT / Mountain mode, which ignores the menu entirely and picks
a difficulty from the tournament floor you are on.

Three small things still follow the in-game setting:

  - CPU passing. The AI's choice of pass target scales with the raw difficulty
    value, outside the three skill sets.

  - Wind Mountain (GOAT) mode. It swaps in its own wind-specific shot bars,
    selected by difficulty, so the shot-window sliders do not apply there.

  - Simulated season games. Games you sim rather than play carry the saved
    difficulty into the result.

If you want those consistent too, set the in-game difficulty to Advanced (the
middle option) and leave it there.

--------------------------------------------------------------------------

CAREFUL: two different things are called "Pro" and "Rookie"

The preset buttons in this app use the NBA 2K difficulty names. NBA Bounce uses
its own, and they overlap without meaning the same thing:

    App preset            What it actually matches in NBA Bounce
    ------------------    --------------------------------------------
    Rookie                EASIER than the game's Rookie difficulty
    Pro                   the game's ADVANCED difficulty (stock game)
    All-Star              between the game's Advanced and Pro
    Superstar             roughly the game's PRO difficulty (its hardest)
    Hall of Fame          harder than anything in the retail game

So the app's "Pro" preset is the stock middle setting, while the game's own "Pro"
difficulty is its hardest. If you want the game exactly as shipped, use the Pro
preset. If you want what the in-game Pro option gives you, use Superstar.

--------------------------------------------------------------------------

About the 0-100 scale

Unlike NBA 2K, where 50 is an abstract midpoint, this scale is anchored to the
values the developers actually shipped:

    25  =  the game's Rookie difficulty
    50  =  the game's Advanced difficulty   (i.e. the game exactly as balanced)
    75  =  the game's Pro difficulty        (its hardest in-game option)

Above 75 the values extrapolate past anything in the retail game. So setting a
slider to 50 is not a guess -- it reproduces stock NBA Bounce exactly.

Higher always means MORE of that thing for that side, the same convention 2K
uses. Raising a CPU slider makes the CPU better; raising a User slider makes
you better.

(For the curious: internally the game calls these three skill sets Beginner,
Advanced and Legend. Those names never appear on screen -- the English menu
shows Rookie, Advanced and Pro. There is even an unused "All-Star" difficulty
name left in the game's text data with no fourth skill set to go with it.)

--------------------------------------------------------------------------

IMPORTANT: fully quit the game before testing

NBA Bounce loads these assets once per process and keeps them in memory for the
whole session. Returning to the main menu or starting a new match will NOT pick
up your changes. You must quit all the way to the desktop and relaunch.
"""


class SliderTab(ttk.Frame):
    def __init__(self, master, game_data_path: str, theme: dict | None = None, **kw):
        self.th = dict(DEFAULT_THEME)
        if theme:
            self.th.update(theme)
        self._init_styles(master)
        super().__init__(master, style="Sld.TFrame", **kw)
        self.game_data_path = game_data_path
        self.sm = None
        self.vars: dict[str, tk.DoubleVar] = {}
        self.value_labels: dict[str, ttk.Label] = {}
        self._busy = False
        self._build()
        self.after(50, self._load_async)

    # ---------------------------------------------------------------- styles
    def _init_styles(self, master):
        t = self.th
        s = ttk.Style(master)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("Sld.TFrame", background=t["bg"])
        s.configure("SldPanel.TFrame", background=t["panel"])
        s.configure("Sld.TLabel", background=t["bg"], foreground=t["text"],
                    font=("Segoe UI", 10))
        s.configure("SldMuted.TLabel", background=t["bg"], foreground=t["muted"],
                    font=("Segoe UI", 9))
        s.configure("SldHead.TLabel", background=t["bg"], foreground=t["red"],
                    font=("Segoe UI", 13, "bold"))
        s.configure("Sld.TLabelframe", background=t["bg"], bordercolor=t["accent"],
                    relief="solid", borderwidth=1)
        s.configure("Sld.TLabelframe.Label", background=t["bg"],
                    foreground=t["gold"], font=("Segoe UI", 10, "bold"))
        # t["accent"] is a fixed navy brand color in both themes (not a light/dark
        # surface), so anything drawn on top of it uses a fixed white foreground
        # rather than t["text"] -- t["text"] flips to near-black in Light mode.
        s.configure("Sld.TButton", background=t["accent"], foreground="#FFFFFF",
                    font=("Segoe UI", 10), relief="flat", padding=6)
        s.map("Sld.TButton", background=[("active", "#1a4a80"), ("disabled", "#333")])
        s.configure("SldAccent.TButton", background=t["red"], foreground="white",
                    font=("Segoe UI", 10, "bold"), relief="flat", padding=6)
        s.map("SldAccent.TButton",
              background=[("active", "#c73652"), ("disabled", "#555")])
        s.configure("Sld.Horizontal.TScale", background=t["bg"],
                    troughcolor=t["entry"], borderwidth=0)
        s.configure("Sld.Vertical.TScrollbar", background=t["accent"],
                    troughcolor=t["panel"], arrowcolor="#FFFFFF", borderwidth=0)

    # -------------------------------------------------------------------- UI
    def _build(self):
        t = self.th
        header = ttk.Frame(self, style="Sld.TFrame")
        header.pack(fill="x", padx=12, pady=(12, 4))
        ttk.Label(header, text="\U0001F39A  Gameplay Sliders",
                  style="SldHead.TLabel").pack(side="left")
        self.status = ttk.Label(header, text="Loading game data\u2026",
                                style="SldMuted.TLabel")
        self.status.pack(side="right")

        presets = ttk.Labelframe(self, text=" Difficulty Presets (2K names) ",
                                 style="Sld.TLabelframe")
        presets.pack(fill="x", padx=12, pady=6)
        self.preset_row = ttk.Frame(presets, style="Sld.TFrame")
        self.preset_row.pack(fill="x", padx=10, pady=(10, 4))
        self.preset_desc = ttk.Label(presets, text="", wraplength=820,
                                     style="SldMuted.TLabel", justify="left")
        self.preset_desc.pack(fill="x", padx=10, pady=(0, 4))
        # The game's own menu says Rookie / Advanced / Pro. Two of those names
        # collide with the 2K preset names above and mean different things, so
        # say so on screen rather than only inside the explainer dialog.
        tk.Label(presets,
                 text="These are 2K-style preset names. NBA Bounce's own menu says "
                      "Rookie / Advanced / Pro — the app's “Pro” preset is the game's "
                      "ADVANCED setting, and the game's “Pro” is its hardest "
                      "(≈ Superstar preset).",
                 bg=t["bg"], fg=t["gold"], font=("Segoe UI", 8), justify="left",
                 wraplength=880, anchor="w").pack(fill="x", padx=10, pady=(0, 10))

        body = ttk.Frame(self, style="Sld.TFrame")
        body.pack(fill="both", expand=True, padx=12, pady=4)
        self.canvas = tk.Canvas(body, highlightthickness=0, bg=t["bg"], bd=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview,
                               style="Sld.Vertical.TScrollbar")
        self.inner = ttk.Frame(self.canvas, style="Sld.TFrame")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        # bind to this widget only -- bind_all would hijack the host app's scrolling
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
            w.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        foot = ttk.Frame(self, style="Sld.TFrame")
        foot.pack(fill="x", padx=12, pady=(6, 12))
        self.apply_btn = ttk.Button(foot, text="\u25B6  Apply to Game",
                                    style="SldAccent.TButton", command=self._apply)
        self.apply_btn.pack(side="left")
        ttk.Button(foot, text="\u267B  Restore Stock", style="Sld.TButton",
                   command=self._revert).pack(side="left", padx=6)
        ttk.Button(foot, text="\U0001F4BE  Save Set\u2026", style="Sld.TButton",
                   command=self._save).pack(side="left", padx=6)
        ttk.Button(foot, text="\U0001F4C2  Load Set\u2026", style="Sld.TButton",
                   command=self._load_set).pack(side="left")
        ttk.Button(foot, text="\u2753  Which difficulty in-game?", style="Sld.TButton",
                   command=self.show_explainer).pack(side="right")

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

    # ---------------------------------------------------------------- load
    def _load_async(self):
        def work():
            try:
                self.after(0, lambda: self.status.configure(
                    text="Checking dependencies…", foreground=self.th["gold"]))
                from slider_manager import SliderManager
                self.after(0, lambda: self.status.configure(
                    text="Reading game data… (level2 is 95 MB)",
                    foreground=self.th["gold"]))
                sm = SliderManager(self.game_data_path).load()
                rep = sm.validate()
                self.after(0, lambda: self._on_loaded(sm, rep))
            except Exception as exc:
                self.after(0, lambda e=exc: self._fail(e))
        threading.Thread(target=work, daemon=True).start()

    def _fail(self, exc):
        self.status.configure(text="Failed to load", foreground=self.th["red"])
        msg = str(exc)
        if isinstance(exc, ImportError) and "dnfile" in msg:
            msg = ("Gameplay Sliders needs the 'dnfile' package to read the "
                   "game's field layout, and it could not be installed "
                   "automatically.\n\nOpen a command prompt and run:\n\n"
                   "    py -m pip install dnfile\n\n"
                   "then reopen this window.\n\n(details: %s)" % msg)
        messagebox.showerror("Gameplay Sliders", msg, parent=self)

    def _on_loaded(self, sm, rep):
        self.sm = sm
        if not rep["ok"]:
            self.status.configure(text="Game data did not decode cleanly",
                                  foreground=self.th["red"])
            self.apply_btn.state(["disabled"])
            messagebox.showwarning(
                "Gameplay Sliders",
                "Some game objects did not decode to their expected size.\n\n"
                "This usually means NBA Bounce updated and the field layout "
                "changed. Sliders are disabled so nothing gets corrupted.\n\n"
                "Re-run build_slider_assets.py to regenerate the catalog for "
                "the new game version.", parent=self)
            return
        self.status.configure(
            text="%d objects verified  \u2022  %d CPU tiers"
                 % (rep["decoded_ok"], sm.tier_count), foreground=OK_GREEN)
        for name in sm.preset_names():
            ttk.Button(self.preset_row, text=name, style="Sld.TButton",
                       command=lambda n=name: self.pick_preset(n)).pack(side="left", padx=4)
        self._build_sliders()
        self.pick_preset("Pro")

    def _build_sliders(self):
        cat = self.sm.catalog
        for section in cat["categories"]:
            ids = [k for k, v in cat["sliders"].items() if v["category"] == section]
            if not ids:
                continue
            box = ttk.Labelframe(self.inner, text=" %s " % section,
                                 style="Sld.TLabelframe")
            box.pack(fill="x", pady=5, padx=2)
            for sid in sorted(ids, key=lambda k: (cat["sliders"][k]["side"],
                                                  cat["sliders"][k]["label"])):
                self._slider_row(box, sid, cat["sliders"][sid])

    def _slider_row(self, parent, sid, spec):
        row = ttk.Frame(parent, style="Sld.TFrame")
        row.pack(fill="x", padx=10, pady=3)
        tk.Label(row, text=spec["side"], width=5, font=("Segoe UI", 7, "bold"),
                 fg="white", bg=SIDE_BADGE.get(spec["side"], "#555"),
                 padx=2).pack(side="left")
        # every slider carries a description (build_slider_assets.py refuses to
        # emit a catalog otherwise) -- the marker tells users to hover for it
        tip = spec.get("description") or "No description available for this slider."
        lbl = ttk.Label(row, text=spec["label"] + "  ⓘ", width=32, anchor="w",
                        style="Sld.TLabel")
        lbl.pack(side="left", padx=(8, 0))
        self._tooltip(lbl, tip)
        var = tk.DoubleVar(value=50.0)
        self.vars[sid] = var
        val = ttk.Label(row, text="50", width=4, anchor="e", style="Sld.TLabel")
        self.value_labels[sid] = val

        def on_move(_v, s=sid):
            self.value_labels[s].configure(text="%d" % round(self.vars[s].get()))
            if self.sm:
                self.sm.set_slider(s, self.vars[s].get())
                self.preset_desc.configure(text="Custom \u2014 modified from a preset")
        ttk.Scale(row, from_=0, to=100, variable=var, orient="horizontal",
                  style="Sld.Horizontal.TScale", command=on_move).pack(
            side="left", fill="x", expand=True, padx=10)
        val.pack(side="left")

    def _tooltip(self, widget, text):
        state = {"win": None}

        def enter(_e):
            if state["win"]:
                return
            w = tk.Toplevel(widget)
            w.wm_overrideredirect(True)
            w.wm_geometry("+%d+%d" % (widget.winfo_rootx() + 24,
                                      widget.winfo_rooty() + widget.winfo_height() + 4))
            tk.Label(w, text=text, justify="left", wraplength=400,
                     bg=self.th["entry"], fg=self.th["text"],
                     relief="solid", borderwidth=1,
                     font=("Segoe UI", 9)).pack(ipadx=6, ipady=4)
            state["win"] = w

        def leave(_e):
            if state["win"]:
                state["win"].destroy()
                state["win"] = None
        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)

    # ------------------------------------------------------------- actions
    def pick_preset(self, name):
        self.sm.apply_preset(name)
        for sid, v in self.sm.values.items():
            if sid in self.vars:
                self.vars[sid].set(v)
                self.value_labels[sid].configure(text="%d" % round(v))
        self.preset_desc.configure(
            text="%s \u2014 %s" % (name, self.sm.presets["presets"][name]["description"]))

    def _apply(self):
        if self._busy or not self.sm:
            return
        self._busy = True
        self.apply_btn.state(["disabled"])
        self.status.configure(text="Writing\u2026 (level2 is 95 MB, give it a moment)",
                              foreground=self.th["gold"])

        def work():
            try:
                rep = self.sm.commit()
                self.after(0, lambda: self._done(rep))
            except Exception as exc:
                self.after(0, lambda e=exc: self._fail(e))
            finally:
                self._busy = False
                self.after(0, lambda: self.apply_btn.state(["!disabled"]))
        threading.Thread(target=work, daemon=True).start()

    def _done(self, rep):
        if rep["failed"]:
            self.status.configure(text="%d writes failed" % len(rep["failed"]),
                                  foreground=self.th["red"])
            messagebox.showerror("Gameplay Sliders",
                                 "%d values failed to write." % len(rep["failed"]),
                                 parent=self)
            return
        self.status.configure(text="%d of %d values verified"
                                   % (rep["verified"], rep["written"]),
                              foreground=OK_GREEN)
        messagebox.showinfo(
            "Applied",
            "Applied and verified %d values.\n\n"
            "Now fully quit NBA Bounce to the desktop and relaunch before "
            "testing.\n\nReturning to the main menu or starting a new match is "
            "NOT enough -- the game caches these assets for the whole session."
            % rep["verified"], parent=self)

    def _revert(self):
        if not self.sm:
            return
        if not self.sm.has_backup():
            messagebox.showinfo("Gameplay Sliders",
                                "No slider backup found \u2014 nothing to restore.",
                                parent=self)
            return
        self.status.configure(text="Restoring\u2026", foreground=self.th["gold"])

        def work():
            done = self.sm.revert()
            self.after(0, lambda: (
                self.status.configure(text="Restored %d files" % len(done),
                                      foreground=OK_GREEN),
                self.pick_preset("Pro")))
        threading.Thread(target=work, daemon=True).start()

    def _save(self):
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                                         filetypes=[("Slider set", "*.json")])
        if p:
            self.sm.export_set(p, os.path.splitext(os.path.basename(p))[0])
            self.status.configure(text="Saved %s" % os.path.basename(p),
                                  foreground=OK_GREEN)

    def _load_set(self):
        p = filedialog.askopenfilename(parent=self,
                                       filetypes=[("Slider set", "*.json")])
        if not p:
            return
        label = self.sm.import_set(p)
        for sid, v in self.sm.values.items():
            if sid in self.vars:
                self.vars[sid].set(v)
                self.value_labels[sid].configure(text="%d" % round(v))
        self.preset_desc.configure(text="Loaded set: %s" % label)

    def show_explainer(self):
        t = self.th
        win = tk.Toplevel(self)
        win.title("In-game difficulty and your sliders")
        win.geometry("660x560")
        win.configure(bg=t["bg"])
        frame = tk.Frame(win, bg=t["bg"])
        frame.pack(fill="both", expand=True, padx=2, pady=2)
        sb = ttk.Scrollbar(frame, orient="vertical",
                           style="Sld.Vertical.TScrollbar")
        txt = tk.Text(frame, wrap="word", padx=16, pady=16,
                      relief="flat", bg=t["panel"], fg=t["text"],
                      font=("Segoe UI", 10), yscrollcommand=sb.set,
                      insertbackground=t["text"])
        sb.configure(command=txt.yview)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        txt.insert("1.0", DIFFICULTY_EXPLAINER)
        txt.configure(state="disabled")


def open_gameplay_sliders(parent, game_data_path: str, theme: dict | None = None):
    """Open the Gameplay Sliders window. Mirrors open_floor_patterns()."""
    win = tk.Toplevel(parent)
    win.title("NBA Bounce \u2014 Gameplay Sliders")
    win.geometry("940x780")
    win.minsize(820, 600)
    win.configure(bg=(theme or DEFAULT_THEME)["bg"])
    SliderTab(win, game_data_path, theme=theme).pack(fill="both", expand=True)
    win.transient(parent)
    return win


if __name__ == "__main__":
    import json
    import app_paths
    cfg = app_paths.user("config.json")
    path = ""
    if len(sys.argv) > 1:
        path = sys.argv[1]
    elif os.path.exists(cfg):
        path = json.load(open(cfg, encoding="utf-8")).get("game_data_path", "")
    if not path or not os.path.isdir(path):
        sys.exit("Pass your NBA Bounce_Data folder as an argument, or set the Game "
                 "Data Folder in the Mod Manager's Settings first.")
    root = tk.Tk()
    root.title("NBA Bounce \u2014 Gameplay Sliders")
    root.geometry("940x780")
    root.configure(bg=DEFAULT_THEME["bg"])
    SliderTab(root, path).pack(fill="both", expand=True)
    root.mainloop()
