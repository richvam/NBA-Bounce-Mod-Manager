"""
team_layout.py -- the Layout section of the Teams tab.

Shows a live mockup of the team-select screen, drawn from the game files as
they are, and lets the user change where the grid sits, how many columns it
has and what order the teams appear in. Like team_tab.py it is only tkinter:
every byte it changes goes through team_manager.py, and it never imports
app.py or calls apply_single_mod().

WHAT EACH CONTROL TOUCHES
-------------------------
    Position        level2, in place (the GridLayOut strip)       asset edit
    Team order      level1, in place (m_aoBundledTeams)           asset edit
                    + the grid's nickname sort switched off        CODE edit
    Columns         Assembly-CSharp.dll, same-length byte swaps    CODE edit
    Reset           every team file back to stock, save repaired

Anything that edits code needs the consent box ticked, the same line the
Advanced window draws for the navigation fix.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk

try:
    import team_manager as tmgr
except Exception:                                   # standalone / broken install
    tmgr = None

OK_GREEN = "#4ade80"
SCREEN_BG = "#0b1020"
TILE_FALLBACK = "#3a4256"
CFG_DLC_KEY = "hardwoods_dlc"


def _hex(text):
    """'#rrggbb' from team_manager's bare 'rrggbb' (or None if unusable)."""
    if not isinstance(text, str):
        return None
    text = text.strip().lstrip("#")
    if len(text) == 8:                       # rrggbbaa
        text = text[:6]
    if len(text) != 6:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return "#" + text.lower()


def _luma(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b


def _tile_label(team):
    """Short text for a tile: the scoreboard abbreviation when the container
    has a readable one, else the first letters of the team's own name."""
    ident = team.get("identity") or {}
    abbr = ((ident.get("abbr") or {}).get("text") or "").strip()
    if abbr:
        return abbr.upper()
    name = team.get("name", "").strip()
    head, _sep, tail = name.partition("_")
    if head.isdigit() and tail:
        name = tail
    words = name.split()
    return (words[-1][:3] if words else "?").upper()


class LayoutPanel(tk.Frame):
    """Lives inside TeamTab; borrows its theme, worker thread and host."""

    def __init__(self, parent, tab):
        self.tab = tab
        self.T = tab.T
        super().__init__(parent, bg=self.T["bg"])

        self.teams_by_pid = {}
        self.bundled_count = 0
        self.current = None           # read_layout() as the files stand
        self.pending = None           # what Apply would write
        self._drag = None
        self._rects = []              # (x0, y0, x1, y1, kind, team) on canvas
        self._selected_pid = None
        self._syncing = False

        cfg = getattr(tab.host, "cfg", None)
        dlc = bool(cfg.get(CFG_DLC_KEY)) if isinstance(cfg, dict) else False
        self.dlc_var = tk.BooleanVar(value=dlc)
        self.view_var = tk.StringVar(value="pending")
        self.order_mode = tk.StringVar(value="alpha")
        self.cols_var = tk.IntVar(value=8)
        self.pos_x = tk.DoubleVar(value=0.0)
        self.pos_y = tk.DoubleVar(value=0.0)
        self.consent = tk.BooleanVar(value=False)

        self._build()

    # ── layout ────────────────────────────────────────────────────────────
    def _build(self):
        T = self.T
        left = tk.Frame(self, bg=T["bg"])
        left.pack(side="left", fill="both", expand=True)

        top = tk.Frame(left, bg=T["bg"])
        top.pack(fill="x", pady=(0, 4))
        tk.Label(top, text="Preview:", bg=T["bg"], fg=T["muted"],
                 font=("Segoe UI", 9)).pack(side="left")
        for val, label in (("current", "In game now"),
                           ("pending", "With my changes")):
            tk.Radiobutton(top, text=label, value=val, variable=self.view_var,
                           command=self.redraw, bg=T["bg"], fg=T["text"],
                           selectcolor=T["entry"], activebackground=T["bg"],
                           activeforeground=T["text"],
                           font=("Segoe UI", 9)).pack(side="left", padx=(6, 0))
        tk.Checkbutton(top, text="I own the HardWoods DLC", variable=self.dlc_var,
                       command=self._dlc_changed, bg=T["bg"], fg=T["text"],
                       selectcolor=T["entry"], activebackground=T["bg"],
                       activeforeground=T["text"],
                       font=("Segoe UI", 9)).pack(side="right")

        self.canvas = tk.Canvas(left, bg=T["bg"], highlightthickness=0,
                                width=640, height=360)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)

        self.info = tk.Label(left, text="", bg=T["bg"], fg=T["muted"],
                             font=("Segoe UI", 8), anchor="w", justify="left",
                             wraplength=640)
        self.info.pack(fill="x", pady=(4, 0))

        right = tk.Frame(self, bg=T["panel"], highlightbackground=T["accent"],
                         highlightthickness=1, width=330)
        right.pack(side="left", fill="y", padx=(10, 0))
        right.pack_propagate(False)
        self.controls = right

        def header(text, tag=None, tag_color=None):
            row = tk.Frame(right, bg=T["panel"])
            row.pack(fill="x", padx=12, pady=(10, 2))
            tk.Label(row, text=text, bg=T["panel"], fg=T["text"],
                     font=("Segoe UI", 10, "bold")).pack(side="left")
            if tag:
                tk.Label(row, text=tag, bg=T["panel"], fg=tag_color or T["muted"],
                         font=("Segoe UI", 8, "bold")).pack(side="left", padx=6)
            return row

        # position ---------------------------------------------------------
        row = header("Grid position", "asset edit", OK_GREEN)
        ttk.Button(row, text="Center", width=7,
                   command=self._center).pack(side="right")
        for label, var, lo, hi in (("Left / right", self.pos_x, -500, 500),
                                   ("Down / up", self.pos_y, -400, 300)):
            r = tk.Frame(right, bg=T["panel"])
            r.pack(fill="x", padx=12)
            tk.Label(r, text=label, bg=T["panel"], fg=T["muted"], width=11,
                     anchor="w", font=("Segoe UI", 8)).pack(side="left")
            val = tk.Label(r, text="0", bg=T["panel"], fg=T["muted"], width=5,
                           font=("Segoe UI", 8))
            val.pack(side="right")
            ttk.Scale(r, from_=lo, to=hi, variable=var,
                      command=lambda v, lab=val: self._pos_moved(v, lab)
                      ).pack(side="left", fill="x", expand=True)
            var._label = val

        # columns ----------------------------------------------------------
        row = header("Columns", "edits game code", T["red"])
        self.cols_spin = tk.Spinbox(row, from_=tmgr.MIN_COLUMNS if tmgr else 3,
                                    to=8, width=4, textvariable=self.cols_var,
                                    command=self._cols_changed, justify="center",
                                    bg=T["entry"], fg=T["text"],
                                    buttonbackground=T["accent"],
                                    insertbackground=T["text"], relief="flat")
        self.cols_spin.pack(side="right")
        self.cols_spin.bind("<KeyRelease>", lambda _e: self._cols_changed())
        self.cols_hint = tk.Label(right, text="", bg=T["panel"], fg=T["muted"],
                                  font=("Segoe UI", 8), wraplength=300,
                                  justify="left")
        self.cols_hint.pack(anchor="w", padx=12)

        # order ------------------------------------------------------------
        header("Team order")
        for val, label in (("alpha", "Alphabetical by nickname (game default)"),
                           ("custom", "Custom order  — edits game code")):
            tk.Radiobutton(right, text=label, value=val,
                           variable=self.order_mode, command=self._order_mode,
                           bg=T["panel"], fg=T["text"], selectcolor=T["entry"],
                           activebackground=T["panel"],
                           activeforeground=T["text"], anchor="w",
                           font=("Segoe UI", 8)).pack(fill="x", padx=12)
        lf = tk.Frame(right, bg=T["panel"])
        lf.pack(fill="both", expand=True, padx=12, pady=(4, 0))
        self.order_list = tk.Listbox(lf, height=8, bg=T["entry"], fg=T["text"],
                                     selectbackground=T["accent"],
                                     highlightthickness=0, relief="flat",
                                     activestyle="none", font=("Segoe UI", 9),
                                     exportselection=False)
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.order_list.yview)
        self.order_list.configure(yscrollcommand=sb.set)
        self.order_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.order_list.bind("<<ListboxSelect>>", self._list_select)
        ob = tk.Frame(right, bg=T["panel"])
        ob.pack(fill="x", padx=12, pady=(4, 0))
        self.order_btns = []
        for text, cmd in (("▲", lambda: self._move(-1)),
                          ("▼", lambda: self._move(1)),
                          ("A–Z", self._sort_alpha),
                          ("By ID", self._sort_id)):
            b = ttk.Button(ob, text=text, width=6, command=cmd)
            b.pack(side="left", padx=(0, 4))
            self.order_btns.append(b)
        tk.Label(right, text="Drag tiles in the preview to reorder them.",
                 bg=T["panel"], fg=T["muted"], font=("Segoe UI", 8)
                 ).pack(anchor="w", padx=12)

        # apply ------------------------------------------------------------
        tk.Frame(right, bg=T["accent"], height=1).pack(fill="x", padx=12,
                                                       pady=(10, 6))
        self.summary = tk.Label(right, text="No changes.", bg=T["panel"],
                                fg=T["muted"], font=("Segoe UI", 8),
                                wraplength=300, justify="left")
        self.summary.pack(anchor="w", padx=12)
        self.consent_cb = tk.Checkbutton(
            right, text="I understand this edits the game's code",
            variable=self.consent, command=self._sync_buttons,
            bg=T["panel"], fg=T["text"], selectcolor=T["entry"],
            activebackground=T["panel"], activeforeground=T["text"],
            font=("Segoe UI", 8))
        self.consent_cb.pack(anchor="w", padx=12, pady=(4, 0))
        bb = tk.Frame(right, bg=T["panel"])
        bb.pack(fill="x", padx=12, pady=(6, 4))
        self.apply_btn = ttk.Button(bb, text="Apply Layout",
                                    style="Accent.TButton", command=self.apply)
        self.apply_btn.pack(side="left")
        ttk.Button(bb, text="Discard", command=self.discard).pack(side="left",
                                                                  padx=6)
        ttk.Button(right, text="Reset Everything to Stock…",
                   command=self.reset_all).pack(fill="x", padx=12, pady=(4, 12))

        self._set_enabled(False)

    # ── data ──────────────────────────────────────────────────────────────
    def load(self, teams, bundled_count):
        """Called by TeamTab after every scan. Reads the layout off the disk
        on the tab's worker thread, then redraws."""
        self.teams_by_pid = {t["path_id"]: t for t in teams}
        self.bundled_count = bundled_count
        if tmgr is None or not self.tab.game_data:
            return

        def work():
            return tmgr.read_layout(self.tab.game_data, teams)

        def done(res, err):
            if err:
                self.current = None
                self.info.configure(text=f"Could not read the layout: {err}",
                                    fg=self.T["red"])
                self._set_enabled(False)
                self.redraw()
                return
            self.current = res
            self.discard()
            self.tab._set_status("Loaded.", OK_GREEN)

        self.tab._run(work, done, "Reading the team-select layout …")

    def _grid_key(self):
        return tmgr.MATRIX_HARDWOODS if self.dlc_var.get() else tmgr.MATRIX_STANDARD

    def _code(self):
        return (self.current or {}).get("code")

    def discard(self):
        """Pending := what is on disk."""
        if not self.current:
            return
        code = self._code()
        self.pending = {
            "order": list(self.current["order"]),
            "custom_order": bool(code and code["custom_order"]),
            "columns": dict(code["columns"]) if code else
            dict(tmgr.STOCK_COLUMNS),
            "position": tuple(self.current["position"]),
        }
        self._pull_controls()
        self._set_enabled(True)
        self.redraw()

    def _pull_controls(self):
        """Pending state -> widgets, without the widgets writing back."""
        p = self.pending
        self._syncing = True
        try:
            sx, sy = tmgr.STOCK_GRID_POSITION
            self.pos_x.set(round(p["position"][0] - sx))
            self.pos_y.set(round(p["position"][1] - sy))
            for var in (self.pos_x, self.pos_y):
                var._label.configure(text=f"{var.get():+.0f}")
            key = self._grid_key()
            self.cols_spin.configure(to=tmgr.MAX_COLUMNS[key])
            self.cols_var.set(p["columns"][key])
            self.order_mode.set("custom" if p["custom_order"] else "alpha")
            self._fill_list()
        finally:
            self._syncing = False

    def _fill_list(self):
        self.order_list.delete(0, "end")
        for pid in self._main_order():
            t = self.teams_by_pid.get(pid)
            if t is None:
                continue
            nick = tmgr.sort_key(t)
            tag = "  (added)" if t.get("added") else ""
            self.order_list.insert("end", f"{nick}{tag}")
        self._sync_selection()

    def _main_order(self):
        """Team path_ids in the order their tiles appear for the pending state."""
        p = self.pending
        if p is None:
            return []
        if p["custom_order"]:
            return list(p["order"])
        return [t["path_id"] for t in sorted(
            (self.teams_by_pid[x] for x in p["order"] if x in self.teams_by_pid),
            key=tmgr.sort_key)]

    # ── control callbacks ─────────────────────────────────────────────────
    def _dlc_changed(self):
        cfg = getattr(self.tab.host, "cfg", None)
        if isinstance(cfg, dict):
            cfg[CFG_DLC_KEY] = bool(self.dlc_var.get())
            saver = getattr(self.tab.host, "save_cfg", None)
            if callable(saver):
                try:
                    saver()
                except Exception:
                    pass
        if self.pending:
            self._pull_controls()
        self.redraw()

    def _pos_moved(self, value, label):
        label.configure(text=f"{float(value):+.0f}")
        if self._syncing or not self.pending:
            return
        sx, sy = tmgr.STOCK_GRID_POSITION
        self.pending["position"] = (sx + round(self.pos_x.get()),
                                    sy + round(self.pos_y.get()))
        self.redraw()

    def _center(self):
        if not self.pending:
            return
        self.pending["position"] = tuple(tmgr.STOCK_GRID_POSITION)
        self._pull_controls()
        self.redraw()

    def _cols_changed(self):
        if self._syncing or not self.pending:
            return
        key = self._grid_key()
        try:
            n = int(self.cols_var.get())
        except (tk.TclError, ValueError):
            return
        n = max(tmgr.MIN_COLUMNS, min(tmgr.MAX_COLUMNS[key], n))
        self.pending["columns"][key] = n
        self.redraw()

    def _order_mode(self):
        if self._syncing or not self.pending:
            return
        custom = self.order_mode.get() == "custom"
        if custom and not self.pending["custom_order"]:
            # Start the custom order from what the player sees today, so
            # switching modes does not reshuffle the grid by itself.
            self.pending["order"] = self._main_order()
        self.pending["custom_order"] = custom
        self._fill_list()
        self._sync_buttons()
        self.redraw()

    def _list_select(self, _e=None):
        sel = self.order_list.curselection()
        order = self._main_order()
        if sel and sel[0] < len(order):
            self._selected_pid = order[sel[0]]
            self.redraw()

    def _sync_selection(self):
        order = self._main_order()
        self.order_list.selection_clear(0, "end")
        if self._selected_pid in order:
            i = order.index(self._selected_pid)
            self.order_list.selection_set(i)
            self.order_list.see(i)

    def _move(self, step):
        if not self._custom_ok():
            return
        order = self.pending["order"]
        if self._selected_pid not in order:
            return
        i = order.index(self._selected_pid)
        j = max(0, min(len(order) - 1, i + step))
        if i != j:
            order.insert(j, order.pop(i))
            self._fill_list()
            self.redraw()

    def _sort_alpha(self):
        if not self._custom_ok():
            return
        self.pending["order"].sort(key=lambda p: tmgr.sort_key(self.teams_by_pid[p]))
        self._fill_list()
        self.redraw()

    def _sort_id(self):
        if not self._custom_ok():
            return
        # Stock IDs run -1 (Celtics) downward; added teams (-101 ...) last.
        self.pending["order"].sort(
            key=lambda p: -(self.teams_by_pid[p].get("unique_id") or 0))
        self._fill_list()
        self.redraw()

    def _custom_ok(self):
        return bool(self.pending and self.pending["custom_order"])

    # ── drag and drop on the preview ──────────────────────────────────────
    def _tile_at(self, x, y):
        for i, (x0, y0, x1, y1, kind, team) in enumerate(self._rects):
            if x0 <= x <= x1 and y0 <= y <= y1:
                return i, kind, team
        return None, None, None

    def _press(self, e):
        _i, kind, team = self._tile_at(e.x, e.y)
        if kind in ("team", "classic") and team is not None:
            self._selected_pid = team["path_id"]
            self._sync_selection()
            if kind == "team" and self._custom_ok() and \
                    self.view_var.get() == "pending":
                self._drag = {"pid": team["path_id"], "ghost": None}
            self.redraw()

    def _motion(self, e):
        if not self._drag:
            return
        c = self.canvas
        if self._drag["ghost"] is None:
            self._drag["ghost"] = c.create_rectangle(
                e.x - 20, e.y - 10, e.x + 20, e.y + 10,
                outline="#ffffff", width=2, dash=(3, 2))
        else:
            c.coords(self._drag["ghost"], e.x - 20, e.y - 10, e.x + 20, e.y + 10)

    def _release(self, e):
        drag, self._drag = self._drag, None
        if not drag:
            return
        if drag["ghost"] is not None:
            self.canvas.delete(drag["ghost"])
        _i, kind, team = self._tile_at(e.x, e.y)
        if kind != "team" or team is None or team["path_id"] == drag["pid"]:
            self.redraw()
            return
        order = self.pending["order"]
        src = order.index(drag["pid"])
        dst = order.index(team["path_id"])
        order.insert(dst, order.pop(src))
        self._fill_list()
        self.redraw()

    # ── drawing ───────────────────────────────────────────────────────────
    def _state_for_view(self):
        """(order, custom_order, columns, position, scale) for the chosen view."""
        cur = self.current
        if cur is None:
            return None
        key = self._grid_key()
        if self.view_var.get() == "current" or self.pending is None:
            code = self._code()
            cols = (code["columns"] if code else tmgr.STOCK_COLUMNS)[key]
            return (cur["order"], bool(code and code["custom_order"]), cols,
                    cur["position"], cur["scale"])
        p = self.pending
        return (p["order"], p["custom_order"], p["columns"][key],
                p["position"], cur["scale"])

    def redraw(self):
        c = self.canvas
        c.delete("all")
        self._rects = []
        if tmgr is None:
            return
        W, H = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        SW, SH = tmgr.CANVAS_SIZE
        f = min((W - 8) / SW, (H - 8) / SH)
        ox, oy = (W - SW * f) / 2, (H - SH * f) / 2

        def X(v):
            return ox + v * f

        def Y(v):
            return oy + v * f

        c.create_rectangle(X(0), Y(0), X(SW), Y(SH), fill=SCREEN_BG,
                           outline=self.T["accent"])
        state = self._state_for_view()
        if state is None:
            c.create_text(W / 2, H / 2, fill=self.T["muted"],
                          text="Set the game folder and press Refresh.",
                          font=("Segoe UI", 10))
            return
        order, custom, cols, pos, scale = state

        # fixed scenery, so the grid has something to be judged against
        faint = "#2a3350"
        c.create_text(X(SW / 2), Y(40), text="SELECT TEAM", fill="#56607e",
                      font=("Segoe UI", max(7, int(26 * f)), "bold"))
        c.create_rectangle(X(135), Y(80), X(465), Y(460), outline=faint, dash=(4, 3))
        c.create_text(X(300), Y(270), text="P1", fill=faint,
                      font=("Segoe UI", max(7, int(40 * f)), "bold"))
        c.create_rectangle(X(SW - 465), Y(80), X(SW - 135), Y(460), outline=faint,
                           dash=(4, 3))
        c.create_text(X(SW - 300), Y(270), text="P2", fill=faint,
                      font=("Segoe UI", max(7, int(40 * f)), "bold"))
        c.create_oval(X(962 - 150), Y(298 - 150), X(962 + 150), Y(298 + 150),
                      outline=faint, dash=(4, 3))

        # the strip GridLayOut occupies, after any position change
        dy = pos[1] - tmgr.STOCK_GRID_POSITION[1]
        top = SH - tmgr.GRID_STRIP_HEIGHT - dy
        c.create_rectangle(X(pos[0]), Y(top), X(SW + pos[0]), Y(SH - dy),
                           outline="#34406a", dash=(2, 4))

        hw = self.dlc_var.get()
        key = self._grid_key()
        tiles = tmgr.tile_sequence(self.teams_by_pid, order,
                                   self.current["hardwoods"], custom, hw)
        rects = tmgr.grid_geometry(len(tiles), cols, tmgr.RUNTIME_CELL[key],
                                   scale, pos)
        off = 0
        fsize = max(6, int(22 * f * scale))
        isize = max(5, int(12 * f * scale))
        for i, ((kind, team), (x0, y0, x1, y1)) in enumerate(zip(tiles, rects)):
            outside = x0 < 0 or y0 < 0 or x1 > SW or y1 > SH
            off += outside
            if kind == "random":
                fill, label = "#4b5563", "RANDOM"
            else:
                colors = team.get("colors") or {}
                fill = _hex(colors.get("_Color_Area_G")) or next(
                    (h for h in map(_hex, colors.values()) if h), TILE_FALLBACK)
                label = _tile_label(team)
            sel = team is not None and team["path_id"] == self._selected_pid
            outline = (self.T["red"] if outside else
                       "#ffffff" if sel else
                       self.T["gold"] if kind == "classic" or
                       (team and team.get("added")) else "#0b0f1a")
            c.create_rectangle(X(x0), Y(y0), X(x1), Y(y1), fill=fill,
                               outline=outline, width=3 if (sel or outside) else 1)
            fg = "#111111" if _luma(fill) > 150 else "#ffffff"
            c.create_text(X((x0 + x1) / 2), Y((y0 + y1) / 2), text=label,
                          fill=fg, font=("Segoe UI", fsize, "bold"))
            c.create_text(X(x0) + 3, Y(y0) + 2, text=str(i), anchor="nw",
                          fill=fg, font=("Segoe UI", isize))
            self._rects.append((X(x0), Y(y0), X(x1), Y(y1), kind, team))

        view = "in game now" if self.view_var.get() == "current" else \
            "with your changes"
        rows = -(-len(tiles) // cols)
        parts = [f"Showing {view}: {len(tiles)} tiles ({len(tiles) - 1} teams + "
                 f"Random) in {cols} columns × {rows} rows, grid at "
                 f"{scale * 100:.0f}%, "
                 f"{'custom order' if custom else 'alphabetical by nickname'}."]
        if off:
            parts.append(f"⚠ {off} tile(s) fall off the screen — shrink the grid "
                         f"in Advanced… or move it.")
        if self.current and self.current.get("code_error"):
            parts.append("Columns and custom order are unavailable: "
                         + self.current["code_error"])
        self.info.configure(text="  ".join(parts),
                            fg=self.T["gold"] if off else self.T["muted"])
        self._update_summary()

    # ── apply / reset ─────────────────────────────────────────────────────
    def _changes(self):
        """{"position", "order", "code"} -> bool, pending vs disk."""
        if not (self.current and self.pending):
            return {}
        p, cur, code = self.pending, self.current, self._code()
        cols_now = code["columns"] if code else tmgr.STOCK_COLUMNS
        custom_now = bool(code and code["custom_order"])
        return {
            "position": tuple(round(v, 3) for v in p["position"]) !=
            tuple(round(v, 3) for v in cur["position"]),
            "order": p["custom_order"] and p["order"] != cur["order"],
            "code": p["columns"] != cols_now or p["custom_order"] != custom_now,
        }

    def _update_summary(self):
        ch = self._changes()
        lines = []
        if ch.get("position"):
            lines.append("• Move the grid (level2)")
        if ch.get("order"):
            lines.append("• New team order (level1)")
        if ch.get("code"):
            code = self._code() or {"columns": tmgr.STOCK_COLUMNS}
            for k, n in self.pending["columns"].items():
                if n != code["columns"][k]:
                    short = "HardWoods" if k == tmgr.MATRIX_HARDWOODS else "Standard"
                    lines.append(f"• {short} grid: {code['columns'][k]} → {n} "
                                 f"columns (game code)")
            if self.pending["custom_order"] != bool(code.get("custom_order")):
                lines.append("• Turn the nickname sort "
                             f"{'off' if self.pending['custom_order'] else 'back on'}"
                             " (game code)")
        self.summary.configure(
            text="\n".join(lines) if lines else "No changes.",
            fg=self.T["text"] if lines else self.T["muted"])
        key = self._grid_key()
        stock = tmgr.STOCK_COLUMNS[key]
        self.cols_hint.configure(
            text=f"{'HardWoods' if self.dlc_var.get() else 'Standard'} grid: "
                 f"stock {stock}, allowed {tmgr.MIN_COLUMNS}–"
                 f"{tmgr.MAX_COLUMNS[key]}. The game stores this number in a "
                 f"one-byte instruction, so it can shrink but not grow.")
        self._sync_buttons()

    def _set_enabled(self, on):
        code_ok = bool(on and self._code())
        state = "normal" if on else "disabled"
        for w in (self.apply_btn,):
            w.configure(state=state)
        self.cols_spin.configure(state="normal" if code_ok else "disabled")
        self._sync_buttons()

    def _sync_buttons(self):
        custom = self._custom_ok()
        for b in self.order_btns:
            b.configure(state="normal" if custom else "disabled")
        self.order_list.configure(state="normal")
        ch = self._changes()
        needs_code = bool(ch.get("code"))
        self.consent_cb.configure(state="normal" if needs_code else "disabled")
        can = any(ch.values()) and (not needs_code or self.consent.get())
        self.apply_btn.configure(state="normal" if can else "disabled")

    def apply(self):
        if not self.tab._guard() or not self.pending:
            return
        ch = self._changes()
        if not any(ch.values()):
            return
        if ch["code"] and not self.consent.get():
            messagebox.showwarning("Game code", "Tick the box to confirm this "
                                   "edits the game's code first.")
            return
        if ch["code"] and not messagebox.askyesno(
                "Edit the game's code?",
                "Columns and custom order are stored in Assembly-CSharp.dll, so "
                "this patches it. The original is backed up first, and Reset "
                "Everything to Stock (or Undo in Advanced…) restores it.\n\n"
                "A Steam update or Verify Integrity also restores it, which "
                "undoes these changes.\n\nContinue?"):
            return
        game, p = self.tab.game_data, dict(self.pending)
        pids = set(self.teams_by_pid)
        teams = self.bundled_count

        def work():
            done_steps = []
            if ch["position"]:
                tmgr.set_grid_position(game, *p["position"])
                done_steps.append("position")
            if ch["order"]:
                tmgr.set_team_order(game, pids, p["order"])
                done_steps.append("order")
            if ch["code"]:
                tmgr.apply_code_layout(game, teams, columns=p["columns"],
                                       custom_order=p["custom_order"])
                done_steps.append("code")
            return done_steps

        def done(res, err):
            if err:
                messagebox.showerror("Could not apply the layout", str(err))
                self.tab._set_status(str(err), self.T["red"])
            else:
                self.tab._set_status(
                    "Layout applied: " + ", ".join(res) +
                    ". Launch the game to see it.", OK_GREEN)
            self.consent.set(False)
            self.load(list(self.teams_by_pid.values()), self.bundled_count)

        self.tab._run(work, done, "Applying the layout …")

    def reset_all(self):
        if not self.tab._guard():
            return
        game = self.tab.game_data

        def plan():
            steps = tmgr.reset_plan(game)
            saves = []
            try:
                import save_manager as sm
                for path in sm.find_save_files() or []:
                    saves.append(path)
            except Exception:
                pass
            return steps, saves

        def confirm(res, err):
            if err:
                messagebox.showerror("Reset", str(err))
                return
            steps, saves = res
            if not steps:
                messagebox.showinfo("Already stock",
                                    "Every team file already matches the game "
                                    "as shipped. Nothing to reset.")
                return
            text = ("This puts every team change back to how the game shipped:\n\n"
                    + "\n".join(f"  • {s}" for s in steps) +
                    "\n\nAdded teams, renames, recolors, borrowed art, custom "
                    "order, columns and the navigation fix all go. Slider edits "
                    "stay. Your texture, mesh and audio mods are re-applied "
                    "afterwards.\n\n")
            if saves:
                text += ("If your save still lists a removed team, it is backed up "
                         "and that entry is patched so the save keeps loading. "
                         "Quit Steam first, or Steam Cloud may put the old save "
                         "back.\n\n")
            text += "Close NBA Bounce before continuing. Reset now?"
            if not messagebox.askyesno("Reset everything to stock?", text):
                return
            self.tab._run(lambda: self._do_reset(game, saves), finished,
                          "Resetting team files …")

        def finished(res, err):
            if err:
                messagebox.showerror("Reset failed", str(err))
                self.tab._set_status(str(err), self.T["red"])
                return
            note = ""
            if res.get("save_fixed"):
                note = (f" Patched {res['save_fixed']} removed-team entr"
                        f"{'y' if res['save_fixed'] == 1 else 'ies'} in your save "
                        f"(backup made first).")
            if res.get("save_error"):
                messagebox.showwarning("Save not patched", res["save_error"])
            self.tab._after_rebuild(res)      # re-apply mods, then rescan
            messagebox.showinfo(
                "Back to stock",
                "Every team file is back to how the game shipped, and your "
                "queued texture, mesh and audio mods were re-applied." + note)

        self.tab._run(plan, confirm, "Checking what differs from stock …")

    @staticmethod
    def _do_reset(game, saves):
        res = tmgr.reset_team_changes(game)
        fixed, errors = 0, []
        if saves:
            ids = tmgr.stock_team_ids(game)
            for path in saves:
                try:
                    fixed += tmgr.repair_save_teams(path, ids)["fixed"]
                except Exception as exc:
                    errors.append(f"{os.path.basename(path)}: {exc}")
        res["save_fixed"] = fixed
        res["save_error"] = "\n".join(errors) if errors else None
        return res
