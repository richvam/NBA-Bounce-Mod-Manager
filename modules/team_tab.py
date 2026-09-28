"""
team_tab.py -- Teams UI for the NBA Bounce Mod Manager.

Self-contained, exactly like save_tab.py and slider_tab.py: imports nothing from
app.py and never calls apply_single_mod(). All byte work lives in
team_manager.py; this file is only tkinter.

    from team_tab import TeamTab
    TeamTab(parent, game_data_path, host=app, theme={...},
            on_rebuilt=app.reapply_mods).pack(fill="both", expand=True)

or standalone for testing:

    python team_tab.py [game_data_path]

TWO CLASSES OF EDIT, KEPT VISUALLY APART
----------------------------------------
Renaming and recoloring are in-place patches: instant, reversible, and unable to
disturb queued texture or audio mods. Adding or removing teams rebuilds two
asset files, which moves every byte offset in them -- so those buttons say so,
confirm first, and hand the host an on_rebuilt() callback to re-apply the mod
queue afterwards.

Everything that edits the game's CODE rather than its assets is behind the
Advanced section, off by default, and labelled with what it does and what undoes
it. That is a deliberate line: the rest of this app never touches code.
"""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from tkinter import colorchooser, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import team_manager as tmgr
except Exception:                                   # standalone / broken install
    tmgr = None
try:
    from team_layout import LayoutPanel
except Exception:                                   # Layout is optional
    LayoutPanel = None

DEFAULT_THEME = {
    "bg": "#1a1a2e", "panel": "#16213e", "accent": "#0f3460",
    "text": "#eaeaea", "muted": "#a0a0b0", "entry": "#0d1b2a",
    "gold": "#FDB927", "red": "#e94560",
}
OK_GREEN = "#4ade80"

FRIENDLY_COLOR_NAMES = {
    "_Color_Area_G":            "Painted Area",
    "_Color_Area_Line_R":       "Key Outline",
    "_Color_Floor_Court_B":     "Floor",
    "_Color_Outside_Court_R":   "Apron",
    "_Color_ThreePoint_Line_G": "3-Point Line",
    "_Color_Free_Throw_Line_B": "Free-Throw Circle",
    "_Color_Restricted_Line_A": "Restricted Arc",
}

NAV_EXPLAINER = (
    "NBA Bounce hardcodes which row of the team grid is the LAST one: past 32 "
    "teams, pressing Down in the middle of the grid jumps to Random instead of "
    "moving to the next row.\n\n"
    "That number lives in the game's code, not its assets, so fixing it means "
    "editing Assembly-CSharp.dll. Everything else in this app only touches "
    "asset files.\n\n"
    "What you should know before using it:\n"
    "  • A Steam update, or Verify Integrity of Game Files, restores the "
    "original code and silently undoes this.\n"
    "  • The original is backed up first, and Undo puts it back exactly.\n"
    "  • Re-apply it whenever you change how many teams you have."
)


class TeamTab(tk.Frame):
    def __init__(self, parent, game_data_path, host=None, theme=None,
                 on_rebuilt=None):
        self.T = dict(DEFAULT_THEME, **(theme or {}))
        super().__init__(parent, bg=self.T["bg"])
        self.game_data = game_data_path or ""
        self.host = host
        self.on_rebuilt = on_rebuilt

        self._adv = None
        self.teams = []
        self.art = {}
        self.stock_abbrs = set()
        self.bundled_count = 0
        self.selected = None
        self.color_vars = {}
        self.busy = False
        self.scale_val = tk.DoubleVar(value=1.0)
        self.nav_enable = tk.BooleanVar(value=False)

        self._build()
        if self.game_data:
            self.after(60, self.load)

    # ── layout ────────────────────────────────────────────────────────────
    def _build(self):
        T = self.T

        bar = tk.Frame(self, bg=T["bg"])
        bar.pack(fill="x", padx=10, pady=(10, 6))
        tk.Label(bar, text="Teams", bg=T["bg"], fg=T["text"],
                 font=("Segoe UI", 14, "bold")).pack(side="left")
        self.count_lbl = tk.Label(bar, text="", bg=T["bg"], fg=T["muted"],
                                  font=("Segoe UI", 9))
        self.count_lbl.pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="Refresh", command=self.load).pack(side="right")
        ttk.Button(bar, text="Remove Added Teams",
                   command=self.remove_added).pack(side="right", padx=6)
        ttk.Button(bar, text="Add Teams…", style="Accent.TButton",
                   command=self.add_dialog).pack(side="right")

        # Two sections: per-team editing, and the select screen as a whole.
        self.sections = ttk.Notebook(self)
        self.sections.pack(fill="both", expand=True, padx=10)
        body = tk.Frame(self.sections, bg=T["bg"])
        self.sections.add(body, text="  Edit Teams  ")
        self.layout = None
        if LayoutPanel is not None:
            self.layout = LayoutPanel(self.sections, self)
            self.sections.add(self.layout, text="  Layout  ")

        left = tk.Frame(body, bg=T["bg"])
        left.pack(side="left", fill="both", expand=False)
        cols = ("id", "kind")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings",
                                 height=18, selectmode="browse")
        self.tree.heading("#0", text="Team")
        self.tree.heading("id", text="ID")
        self.tree.heading("kind", text="Source")
        self.tree.column("#0", width=230, anchor="w")
        self.tree.column("id", width=50, anchor="center")
        self.tree.column("kind", width=70, anchor="center")
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.tag_configure("added", foreground=self.T["gold"])

        right = tk.Frame(body, bg=T["panel"], highlightbackground=T["accent"],
                         highlightthickness=1)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.editor = right

        self.title_lbl = tk.Label(right, text="Select a team",
                                  bg=T["panel"], fg=T["text"],
                                  font=("Segoe UI", 12, "bold"))
        self.title_lbl.pack(anchor="w", padx=14, pady=(12, 2))
        self.sub_lbl = tk.Label(right, text="", bg=T["panel"], fg=T["muted"],
                                font=("Segoe UI", 9))
        self.sub_lbl.pack(anchor="w", padx=14)

        # The game shows CITY and NICKNAME on the versus panel and the
        # ABBREVIATION on the scoreboard. m_Name is internal -- renaming only
        # that changes nothing a player can see.
        self.id_vars, self.id_hints = {}, {}
        for field, label in (("city", "City"), ("nickname", "Nickname"),
                             ("abbr", "Abbrev."), ("asset_name", "Asset name")):
            row = tk.Frame(right, bg=T["panel"])
            row.pack(fill="x", padx=14, pady=(8 if field == "city" else 2, 0))
            tk.Label(row, text=label, bg=T["panel"], fg=T["text"],
                     font=("Segoe UI", 10, "bold"), width=10,
                     anchor="w").pack(side="left")
            var = tk.StringVar()
            self.id_vars[field] = var
            entry = tk.Entry(row, textvariable=var, bg=T["entry"], fg=T["text"],
                             insertbackground=T["text"], relief="flat",
                             width=12 if field == "abbr" else 30)
            entry.pack(side="left", fill="x" if field != "abbr" else None,
                       expand=field != "abbr", ipady=3)
            hint = tk.Label(row, text="", bg=T["panel"], fg=T["muted"],
                            font=("Segoe UI", 8))
            hint.pack(side="left", padx=8)
            self.id_hints[field] = hint
            var.trace_add("write",
                          lambda *_a, f=field: self._update_id_hint(f))
        self.name_entry = None          # kept for _set_editor_state
        tk.Label(right,
                 text="The match-end screen reads a team's name from the "
                      "game's localization table, keyed on the abbreviation, so "
                      "a custom abbreviation shows *** nba.team_X *** there "
                      "until that entry exists. Everywhere else uses the names "
                      "above.\n\n"
                      "Shipped teams are renamed in place, so each field is "
                      "capped at the bytes it already uses — most are short "
                      "(Phoenix: city 12, nickname 4, abbr 4). Portland has "
                      "the roomiest slots in the game (22 / 13 / 3). For a "
                      "longer name, add a team instead: added teams have no "
                      "limit, and copying from Portland also gives the "
                      "longest jersey slot to borrow into.",
                 bg=T["panel"], fg=T["muted"], font=("Segoe UI", 8),
                 wraplength=460, justify="left").pack(anchor="w", padx=14,
                                                      pady=(4, 0))

        tk.Label(right, text="Court colors", bg=T["panel"], fg=T["text"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14,
                                                     pady=(14, 4))
        self.colors_frame = tk.Frame(right, bg=T["panel"])
        self.colors_frame.pack(fill="x", padx=14)

        tk.Label(right, text="Art", bg=T["panel"], fg=T["text"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14,
                                                     pady=(14, 4))
        self.art_frame = tk.Frame(right, bg=T["panel"])
        self.art_frame.pack(fill="x", padx=14)

        btns = tk.Frame(right, bg=T["panel"])
        btns.pack(fill="x", padx=14, pady=14)
        self.save_btn = ttk.Button(btns, text="Save Changes",
                                   style="Accent.TButton", command=self.save)
        self.save_btn.pack(side="left")
        ttk.Button(btns, text="Revert Fields",
                   command=self._fill_editor).pack(side="left", padx=8)
        ttk.Button(btns, text="Advanced…",
                   command=self.open_advanced).pack(side="right")

        self.status = tk.Label(self, text="", bg=T["bg"], fg=T["muted"],
                               font=("Segoe UI", 9), anchor="w")
        self.status.pack(fill="x", padx=12, pady=(4, 10))
        self._set_editor_state(False)

    def open_advanced(self):
        """Grid size and the navigation fix, in a window of their own.

        Not merely tidiness: these are the two settings that change the whole
        screen rather than one team, and the navigation one edits the game's
        code. Giving them their own window keeps them away from an accidental
        click and stops them being clipped off the bottom of a short window.
        """
        if getattr(self, "_adv", None) is not None:
            try:
                self._adv.lift()
                return
            except Exception:
                pass
        T = self.T
        win = tk.Toplevel(self)
        self._adv = win
        win.title("Advanced — Teams")
        win.configure(bg=T["panel"])
        win.resizable(False, False)
        win.transient(self.winfo_toplevel())

        def closed():
            self._adv = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", closed)
        box = tk.Frame(win, bg=T["panel"])
        box.pack(fill="both", expand=True)

        # grid scale -- an asset edit, safe, so it sits at the top
        row = tk.Frame(box, bg=T["panel"])
        row.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(row, text="Team-select grid size", bg=T["panel"], fg=T["text"],
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        self.scale_lbl = tk.Label(row, text="100%", bg=T["panel"],
                                  fg=T["muted"], font=("Segoe UI", 9), width=6)
        self.scale_lbl.pack(side="right")
        ttk.Button(row, text="Apply", width=7,
                   command=self.apply_scale).pack(side="right", padx=6)
        sc = ttk.Scale(box, from_=0.4, to=1.0, variable=self.scale_val,
                       command=lambda v: self.scale_lbl.configure(
                           text=f"{float(v) * 100:.0f}%"))
        sc.pack(fill="x", padx=10)
        tk.Label(box, text="Shrinks the whole grid so more teams fit on screen. "
                           "Asset edit — safe, and Apply at 100% restores stock.",
                 bg=T["panel"], fg=T["muted"], font=("Segoe UI", 8),
                 wraplength=420, justify="left").pack(anchor="w", padx=10,
                                                      pady=(2, 10))

        tk.Frame(box, bg=T["accent"], height=1).pack(fill="x", padx=10)

        # navigation patch -- edits game CODE, so it is guarded
        row2 = tk.Frame(box, bg=T["panel"])
        row2.pack(fill="x", padx=10, pady=(10, 2))
        tk.Label(row2, text="Grid navigation fix", bg=T["panel"], fg=T["text"],
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Label(row2, text="edits game code", bg=T["panel"], fg=T["red"],
                 font=("Segoe UI", 8, "bold")).pack(side="left", padx=8)
        ttk.Button(row2, text="What's this?", width=12,
                   command=self._explain_nav).pack(side="right")

        self.nav_status = tk.Label(box, text="", bg=T["panel"], fg=T["muted"],
                                   font=("Segoe UI", 8), wraplength=420,
                                   justify="left")
        self.nav_status.pack(anchor="w", padx=10, pady=(2, 6))

        row3 = tk.Frame(box, bg=T["panel"])
        row3.pack(fill="x", padx=10, pady=(0, 10))
        tk.Checkbutton(row3, text="I understand this edits the game's code",
                       variable=self.nav_enable, bg=T["panel"], fg=T["text"],
                       selectcolor=T["entry"], activebackground=T["panel"],
                       activeforeground=T["text"], font=("Segoe UI", 8),
                       command=self._sync_nav_buttons).pack(side="left")
        self.nav_undo_btn = ttk.Button(row3, text="Undo", width=8,
                                       command=self.nav_revert)
        self.nav_undo_btn.pack(side="right")
        self.nav_apply_btn = ttk.Button(row3, text="Apply Fix", width=10,
                                        command=self.nav_apply)
        self.nav_apply_btn.pack(side="right", padx=6)
        self._sync_nav_buttons()

    # ── state helpers ─────────────────────────────────────────────────────
    def _set_status(self, text, color=None):
        self.status.configure(text=text, fg=color or self.T["muted"])
        self.update_idletasks()

    def _set_editor_state(self, on):
        state = "normal" if on else "disabled"
        try:
            self.save_btn.configure(state=state)
        except Exception:
            pass

    def _sync_nav_buttons(self):
        on = "normal" if self.nav_enable.get() else "disabled"
        for b in (getattr(self, "nav_apply_btn", None),
                  getattr(self, "nav_undo_btn", None)):
            if b is None:
                continue
            try:
                b.configure(state=on)
            except Exception:
                pass

    def _run(self, work, done, busy_text):
        """Run a blocking job off the UI thread; `done` gets (result, error)."""
        if self.busy:
            return
        self.busy = True
        self._set_status(busy_text)

        def worker():
            try:
                res, err = work(), None
            except Exception as exc:                # surfaced as a message box
                res, err = None, exc
            self.after(0, lambda: self._finish(done, res, err))

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, done, res, err):
        self.busy = False
        done(res, err)

    def _host_path(self):
        """The host's current game path, if it has one.

        The tab is constructed with whatever path the app had at startup, which
        is an empty string on a fresh install. Settings tells us when that
        changes, but reading it back here as well means a missed notification
        degrades to a Refresh rather than a tab that never works.
        """
        cfg = getattr(self.host, "cfg", None)
        if isinstance(cfg, dict):
            return (cfg.get("game_data_path") or "").strip()
        return ""

    def _guard(self):
        if tmgr is None:
            messagebox.showerror("Teams unavailable",
                                 "team_manager.py could not be imported. It "
                                 "should sit in the modules folder next to "
                                 "team_tab.py.")
            return False
        if not self.game_data or not os.path.isdir(self.game_data):
            self.game_data = self._host_path()          # catch up if we missed it
        if not self.game_data or not os.path.isdir(self.game_data):
            messagebox.showwarning("No game path",
                                   "Set your game data folder in Settings first.")
            return False
        return True

    # ── loading ───────────────────────────────────────────────────────────
    def load(self, silent=False):
        if not self.game_data or not os.path.isdir(self.game_data):
            self.game_data = self._host_path()
        if silent and (not self.game_data or not os.path.isdir(self.game_data)):
            self._set_status("Set your game data folder in Settings, then press "
                             "Refresh.", self.T["gold"])
            return
        if not self._guard():
            return

        def done(res, err):
            if err:
                self._set_status(str(err), self.T["red"])
                return
            self.teams = res["teams"]
            self.art = res.get("art", {})
            # Read straight from the localization table rather than guessing
            # from the shipped teams: it is the same list the game consults.
            self.stock_abbrs = res.get("localized") or set()
            self.bundled_count = res["bundled_count"]
            self.tree.delete(*self.tree.get_children())
            for t in self.teams:
                self.tree.insert(
                    "", "end", iid=str(t["path_id"]), text=t["name"].strip(),
                    values=(t["unique_id"] if t["unique_id"] is not None else "?",
                            "added" if t["added"] else "stock"),
                    tags=("added",) if t["added"] else ())
            added = sum(1 for t in self.teams if t["added"])
            self.count_lbl.configure(
                text=f"{len(self.teams)} containers · {self.bundled_count} in the "
                     f"team list · {added} added by this app")
            self._set_status("Loaded.", OK_GREEN)
            self._refresh_advanced()
            self._refresh_layout()

        def work():
            res = tmgr.list_teams(self.game_data)
            try:
                res["art"] = tmgr.list_art(self.game_data)
            except Exception:
                res["art"] = {}          # art is a bonus; the list still works
            try:
                res["localized"] = tmgr.localized_abbreviations(self.game_data)
            except Exception:
                res["localized"] = set()
            return res

        self._run(work, done, "Scanning teams …")

    def _refresh_advanced(self):
        """Only meaningful while the Advanced window is open; the values are
        re-read each time it opens, so there is nothing to do otherwise."""
        if getattr(self, "_adv", None) is None:
            return
        try:
            self.scale_val.set(tmgr.get_grid_scale(self.game_data))
            self.scale_lbl.configure(text=f"{self.scale_val.get() * 100:.0f}%")
        except Exception:
            pass
        try:
            rep = tmgr.nav_report(self.game_data, self.bundled_count)
            rows = [r for r in rep["rows"] if r["at"] is not None]
            if not rows:
                msg = ("The game's code does not match what this fix expects — "
                       "it may already be patched, or the game was updated.")
            else:
                cur = rows[0]["current"]
                want = (rows[0]["start"], rows[0]["end"])
                state = ("already correct" if tuple(cur.values()) == want
                         else "needs the fix")
                msg = (f"Your team list has {self.bundled_count} teams. "
                       f"The grid's last row should be {want[0]}–{want[1]}; "
                       f"the game currently says "
                       f"{'–'.join(str(v) for v in cur.values())} ({state}).")
            if rep["has_backup"]:
                msg += "  A backup of the original code exists, so Undo works."
            self.nav_status.configure(text=msg)
        except Exception as exc:
            self.nav_status.configure(text=str(exc))

    def _refresh_layout(self):
        """Re-read the Layout section from disk after anything that could
        have changed the grid, the team list or the game's code."""
        if self.layout is not None and self.teams:
            self.layout.load(self.teams, self.bundled_count)

    # ── editing one team ──────────────────────────────────────────────────
    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        pid = int(sel[0])
        self.selected = next((t for t in self.teams if t["path_id"] == pid), None)
        self._fill_editor()

    def _fill_editor(self):
        t = self.selected
        for w in self.colors_frame.winfo_children():
            w.destroy()
        self.color_vars = {}
        if t is None:
            self._set_editor_state(False)
            return

        self.title_lbl.configure(text=t["name"].strip() or "(unnamed)")
        self.sub_lbl.configure(
            text=f"unique ID {t['unique_id']} · path_id {t['path_id']} · "
                 f"{'added by this app' if t['added'] else 'shipped with the game'}")
        if t.get("identity_error"):
            self.sub_lbl.configure(
                text=f"unique ID {t['unique_id']} · path_id {t['path_id']} · "
                     f"name fields unreadable ({t['identity_error']})")
        ident = t.get("identity", {})
        for field, var in self.id_vars.items():
            var.set((ident.get(field, {}) or {}).get("text", "").strip())
            self._update_id_hint(field)

        T = self.T
        for prop, hexval in sorted(t["colors"].items(),
                                   key=lambda kv: FRIENDLY_COLOR_NAMES.get(kv[0],
                                                                           kv[0])):
            row = tk.Frame(self.colors_frame, bg=T["panel"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text=FRIENDLY_COLOR_NAMES.get(prop, prop),
                     bg=T["panel"], fg=T["text"], font=("Segoe UI", 9),
                     width=18, anchor="w").pack(side="left")
            var = tk.StringVar(value=hexval)
            self.color_vars[prop] = var
            swatch = tk.Frame(row, bg=f"#{hexval}", width=26, height=18,
                              highlightbackground=T["accent"], highlightthickness=1)
            swatch.pack(side="left", padx=(0, 6))
            swatch.pack_propagate(False)
            entry = tk.Entry(row, textvariable=var, width=8, bg=T["entry"],
                             fg=T["text"], insertbackground=T["text"],
                             relief="flat")
            entry.pack(side="left", ipady=2)
            ttk.Button(row, text="Pick", width=6,
                       command=lambda v=var, s=swatch, p=prop:
                       self._pick(v, s, p)).pack(side="left", padx=6)
            var.trace_add("write",
                          lambda *_a, v=var, s=swatch: self._sync_swatch(v, s))
        self._fill_art(t)
        self._set_editor_state(True)


    def _fill_art(self, t):
        """Borrowing art is a pointer swap, so it is in-place and instant --
        but only for logos, the grid icon and the court materials. Jerseys are
        Resources.Load NAMES, and a name only fits its slot if it is within a
        few characters of the one already there, so those are offered and then
        honestly reported as skipped when they do not fit."""
        T = self.T
        for w in self.art_frame.winfo_children():
            w.destroy()
        info = self.art.get(t["name"])
        if not info:
            tk.Label(self.art_frame, text="Press Refresh to read this team's art.",
                     bg=T["panel"], fg=T["muted"],
                     font=("Segoe UI", 8)).pack(anchor="w")
            return

        bits = [f"{info['logo_count']} logo sprite(s)"]
        if info.get("icon"):
            bits.append(f"icon {info['icon']}")
        if info.get("jersey"):
            bits.append(f"jerseys {info['jersey']}")
        tk.Label(self.art_frame, text=" · ".join(bits), bg=T["panel"],
                 fg=T["muted"], font=("Segoe UI", 8), wraplength=460,
                 justify="left", anchor="w").pack(fill="x")

        shares = info.get("shares_with") or []
        if shares:
            tk.Label(self.art_frame,
                     text=("Sharing art with " + ", ".join(shares[:3])
                           + (f" (+{len(shares) - 3} more)" if len(shares) > 3
                              else "")
                           + " — replacing one of these textures changes all of "
                             "them."),
                     bg=T["panel"], fg=T["gold"], font=("Segoe UI", 8),
                     wraplength=460, justify="left", anchor="w").pack(fill="x",
                                                                      pady=(2, 0))

        row = tk.Frame(self.art_frame, bg=T["panel"])
        row.pack(fill="x", pady=(6, 2))
        tk.Label(row, text="Borrow from", bg=T["panel"], fg=T["text"],
                 font=("Segoe UI", 9)).pack(side="left")
        others = sorted(n.strip() for n in self.art if n != t["name"])
        self.donor_var = tk.StringVar(value=others[0] if others else "")
        ttk.Combobox(row, textvariable=self.donor_var, values=others,
                     state="readonly", width=26).pack(side="left", padx=6)
        ttk.Button(row, text="Preview", width=8,
                   command=lambda: self._borrow(preview=True)).pack(side="left")
        ttk.Button(row, text="Borrow", width=8,
                   command=lambda: self._borrow(preview=False)).pack(side="left",
                                                                     padx=4)
        ttk.Button(row, text="Detach", width=8,
                   command=self._detach).pack(side="left")

        picks = tk.Frame(self.art_frame, bg=T["panel"])
        picks.pack(fill="x")
        self.part_vars = {}
        for key, label in (("logos", "Logos"), ("icon", "Grid icon"),
                           ("court", "Court"), ("jerseys", "Jerseys")):
            v = tk.BooleanVar(value=key != "jerseys")
            self.part_vars[key] = v
            tk.Checkbutton(picks, text=label, variable=v, bg=T["panel"],
                           fg=T["text"], selectcolor=T["entry"],
                           activebackground=T["panel"], activeforeground=T["text"],
                           font=("Segoe UI", 8)).pack(side="left")
        tk.Label(self.art_frame,
                 text="Logos, icon and court are pointer swaps — instant and "
                      "safe. Jerseys are loaded by name, so one only fits when "
                      "it is close in length to the name already there; any that "
                      "do not fit are reported and left alone.",
                 bg=T["panel"], fg=T["muted"], font=("Segoe UI", 8),
                 wraplength=460, justify="left", anchor="w").pack(fill="x",
                                                                  pady=(2, 0))


    def _detach(self):
        """Give this team art of its own.

        Two different mechanisms behind one button, and the dialog says which is
        which because the difference decides what can be edited afterwards:

        Logos and the grid icon are POINTERS. Copying them is contained inside
        sharedassets1.assets.

        Jerseys, the court and the boards are NAMES the game resolves through
        Resources.Load. Copying one means a new texture in resources.assets, a
        new entry in the resource table in globalgamemanagers, and the team's
        name string repointed -- three files, in that order, so the game is
        never left pointing at a name that does not resolve yet.
        """
        if not self._guard() or self.selected is None:
            return
        t = self.selected
        info = self.art.get(t["name"], {})
        shares = info.get("shares_with") or []
        T = self.T

        dlg = tk.Toplevel(self)
        dlg.title("Detach art")
        dlg.configure(bg=T["bg"])
        dlg.resizable(False, False)
        dlg.transient(self.winfo_toplevel())
        tk.Label(dlg, text=f"Give '{t['name'].strip()}' its own art",
                 bg=T["bg"], fg=T["text"], font=("Segoe UI", 11, "bold")
                 ).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(dlg, bg=T["bg"], fg=T["gold"] if shares else T["muted"],
                 font=("Segoe UI", 8), wraplength=430, justify="left",
                 text=("Currently shares art with " + ", ".join(shares[:3])
                       + ". Replacing one of those textures changes every team "
                         "using it." if shares else
                       "This team's art is not shared with another team.")
                 ).pack(anchor="w", padx=16, pady=(0, 10))

        want_logos = tk.BooleanVar(value=True)
        want_res = tk.BooleanVar(value=True)
        for var, label, detail in (
                (want_logos, "Logos and grid icon",
                 "Pointer copies inside sharedassets1.assets."),
                (want_res, "Jerseys, court and boards",
                 "Copies into resources.assets plus new entries in the "
                 "resource table, so Resources.Load can find them.")):
            tk.Checkbutton(dlg, text=label, variable=var, bg=T["bg"],
                           fg=T["text"], selectcolor=T["entry"],
                           activebackground=T["bg"], activeforeground=T["text"],
                           font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14)
            tk.Label(dlg, text=detail, bg=T["bg"], fg=T["muted"],
                     font=("Segoe UI", 8), wraplength=400, justify="left"
                     ).pack(anchor="w", padx=40, pady=(0, 8))

        tk.Label(dlg, bg=T["bg"], fg=T["gold"], font=("Segoe UI", 8),
                 wraplength=430, justify="left",
                 text="The copies are renamed CUSTOM_<team>_… so the Textures "
                      "tab's search finds them, and registered as mods so their "
                      "pixels stay valid. Game files are rebuilt, so your queued "
                      "mods are re-applied afterwards — close NBA Bounce and "
                      "quit Steam first."
                 ).pack(anchor="w", padx=16, pady=(4, 10))

        row = tk.Frame(dlg, bg=T["bg"])
        row.pack(pady=(0, 14))

        def go():
            parts = (want_logos.get(), want_res.get())
            dlg.destroy()
            if any(parts):
                self._run_detach(*parts)

        ttk.Button(row, text="Detach", style="Accent.TButton",
                   command=go).pack(side="left", padx=6)
        ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side="left")
        dlg.grab_set()

    def _run_detach(self, do_logos, do_resources):
        t = self.selected
        pid = t["path_id"]
        note = lambda m: self.after(0, lambda: self._set_status(m))

        def work():
            out = {"rebuilt": [], "parts": []}
            if do_logos:
                r = tmgr.detach_art(self.game_data, pid, progress=note)
                out["parts"].append(("logos", r))
                out["rebuilt"] += r["rebuilt"]
            if do_resources:
                r = tmgr.detach_resource_art(self.game_data, pid, progress=note)
                out["parts"].append(("jerseys, court and boards", r))
                out["rebuilt"] += r["rebuilt"]
            # Register every copy as a mod before anything re-appends a .resS,
            # or the copies end up pointing at pixels that have moved.
            adopt = getattr(self.host, "adopt_detached_textures", None)
            out["adopted"] = 0
            if callable(adopt):
                for _label, r in out["parts"]:
                    ids = r.get("new_texture_ids") or []
                    if ids:
                        out["adopted"] += adopt(r["assets_path"], ids)
            return out

        def done(res, err):
            if err:
                messagebox.showerror("Could not detach art", str(err))
                self._set_status(str(err), self.T["red"])
                return
            prefix = next((r.get("search_prefix") for _l, r in res["parts"]
                           if r.get("search_prefix")), "CUSTOM_")
            lines = []
            for label, r in res["parts"]:
                if label == "logos":
                    lines.append(f"  • logos: {r['sprites']} sprite(s), "
                                 f"{r['textures']} texture(s)")
                else:
                    lines.append(f"  • {label}: {r['textures']} texture(s) — "
                                 + ", ".join(n.split('_')[1] for n in r["names"][:3]))
            self._set_status(
                f"Detached — {res['adopted']} texture(s) registered as mods. "
                f"Search '{prefix}' in the Textures tab.", OK_GREEN)
            messagebox.showinfo(
                "Art detached",
                "This team now owns:\n\n" + "\n".join(lines)
                + f"\n\nFind them in the Textures tab by searching\n\n"
                  f"        {prefix}\n\nUse PNGs whose width and height are "
                  f"multiples of 4 — the game cannot use BC7 compression "
                  f"otherwise and falls back to uncompressed.")
            self._after_rebuild(res)

        self._run(work, done, "Copying art …")

    def _borrow(self, preview):
        if not self._guard() or self.selected is None:
            return
        donor = self.donor_var.get().strip()
        parts = tuple(k for k, v in self.part_vars.items() if v.get())
        if not donor or not parts:
            messagebox.showinfo("Nothing to borrow",
                                "Pick a team to borrow from and at least one "
                                "kind of art.")
            return
        pid = self.selected["path_id"]

        def done(res, err):
            if err:
                messagebox.showerror("Could not borrow art", str(err))
                self._set_status(str(err), self.T["red"])
                return
            lines = res["detail"][:14]
            if len(res["detail"]) > 14:
                lines.append(f"… +{len(res['detail']) - 14} more")
            body = "\n".join(lines) or "Nothing would change."
            if res["skipped"]:
                body += "\n\nSkipped:\n" + "\n".join(res["skipped"][:6])
            if preview:
                messagebox.showinfo(
                    f"Borrowing from {donor}",
                    f"{res['changed']} value(s) would change.\n\n{body}")
                return
            self._set_status(f"Borrowed from {donor} — {res['changed']} "
                             f"value(s) written in place."
                             + (f" {len(res['skipped'])} skipped."
                                if res["skipped"] else ""), OK_GREEN)
            if res["skipped"]:
                messagebox.showinfo("Some art was left alone",
                                    "\n".join(res["skipped"][:8]))
            self.load()

        self._run(lambda: tmgr.borrow_art(self.game_data, pid, donor,
                                          parts=parts, dry_run=preview),
                  done, "Reading art …" if preview else "Borrowing art …")

    def _sync_swatch(self, var, swatch):
        val = var.get().strip().lstrip("#")
        try:
            swatch.configure(bg=f"#{val}")
        except Exception:
            pass

    def _pick(self, var, swatch, prop):
        cur = var.get().strip().lstrip("#")
        try:
            initial = f"#{cur}"
            rgb = colorchooser.askcolor(color=initial,
                                        title=FRIENDLY_COLOR_NAMES.get(prop, prop))
        except Exception:
            rgb = colorchooser.askcolor()
        if rgb and rgb[1]:
            var.set(rgb[1].lstrip("#"))
            self._sync_swatch(var, swatch)

    def _update_id_hint(self, field):
        t = self.selected
        hint = self.id_hints.get(field)
        if hint is None:
            return
        if not t:
            hint.configure(text="")
            return
        cap = (t.get("identity", {}).get(field, {}) or {}).get("capacity", 0)
        value = self.id_vars[field].get().strip()
        used = len(value.encode("utf-8"))
        over = used > cap
        text, colour = f"{used}/{cap}", self.T["red"] if over else self.T["muted"]
        if field == "abbr" and value and self.stock_abbrs:
            if value not in self.stock_abbrs:
                text += ("  ·  no localized name yet — Save offers to add one")
                if not over:
                    colour = self.T["gold"]
        hint.configure(text=text, fg=colour)

    def save(self):
        if not self._guard() or self.selected is None:
            return
        t = self.selected
        names = {f: v.get().strip() for f, v in self.id_vars.items()}
        colors = {p: v.get().strip().lstrip("#") for p, v in self.color_vars.items()}
        for prop, val in colors.items():
            try:
                tmgr.hex_to_rgba(val)
            except Exception as exc:
                messagebox.showerror("Bad color",
                                     f"{FRIENDLY_COLOR_NAMES.get(prop, prop)}: {exc}")
                return

        abbr = names.get("abbr", "").strip()
        display = names.get("city", "").strip() or t["name"].strip()
        add_loc = False
        if abbr and self.stock_abbrs and abbr not in self.stock_abbrs:
            add_loc = messagebox.askyesno(
                "Add a localized name?",
                f"'{abbr}' has no entry in the game's localization table, so "
                f"the match-end screen would read:\n\n"
                f"        *** nba.team_{abbr} ***\n\n"
                f"Add a record for it now, reading '{display}' in every "
                f"language?\n\nThis rebuilds resources.assets and re-applies "
                f"your queued mods, which takes a moment. Close NBA Bounce and "
                f"quit Steam first.")

        def work():
            done = tmgr.set_identity(self.game_data, t["path_id"], **names)
            done += tmgr.set_team_colors(self.game_data, t["path_id"], colors)
            out = {"written": done, "loc": None}
            if add_loc:
                out["loc"] = tmgr.set_localized_team_name(
                    self.game_data, abbr, display,
                    progress=lambda m: self.after(0, lambda: self._set_status(m)))
            return out

        def done(res, err):
            if err:
                messagebox.showerror("Could not save", str(err))
                self._set_status(str(err), self.T["red"])
                return
            loc = res.get("loc") if isinstance(res, dict) else None
            written = res["written"] if isinstance(res, dict) else res
            if loc:
                self._set_status(
                    f"Saved — {written} value(s) written in place, and "
                    f"'{loc['record']}' now reads your name in "
                    f"{loc['languages']} languages.", OK_GREEN)
                self._after_rebuild(loc)
                return
            self._set_status(f"Saved — {written} value(s) written in place. "
                             f"Your texture and audio mods are untouched.",
                             OK_GREEN)
            self.load()

        self._run(work, done, "Writing …")

    # ── adding and removing ───────────────────────────────────────────────
    def add_dialog(self):
        if not self._guard():
            return
        if not self.teams:
            messagebox.showinfo("No teams loaded", "Press Refresh first.")
            return
        T = self.T
        dlg = tk.Toplevel(self)
        dlg.title("Add Teams")
        dlg.configure(bg=T["bg"])
        dlg.transient(self.winfo_toplevel())
        dlg.resizable(False, False)

        tk.Label(dlg, text="New teams start as a copy of an existing one.",
                 bg=T["bg"], fg=T["text"], font=("Segoe UI", 10, "bold")
                 ).grid(row=0, column=0, columnspan=2, padx=14, pady=(14, 2),
                        sticky="w")
        tk.Label(dlg, text="The copy keeps the donor's logo, jerseys, court and "
                           "roster, so it is playable immediately. Rename and "
                           "recolor it here; swap its art later.",
                 bg=T["bg"], fg=T["muted"], font=("Segoe UI", 8),
                 wraplength=380, justify="left"
                 ).grid(row=1, column=0, columnspan=2, padx=14, pady=(0, 10),
                        sticky="w")

        def field(row, label, widget):
            tk.Label(dlg, text=label, bg=T["bg"], fg=T["text"],
                     font=("Segoe UI", 9)).grid(row=row, column=0, padx=(14, 6),
                                                pady=4, sticky="w")
            widget.grid(row=row, column=1, padx=(0, 14), pady=4, sticky="ew")

        # Donor choice decides the ART the new team starts with, and the
        # length of its jersey-name slot -- which is what limits borrowing
        # jerseys later. Portland ships the longest names in the game, so its
        # slots have the most room; that is worth saying rather than leaving
        # people to discover it.
        names = [t["name"].strip() for t in self.teams if not t["added"]]
        best = next((n for n in names if "Portland" in n),
                    names[0] if names else "")
        source = tk.StringVar(value=best)
        combo = ttk.Combobox(dlg, textvariable=source, values=names,
                             state="readonly", width=28)
        field(2, "Copy from", combo)
        donor_note = tk.Label(dlg, bg=T["bg"], fg=T["muted"],
                              font=("Segoe UI", 8), wraplength=380,
                              justify="left", anchor="w")
        donor_note.grid(row=3, column=1, padx=(0, 14), sticky="w")

        def describe_donor(*_a):
            info = self.art.get(source.get()) or {}
            ident = next((t.get("identity", {}) for t in self.teams
                          if t["name"].strip() == source.get()), {})
            caps = [f"{f} {ident.get(f, {}).get('capacity', '?')}"
                    for f in ("city", "nickname", "abbr") if ident]
            bits = []
            if info.get("logo_count"):
                bits.append(f"{info['logo_count']} logos")
            if info.get("jersey"):
                bits.append(f"jersey slot {len(info['jersey'])} chars")
            if caps:
                bits.append("rename room: " + ", ".join(caps))
            donor_note.configure(text=" · ".join(bits) or "")

        combo.bind("<<ComboboxSelected>>", describe_donor)
        describe_donor()
        count = tk.IntVar(value=1)
        field(4, "How many", tk.Spinbox(dlg, from_=1, to=64, textvariable=count,
                                        width=6, bg=T["entry"], fg=T["text"],
                                        relief="flat"))
        base = tk.StringVar(value="New Team")
        field(5, "City / top line", tk.Entry(dlg, textvariable=base,
                                             bg=T["entry"], fg=T["text"],
                                             insertbackground=T["text"],
                                             relief="flat"))
        nick = tk.StringVar(value="")
        field(6, "Nickname", tk.Entry(dlg, textvariable=nick, bg=T["entry"],
                                      fg=T["text"], insertbackground=T["text"],
                                      relief="flat"))
        abbr = tk.StringVar(value="")
        field(7, "Abbreviation", tk.Entry(dlg, textvariable=abbr, bg=T["entry"],
                                          fg=T["text"],
                                          insertbackground=T["text"],
                                          relief="flat"))
        color = tk.StringVar(value="00c853")
        field(8, "Color", tk.Entry(dlg, textvariable=color, bg=T["entry"],
                                   fg=T["text"], insertbackground=T["text"],
                                   relief="flat"))
        tk.Label(dlg, bg=T["bg"], fg=T["muted"], font=("Segoe UI", 8),
                 wraplength=380, justify="left",
                 text="City and nickname are what the versus panel shows; the "
                      "abbreviation is the scoreboard. No length limit here — "
                      "an added team is a new object, so '1996 Chicago Bulls' "
                      "and 'CHI96' are fine. Whatever you type becomes this "
                      "team's budget for later in-place edits, so leave a "
                      "little room if you expect to rename it. Blank nickname "
                      "or abbreviation are derived from the name."
                 ).grid(row=9, column=0, columnspan=2, padx=14, sticky="w")

        warn = tk.Label(
            dlg, bg=T["bg"], fg=T["gold"], font=("Segoe UI", 8),
            wraplength=380, justify="left",
            text="Adding teams rebuilds two game files. Every queued texture and "
                 "audio mod is re-applied automatically afterwards, which takes "
                 "a moment. Close NBA Bounce and quit Steam first, and back up "
                 "your save before playing with custom teams.")
        warn.grid(row=10, column=0, columnspan=2, padx=14, pady=(10, 6), sticky="w")

        row = tk.Frame(dlg, bg=T["bg"])
        row.grid(row=11, column=0, columnspan=2, pady=(0, 14))

        def go():
            src, n = source.get(), int(count.get())
            nm, col = base.get().strip() or "New Team", color.get().strip()
            try:
                tmgr.hex_to_rgba(col)
            except Exception as exc:
                messagebox.showerror("Bad color", str(exc), parent=dlg)
                return
            ab = abbr.get().strip()
            if ab and self.stock_abbrs and ab not in self.stock_abbrs:
                donor_abbr = next(
                    ((t.get("identity", {}).get("abbr", {}) or {}).get("text", "")
                     for t in self.teams if t["name"].strip() == src), "")
                if not messagebox.askyesno(
                        "Abbreviation has no localized name",
                        f"The match-end screen does not use the name you type. "
                        f"It looks up 'nba.team_{ab}' in the game's "
                        f"localization table, and there is no entry for "
                        f"'{ab}'.\n\nThat screen will read:\n\n"
                        f"        *** nba.team_{ab} ***\n\n"
                        f"Everywhere else — team select, the versus panel, the "
                        f"scoreboard — shows your own name correctly.\n\n"
                        f"This app can add that entry for you — it will do so "
                        f"right after the team is created, and the screen will "
                        f"read your own name.\n\n"
                        f"Or use '{donor_abbr}' to borrow the donor's name "
                        f"instead.\n\nContinue with '{ab}'?", parent=dlg):
                    return
            dlg.destroy()
            self._add(src, n, nm, col, nick.get().strip(), abbr.get().strip())

        ttk.Button(row, text="Add", style="Accent.TButton", command=go
                   ).pack(side="left", padx=6)
        ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side="left")
        dlg.columnconfigure(1, weight=1)
        dlg.grab_set()

    def _add(self, source, count, name, color, nickname="", abbr=""):
        def work():
            res = tmgr.add_teams(self.game_data, source, count=count,
                                  base_name=name, base_color=color,
                                  city=name or None,
                                  nickname=nickname or None,
                                  abbr=abbr or None,
                                  progress=lambda m: self.after(
                                      0, lambda: self._set_status(m)))
            # One team, one abbreviation, one localized name. With several
            # clones they share an abbreviation, so one record covers them all.
            if abbr and abbr not in (self.stock_abbrs or set()):
                try:
                    res["loc"] = tmgr.set_localized_team_name(
                        self.game_data, abbr, name,
                        progress=lambda m: self.after(
                            0, lambda: self._set_status(m)))
                except Exception as exc:
                    res["loc_error"] = str(exc)
            return res

        def done(res, err):
            if err:
                messagebox.showerror("Could not add teams", str(err))
                self._set_status(str(err), self.T["red"])
                return
            note = ""
            if res.get("loc"):
                note = (f" '{res['loc']['record']}' reads your name in "
                        f"{res['loc']['languages']} languages.")
            elif res.get("loc_error"):
                note = f" (localized name failed: {res['loc_error']})"
            self._set_status(f"Added {res['added']} team(s). "
                             f"The team list now holds {res['bundled_count']}."
                             + note, OK_GREEN)
            if res.get("loc"):
                res = dict(res, rebuilt=res["rebuilt"] + res["loc"]["rebuilt"])
            self._after_rebuild(res)

        self._run(work, done, "Adding teams …")

    def remove_added(self):
        if not self._guard():
            return
        if not messagebox.askyesno(
                "Remove added teams",
                "This restores the game files from the backup taken before the "
                "first team was added, removing every team this app added.\n\n"
                "Your queued texture and audio mods are re-applied afterwards.\n\n"
                "If a save game used a custom team, back it up first — the game "
                "looks up saved team IDs without checking they still exist.\n\n"
                "Continue?"):
            return

        def done(res, err):
            if err:
                messagebox.showerror("Could not remove teams", str(err))
                self._set_status(str(err), self.T["red"])
                return
            # The CUSTOM_* copies went with the teams. Their mod entries have to
            # go too: new copies start numbering from the same base, so a stale
            # entry would re-apply an old team's logo to an unrelated new one.
            dropped = []
            purge = getattr(self.host, "purge_orphaned_mods", None)
            if callable(purge):
                try:
                    dropped = purge(res.get("rebuilt"))
                except Exception:
                    dropped = []
            self._set_status(
                "Added teams removed."
                + (f" {len(dropped)} custom-art mod(s) dropped with them."
                   if dropped else ""), OK_GREEN)
            if dropped:
                messagebox.showinfo(
                    "Custom art removed too",
                    "These mods pointed at art that belonged to the teams you "
                    "just removed, so they were taken out of your mod list:\n\n"
                    + "\n".join(f"  • {n}" for n in dropped[:12])
                    + ("" if len(dropped) <= 12
                       else f"\n  … +{len(dropped) - 12} more")
                    + "\n\nThe PNGs are still in your mods folder if you want "
                      "to reuse them.")
            self._after_rebuild(res)

        self._run(lambda: tmgr.remove_added_teams(
            self.game_data,
            progress=lambda m: self.after(0, lambda: self._set_status(m))),
            done, "Restoring game files …")

    def _after_rebuild(self, res):
        """Both files were re-serialized, so every byte offset in them moved.
        Mods are stored per object, not per offset, so re-applying them from
        their saved sources is enough -- but it has to actually happen."""
        if res and res.get("rebuilt") and callable(self.on_rebuilt):
            self._set_status("Re-applying your texture and audio mods …")
            try:
                n = self.on_rebuilt()
                self._set_status(
                    f"Done. {n if isinstance(n, int) else 'All'} queued mod(s) "
                    f"re-applied — file offsets moved when the files were "
                    f"rebuilt.", OK_GREEN)
            except Exception as exc:
                messagebox.showwarning(
                    "Mods need re-applying",
                    f"The team change worked, but re-applying your queued mods "
                    f"failed:\n\n{exc}\n\nOpen the Textures tab and press Apply "
                    f"All Mods.")
        self.load()

    # ── advanced ──────────────────────────────────────────────────────────
    def apply_scale(self):
        if not self._guard():
            return
        val = round(float(self.scale_val.get()), 3)

        def done(res, err):
            if err:
                messagebox.showerror("Could not resize the grid", str(err))
                self._set_status(str(err), self.T["red"])
                return
            self._set_status(
                f"Team-select grid set to {val * 100:.0f}%."
                if res else "Grid is already that size.", OK_GREEN)
            self._refresh_layout()

        self._run(lambda: tmgr.set_grid_scale(self.game_data, val), done,
                  "Resizing the team grid …")

    def _explain_nav(self):
        messagebox.showinfo("Grid navigation fix", NAV_EXPLAINER)

    def nav_apply(self):
        if not self._guard():
            return
        if not messagebox.askyesno(
                "Edit the game's code?",
                f"This patches Assembly-CSharp.dll so the team grid's last row "
                f"matches your {self.bundled_count} teams.\n\n"
                f"The original is backed up first and Undo restores it exactly. "
                f"A Steam update or Verify Integrity also restores it, which "
                f"undoes this fix.\n\nContinue?"):
            return

        def done(res, err):
            if err:
                messagebox.showerror("Could not apply the fix", str(err))
                self._set_status(str(err), self.T["red"])
                return
            self._set_status(
                f"Navigation fix applied ({res['changed']} value(s))."
                if res["changed"] else "The code already holds these values.",
                OK_GREEN)
            self._refresh_advanced()
            self._refresh_layout()

        self._run(lambda: tmgr.nav_patch(self.game_data, self.bundled_count),
                  done, "Patching …")

    def nav_revert(self):
        if not self._guard():
            return

        def done(res, err):
            if err:
                messagebox.showerror("Could not undo", str(err))
                self._set_status(str(err), self.T["red"])
                return
            self._set_status("Original game code restored.", OK_GREEN)
            self._refresh_advanced()
            self._refresh_layout()

        self._run(lambda: tmgr.nav_revert(self.game_data), done, "Restoring …")

    # ── host hooks ────────────────────────────────────────────────────────
    def set_game_path(self, path):
        self.game_data = path or ""
        if self.game_data:
            self.load()


def open_team_manager(parent, game_data_path, theme=None, on_rebuilt=None):
    """Dialog fallback, mirroring open_gameplay_sliders()."""
    T = dict(DEFAULT_THEME, **(theme or {}))
    win = tk.Toplevel(parent)
    win.title("Teams — NBA Bounce Mod Manager")
    win.configure(bg=T["bg"])
    win.geometry("1000x680")
    TeamTab(win, game_data_path, host=parent, theme=T,
            on_rebuilt=on_rebuilt).pack(fill="both", expand=True)
    return win


if __name__ == "__main__":
    root = tk.Tk()
    root.title("Teams (standalone)")
    root.geometry("1000x680")
    root.configure(bg=DEFAULT_THEME["bg"])
    TeamTab(root, sys.argv[1] if len(sys.argv) > 1 else "").pack(
        fill="both", expand=True)
    root.mainloop()
