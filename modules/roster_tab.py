"""
roster_tab.py -- Roster tab: custom players per team (starters / bench / legends),
bundled players' ratings, the BepInEx loader bar, and bundle export / import.

    RosterTab(parent, game_data_path, model, host=app, theme=...)

Replaces the old Ratings tab (ratings_manager.py is reused). Imports nothing from
app.py and never calls apply_single_mod().
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import ratings_manager as rm
import roster_manager as rmg
from avatar_tab import DEFAULT_THEME, ThumbGrid, init_styles

ALL = "All teams (bundled ratings)"
INACTIVE = {"Dunk", "Block"}


class RosterTab(tk.Frame):
    def __init__(self, master, game_data_path, model, host=None, theme=None, **kw):
        self.th = dict(DEFAULT_THEME)
        if theme:
            self.th.update(theme)
        super().__init__(master, bg=self.th["bg"], **kw)
        self.model, self.host = model, host
        self.game_data_path = game_data_path or ""
        model.game_data_path = self.game_data_path
        self._filling = False
        self._body_id = None              # avatar id whose height/weight is shown
        self._bundled_row = None          # ratings row index when a bundled player is shown
        self._photo = None
        self.team_var = tk.StringVar(value=rmg.TEAMS[0])
        self.name_var = tk.StringVar()
        self.num_var = tk.IntVar(value=0)
        self.role_var = tk.StringVar(value="bench")
        self.ft_var, self.in_var = tk.IntVar(value=6), tk.IntVar(value=6)
        self.wt_var = tk.IntVar(value=int(rmg.DEFAULT_WEIGHT))
        self.def_var = tk.BooleanVar(value=True)
        self.vars = [tk.IntVar(value=5) for _ in rmg.RATING_LABELS]
        init_styles(self, self.th)
        self._build()
        model.listeners.append(self._on_model)
        self.refresh_loader()

    # ------------------------------------------------------------- helpers
    @property
    def loader(self):
        return rmg.RosterLoader(rm.game_root(self.game_data_path))

    def _say(self, msg, color=None):
        self.msg.config(text=msg, fg=color or self.th["muted"])

    def _btn(self, parent, text, cmd):
        return ttk.Button(parent, text=text, style="Ros.TButton", command=cmd)

    # --------------------------------------------------------------- build
    def _build(self):
        t = self.th
        bar = tk.Frame(self, bg=t["panel"])
        bar.pack(fill="x", padx=8, pady=(8, 4))
        self.bep_lbl = tk.Label(bar, bg=t["panel"], fg=t["text"], font=("Segoe UI", 10))
        self.bep_lbl.pack(side="left", padx=(10, 16), pady=8)
        self.plug_lbl = tk.Label(bar, bg=t["panel"], fg=t["text"], font=("Segoe UI", 10))
        self.plug_lbl.pack(side="left", pady=8)
        self.uninstall_btn = self._btn(bar, "Uninstall Loader", self.uninstall_loader)
        self.uninstall_btn.pack(side="right", padx=(4, 8), pady=6)
        self.toggle_btn = self._btn(bar, "Disable", self.toggle_loader)
        self.toggle_btn.pack(side="right", padx=4, pady=6)
        self.install_btn = self._btn(bar, "Install Loader", self.install_loader)
        self.install_btn.pack(side="right", padx=4, pady=6)

        self.msg = tk.Label(self, bg=t["bg"], fg=t["muted"], anchor="w", justify="left",
                            font=("Segoe UI", 9), wraplength=1000)
        self.msg.pack(fill="x", padx=12)

        body = tk.Frame(self, bg=t["bg"])
        body.pack(fill="both", expand=True, padx=8, pady=4)
        left = tk.Frame(body, bg=t["bg"])
        left.pack(side="left", fill="both", expand=True)
        top = tk.Frame(left, bg=t["bg"])
        top.pack(fill="x", pady=(0, 4))
        tk.Label(top, text="Team", bg=t["bg"], fg=t["text"]).pack(side="left")
        self.team_cb = ttk.Combobox(top, textvariable=self.team_var, state="readonly", width=26,
                                    values=rmg.TEAMS + [ALL])
        self.team_cb.pack(side="left", padx=6)
        self.team_cb.bind("<<ComboboxSelected>>", lambda e: self._fill())
        self.slots = tk.Label(top, bg=t["bg"], fg=t["muted"], font=("Segoe UI", 9))
        self.slots.pack(side="left", padx=10)

        cols = ["num", "tpl", "ht"] + [l[:3] for l in rmg.RATING_LABELS]
        tf = tk.Frame(left, bg=t["bg"])
        tf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tf, columns=cols, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Player")
        self.tree.column("#0", width=220, stretch=True)
        for c, h in zip(cols, ["#", "Template", "Height"] + rmg.RATING_LABELS):
            self.tree.heading(c, text=h[:5] if c not in ("num", "tpl", "ht") else h)
            self.tree.column(c, width=48 if c not in ("tpl", "ht") else 56, anchor="center", stretch=False)
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.tag_configure("group", foreground=t["gold"])
        self.tree.tag_configure("ro", foreground=t["muted"])

        btns = tk.Frame(left, bg=t["bg"])
        btns.pack(fill="x", pady=4)
        self.b_add = self._btn(btns, "Add player", self.add_player)
        self.b_dup = self._btn(btns, "Duplicate", self.duplicate)
        self.b_del = self._btn(btns, "Delete", self.delete)
        self.b_up = self._btn(btns, "Move up", lambda: self.move(-1))
        self.b_dn = self._btn(btns, "Move down", lambda: self.move(1))
        for b in (self.b_add, self.b_dup, self.b_del, self.b_up, self.b_dn):
            b.pack(side="left", padx=3)

        right = tk.Frame(body, bg=t["panel"], width=300)
        right.pack(side="right", fill="y", padx=(8, 0))
        right.pack_propagate(False)
        self.sel_lbl = tk.Label(right, text="Select a player", bg=t["panel"], fg=t["gold"],
                                font=("Segoe UI", 11, "bold"), anchor="w", wraplength=270, justify="left")
        self.sel_lbl.pack(fill="x", padx=12, pady=(10, 4))
        form = tk.Frame(right, bg=t["panel"])
        form.pack(fill="x", padx=12)
        tk.Label(form, text="Name", bg=t["panel"], fg=t["text"], width=8, anchor="w").grid(row=0, column=0, pady=2)
        self.name_e = tk.Entry(form, textvariable=self.name_var, bg=t["entry"], fg=t["text"],
                               insertbackground=t["text"], relief="flat")
        self.name_e.grid(row=0, column=1, sticky="ew", pady=2)
        tk.Label(form, text="Number", bg=t["panel"], fg=t["text"], width=8, anchor="w").grid(row=1, column=0, pady=2)
        self.num_sp = tk.Spinbox(form, from_=0, to=99, width=5, textvariable=self.num_var, bg=t["entry"],
                                 fg=t["text"], insertbackground=t["text"], relief="flat",
                                 command=self._form_changed)
        self.num_sp.grid(row=1, column=1, sticky="w", pady=2)
        tk.Label(form, text="Role", bg=t["panel"], fg=t["text"], width=8, anchor="w").grid(row=2, column=0, pady=2)
        self.role_cb = ttk.Combobox(form, textvariable=self.role_var, state="readonly", width=10,
                                    values=rmg.ROLES)
        self.role_cb.grid(row=2, column=1, sticky="w", pady=2)
        self.role_cb.bind("<<ComboboxSelected>>", lambda e: self._form_changed())
        tk.Label(form, text="Template", bg=t["panel"], fg=t["text"], width=8, anchor="w").grid(row=3, column=0, pady=2)
        tf2 = tk.Frame(form, bg=t["panel"])
        tf2.grid(row=3, column=1, sticky="w")
        self.tpl_lbl = tk.Label(tf2, bg=t["panel"], fg=t["muted"])
        self.tpl_lbl.pack(side="left")
        self.tpl_btn = self._btn(tf2, "Change...", self.change_template)
        self.tpl_btn.pack(side="left", padx=4)
        form.columnconfigure(1, weight=1)
        for w in (self.name_e, self.num_sp):
            w.bind("<KeyRelease>", lambda e: self._form_changed())
        self.scales = []
        for i, name in enumerate(rmg.RATING_LABELS):
            row = tk.Frame(right, bg=t["panel"])
            row.pack(fill="x", padx=12, pady=1)
            inactive = name in INACTIVE
            tk.Label(row, text=name + (" *" if inactive else ""), width=9, anchor="w", bg=t["panel"],
                     fg=t["muted"] if inactive else t["text"]).pack(side="left")
            sc = tk.Scale(row, from_=1, to=10, orient="horizontal", variable=self.vars[i], showvalue=True,
                          length=150, bg=t["panel"], fg=t["text"], troughcolor=t["entry"], highlightthickness=0,
                          command=lambda _v, k=i: self._rating_changed(k))
            sc.pack(side="left", fill="x", expand=True)
            self.scales.append(sc)
        tk.Label(right, text="5 = game default. Block below 5 can cancel blocks.\n* = may be inactive in game.",
                 bg=t["panel"], fg=t["muted"], font=("Segoe UI", 8), justify="left").pack(padx=12, anchor="w")
        self.reset_btn = self._btn(right, "Reset ratings to 5s", self.reset_ratings)
        self.reset_btn.pack(padx=12, pady=6, anchor="w")
        self._build_body(right)
        self.prev_cv = tk.Canvas(right, width=110, height=110, bg=t["entry"], highlightthickness=0)
        self.prev_cv.pack(pady=4)

        foot = tk.Frame(self, bg=t["bg"])
        foot.pack(fill="x", padx=8, pady=(0, 8))
        self.save_btn = self._btn(foot, "Save", self.save)
        self.save_btn.pack(side="left", padx=4)
        self._btn(foot, "Reload", lambda: self.load()).pack(side="left", padx=4)
        self.dirty_lbl = tk.Label(foot, bg=t["bg"], fg=t["gold"], font=("Segoe UI", 9, "bold"))
        self.dirty_lbl.pack(side="left", padx=10)
        self._btn(foot, "Export roster...", self.export_bundle).pack(side="right", padx=4)
        self._btn(foot, "Import roster...", self.import_bundle).pack(side="right", padx=4)
        self._set_detail(None)

    # ------------------------------------------------------------ body size
    def _ht(self, aid):
        try:
            b = self.model.bodies.get(int(aid))
        except (TypeError, ValueError):
            b = None
        return rmg.fmt_height(b[0]) if b else ""

    def _build_body(self, right):
        t = self.th
        box = tk.Frame(right, bg=t["panel"])
        box.pack(fill="x", padx=12, pady=(2, 0))
        r1 = tk.Frame(box, bg=t["panel"])
        r1.pack(fill="x")
        tk.Label(r1, text="Height", width=7, anchor="w", bg=t["panel"], fg=t["text"]).pack(side="left")
        kw = dict(bg=t["entry"], fg=t["text"], insertbackground=t["text"], relief="flat", width=3)
        self.ft_sp = tk.Spinbox(r1, from_=5, to=8, textvariable=self.ft_var, command=self._body_changed, **kw)
        self.ft_sp.pack(side="left")
        tk.Label(r1, text="ft", bg=t["panel"], fg=t["muted"]).pack(side="left", padx=(2, 6))
        self.in_sp = tk.Spinbox(r1, from_=0, to=11, textvariable=self.in_var, command=self._body_changed, **kw)
        self.in_sp.pack(side="left")
        tk.Label(r1, text="in", bg=t["panel"], fg=t["muted"]).pack(side="left", padx=2)
        self.def_cb = tk.Checkbutton(r1, text="Default size", variable=self.def_var, command=self._body_default,
                                     bg=t["panel"], fg=t["text"], selectcolor=t["entry"],
                                     activebackground=t["panel"], activeforeground=t["text"])
        self.def_cb.pack(side="right")
        for w in (self.ft_sp, self.in_sp):
            w.bind("<KeyRelease>", lambda e: self._body_changed())
        r2 = tk.Frame(box, bg=t["panel"])
        r2.pack(fill="x")
        tk.Label(r2, text="Weight", width=7, anchor="w", bg=t["panel"], fg=t["text"]).pack(side="left")
        self.wt_sc = tk.Scale(r2, from_=rmg.WEIGHT_RANGE[0], to=rmg.WEIGHT_RANGE[1], orient="horizontal",
                              variable=self.wt_var, showvalue=True, length=150, bg=t["panel"], fg=t["text"],
                              troughcolor=t["entry"], highlightthickness=0,
                              command=lambda _v: self._body_changed())
        self.wt_sc.pack(side="left", fill="x", expand=True)
        tk.Label(r2, text="lb", bg=t["panel"], fg=t["muted"]).pack(side="left")
        self.scale_lbl = tk.Label(box, bg=t["panel"], fg=t["muted"], font=("Segoe UI", 8), anchor="w")
        self.scale_lbl.pack(fill="x")

    def _body_height(self):
        try:
            ft, inch = int(self.ft_var.get()), int(self.in_var.get())
        except (tk.TclError, ValueError):
            return None
        lo, hi = rmg.HEIGHT_RANGE
        return max(lo, min(hi, ft * 12 + max(0, min(11, inch))))

    def _update_scale_lbl(self):
        if self._body_id is None:
            self.scale_lbl.config(text="")
            return
        h, w = self._body_height() or rmg.DEFAULT_HEIGHT, self.wt_var.get()
        hs, ws = rmg.body_scale(h, w)
        self.scale_lbl.config(text=f"In game: x{hs:.2f} tall, x{ws:.2f} wide"
                                   + ("  (default size, no row)" if self.def_var.get() else ""))

    def _show_body(self, aid):
        """Fill the body widgets for avatar id aid (None disables them)."""
        self._body_id = aid
        on = aid is not None
        if on:
            h, w, dflt = self.model.body_for(aid)
            h = max(rmg.HEIGHT_RANGE[0], min(rmg.HEIGHT_RANGE[1], int(round(h))))
            self.ft_var.set(h // 12)
            self.in_var.set(h % 12)
            self.wt_var.set(int(round(w)))
            self.def_var.set(dflt)
        else:
            self.def_var.set(True)
        st = "normal" if on else "disabled"
        for wd in (self.ft_sp, self.in_sp, self.wt_sc, self.def_cb):
            wd.config(state=st)
        self._update_scale_lbl()

    def _refresh_ht_cell(self):
        aid = self._body_id
        for iid in ((f"c:{aid}",) if aid is not None and aid <= -1000 else ()) + \
                   (tuple(self.tree.get_children("g:bundled")) if aid is not None and aid > -1000 else ()):
            if not self.tree.exists(iid):
                continue
            if iid.startswith("b:") and f"Bundled {aid}" != self.tree.item(iid, "text"):
                continue
            vals = list(self.tree.item(iid, "values"))
            vals[2] = self._ht(aid)
            self.tree.item(iid, values=vals)
        if aid is not None and aid > -1000:
            for iid in self.tree.get_children():       # "All teams" view groups
                for c in self.tree.get_children(iid):
                    if self.tree.item(c, "text") == f"Bundled {aid}":
                        vals = list(self.tree.item(c, "values"))
                        vals[2] = self._ht(aid)
                        self.tree.item(c, values=vals)

    def _body_changed(self):
        if self._filling or self._body_id is None:
            return
        h = self._body_height()
        if h is None:
            return
        self.def_var.set(False)
        self.model.set_body(self._body_id, h, self.wt_var.get())
        self._update_scale_lbl()
        self._refresh_ht_cell()

    def _body_default(self):
        if self._body_id is None:
            return
        if self.def_var.get():
            self.model.clear_body(self._body_id)
            self._filling = True
            self._show_body(self._body_id)
            self._filling = False
            self._refresh_ht_cell()
        else:
            self._body_changed()

    # -------------------------------------------------------- loader status
    def refresh_loader(self):
        ld = self.loader
        ok_c, bad_c = "#4ade80", self.th["red"]
        if not ld.valid():
            self.bep_lbl.config(text="BepInEx: set your game folder in Settings", fg=bad_c)
            self.plug_lbl.config(text="")
            for b in (self.install_btn, self.toggle_btn, self.uninstall_btn):
                b.state(["disabled"])
            return
        bep, state, on = ld.bepinex_installed(), ld.plugin_state(), ld.enabled()
        self.bep_lbl.config(text="BepInEx: " + (("installed" if on else "installed (disabled)") if bep else "not installed"),
                            fg=ok_c if bep and on else (self.th["gold"] if bep else bad_c))
        self.plug_lbl.config(text="Ratings + Roster plugins: " + state,
                             fg=ok_c if state == "installed" else (self.th["gold"] if state == "outdated" else bad_c))
        need = (not bep) or state != "installed"
        self.install_btn.config(text="Update Plugins" if bep and state == "outdated" else "Install Loader")
        self.install_btn.state(["!disabled"] if need else ["disabled"])
        self.toggle_btn.config(text="Disable" if on else "Enable")
        self.toggle_btn.state(["!disabled"] if bep else ["disabled"])
        self.uninstall_btn.state(["!disabled"] if bep or state != "missing" else ["disabled"])

    def install_loader(self):
        ld = self.loader
        try:
            ld.update_plugin() if ld.bepinex_installed() else ld.install()
        except Exception as e:
            messagebox.showerror("Install Loader", str(e), parent=self)
            return
        self.refresh_loader()
        self.load(silent=True)
        self._say("Loader installed. Launch the game once to the main menu so it creates "
                  "BounceRatings.csv, then press Reload." if not os.path.isfile(ld.csv) else "Loader installed.",
                  "#4ade80")

    def uninstall_loader(self):
        if not messagebox.askyesno("Uninstall Loader",
                                   "Remove BepInEx from the game folder? This removes every BepInEx plugin "
                                   "(including Bounce Camera) and returns the game to vanilla.\n\n"
                                   "Copies of your Bounce*.csv files (ratings, roster, looks, bodies) are kept "
                                   "in the app's backup folder.", parent=self):
            return
        try:
            backup = self.loader.uninstall()
        except Exception as e:
            messagebox.showerror("Uninstall Loader", str(e), parent=self)
            return
        self.refresh_loader()
        self.load()
        self._say("Loader removed." + (f" CSVs backed up to {os.path.dirname(backup)}" if backup else ""))

    def toggle_loader(self):
        ld = self.loader
        try:
            ld.set_enabled(not ld.enabled())
        except Exception as e:
            messagebox.showerror("Loader", str(e), parent=self)
            return
        self.refresh_loader()
        self._say("Roster and ratings are ON in game." if ld.enabled() else "Loader disabled: the game runs vanilla.")

    # ------------------------------------------------------------ load / fill
    def load(self, silent=False):
        """Tab-selected hook: loads once; later calls keep unsaved edits."""
        if silent and self.model.loaded and self.model.dirty:
            return
        if silent and self.model.loaded:
            return
        self._say(self.model.load(self.game_data_path))

    def _on_model(self, kind):
        self.dirty_lbl.config(text="* Unsaved changes" if self.model.dirty else "")
        if kind == "load":
            self._fill()
        elif kind == "change":
            self._refresh_counts()

    def _team_id(self):
        return rmg.ID_BY_TEAM.get(self.team_var.get())

    def _fill(self):
        m, tr = self.model, self.tree
        tr.delete(*tr.get_children())
        tid = self._team_id()
        if tid is None:
            self._fill_all()
        else:
            c = m.counts(tid)
            for role in rmg.ROLES:
                lim = rmg.ROLE_LIMITS[role]
                gid = "g:" + role
                tr.insert("", "end", iid=gid, text=f"{rmg.ROLE_TITLES[role]} ({c[role]}/{lim})",
                          open=True, tags=("group",))
                for p in [p for p in m.team_players(tid) if p.role == role]:
                    tr.insert(gid, "end", iid=f"c:{p.id}", text=p.name or "(unnamed)",
                              values=[p.number, p.template_id, self._ht(p.id)] + p.ratings)
            tr.insert("", "end", iid="g:bundled", text="Bundled players (ratings only)", open=True, tags=("group",))
            name = rmg.TEAM_BY_ID[tid]
            for idx, num, aid, vals in m.bundled_players(name):
                tr.insert("g:bundled", "end", iid=f"b:{idx}", text=f"Bundled {aid}", tags=("ro",),
                          values=[num, aid, self._ht(aid)] + vals)
            for i, it in enumerate(m.ratings.items):
                if not isinstance(it, str) and it[0].strip() == str(tid):
                    tr.insert("g:bundled", "end", iid=f"b:{i}", text=f"Mascot {tid} (kept)", tags=("ro",),
                              values=[it[2].strip(), tid, ""] + [rm.clamp(x) for x in it[4:rm.NCOL]])
        self._refresh_counts()
        sel = m.selected
        if sel is not None and tr.exists(f"c:{sel}"):
            tr.selection_set(f"c:{sel}")
        else:
            self._set_detail(None)
        self._buttons()

    def _fill_all(self):
        tr, m = self.tree, self.model
        seen = {}
        for idx, team, num, aid, vals in m.ratings.players() if m.ratings_loaded else []:
            if aid.lstrip("-").isdigit() and int(aid) <= -1000:
                continue
            key = team or "(mascots / unassigned)"
            if key not in seen:
                seen[key] = f"t:{len(seen)}"
                tr.insert("", "end", iid=seen[key], text=key, tags=("group",))
            tr.insert(seen[key], "end", iid=f"b:{idx}", text=f"Bundled {aid}", tags=("ro",),
                      values=[num, aid, self._ht(aid)] + vals)

    def _refresh_counts(self):
        tid = self._team_id()
        if tid is None:
            self.slots.config(text="")
            return
        c = self.model.counts(tid)
        n = sum(c.values())
        over = n > rmg.TARGET_SIZE or any(c[r] > rmg.ROLE_LIMITS[r] for r in rmg.ROLES)
        self.slots.config(text=f"Custom players {n}/{rmg.TARGET_SIZE}"
                               + ("  (over the target; the game may not handle more)" if over else ""),
                          fg=self.th["red"] if over else self.th["muted"])
        for role in rmg.ROLES:
            if self.tree.exists("g:" + role):
                self.tree.item("g:" + role, text=f"{rmg.ROLE_TITLES[role]} ({c[role]}/{rmg.ROLE_LIMITS[role]})")

    # --------------------------------------------------------------- selection
    def _cur(self):
        s = self.tree.selection()
        if not s:
            return None, None
        k, _, v = s[0].partition(":")
        return k, v

    def _current_player(self):
        k, v = self._cur()
        return self.model.get(int(v)) if k == "c" else None

    def _on_select(self, _e=None):
        k, v = self._cur()
        if k == "c":
            self.model.selected = int(v)
            self.model.notify("select")
            self._set_detail(self.model.get(int(v)))
        elif k == "b":
            self.model.selected = None
            self.model.notify("select")
            self._set_detail(None, bundled=int(v))
        else:
            self._set_detail(None)
        self._buttons()

    def _buttons(self):
        k, _ = self._cur()
        tid = self._team_id()
        self.b_add.state(["!disabled"] if tid is not None and self.model.loaded else ["disabled"])
        for b in (self.b_del, self.b_up, self.b_dn):
            b.state(["!disabled"] if k == "c" else ["disabled"])
        self.b_dup.state(["!disabled"] if k in ("c", "b") and tid is not None else ["disabled"])

    def _set_detail(self, p, bundled=None):
        self._filling = True
        self._bundled_row = None
        custom = p is not None
        for w in (self.name_e, self.num_sp):
            w.config(state="normal" if custom else "disabled")
        self.role_cb.config(state="readonly" if custom else "disabled")
        self.tpl_btn.state(["!disabled"] if custom else ["disabled"])
        rating_vals = None
        if custom:
            self.sel_lbl.config(text=f"{p.name}  (custom {p.id})")
            self.name_var.set(p.name)
            self.num_var.set(p.number)
            self.role_var.set(p.role)
            self.tpl_lbl.config(text=str(p.template_id))
            rating_vals = p.ratings
        elif bundled is not None and 0 <= bundled < len(self.model.ratings.items) \
                and not isinstance(self.model.ratings.items[bundled], str):
            row = self.model.ratings.items[bundled]
            self._bundled_row = bundled
            self.sel_lbl.config(text=f"Bundled {row[0].strip()}  {row[1].strip() or '(mascot / unassigned)'}  "
                                     f"#{row[2].strip()}  (ratings only)")
            self.name_var.set("")
            self.tpl_lbl.config(text="")
            rating_vals = [rm.clamp(x) for x in row[4:rm.NCOL]]
        else:
            self.sel_lbl.config(text="Select a player")
            self.name_var.set("")
            self.tpl_lbl.config(text="")
        for i, v in enumerate(self.vars):
            v.set(rating_vals[i] if rating_vals else 5)
        for sc in self.scales:
            sc.config(state="normal" if rating_vals else "disabled")
        self.reset_btn.state(["!disabled"] if rating_vals else ["disabled"])
        if custom:
            self._show_body(p.id)
        elif self._bundled_row is not None and str(
                self.model.ratings.items[self._bundled_row][0]).strip().lstrip("-").isdigit():
            self._show_body(int(self.model.ratings.items[self._bundled_row][0]))
        else:
            self._show_body(None)
        self._filling = False
        self._draw_small(p)

    def _draw_small(self, p):
        self.prev_cv.delete("all")
        if p is None or not self.model.ensure_catalog():
            return
        try:
            from PIL import ImageTk
            im = rmg.render_avatar(self.model.catalog, self.model.effective(p), 110, self.th["entry"])
            self._photo = ImageTk.PhotoImage(im)
            self.prev_cv.create_image(0, 0, image=self._photo, anchor="nw")
        except Exception:
            pass

    # ------------------------------------------------------------ edit handlers
    def _form_changed(self):
        p = self._current_player()
        if p is None or self._filling:
            return
        p.name = self.name_var.get()
        try:
            p.number = max(0, min(99, int(self.num_var.get())))
        except (tk.TclError, ValueError):
            pass
        if p.role != self.role_var.get():
            p.role = self.role_var.get()
            self.model.touch()
            self._fill()
            self.tree.selection_set(f"c:{p.id}")
            return
        iid = f"c:{p.id}"
        if self.tree.exists(iid):
            self.tree.item(iid, text=p.name or "(unnamed)")
            vals = list(self.tree.item(iid, "values"))
            vals[0] = p.number
            self.tree.item(iid, values=vals)
        self.sel_lbl.config(text=f"{p.name}  (custom {p.id})")
        self.model.touch()

    def _rating_changed(self, _k):
        if self._filling:
            return
        vals = [v.get() for v in self.vars]
        p = self._current_player()
        k, v = self._cur()
        if p is not None:
            p.ratings = [rm.clamp(x) for x in vals]
            iid = f"c:{p.id}"
        elif self._bundled_row is not None:
            self.model.ratings.set_ratings(self._bundled_row, vals)
            iid = f"b:{self._bundled_row}"
        else:
            return
        if self.tree.exists(iid):
            cur = list(self.tree.item(iid, "values"))
            self.tree.item(iid, values=cur[:3] + [rm.clamp(x) for x in vals])
        self.model.touch()

    def reset_ratings(self):
        self._filling = True
        for v in self.vars:
            v.set(rm.DEFAULT_RATING)
        self._filling = False
        self._rating_changed(0)

    # --------------------------------------------------------------- buttons
    def _pick_template(self, current=None):
        cat = self.model.ensure_catalog()
        if not cat:
            messagebox.showerror("Template", self.model.catalog_error or "Catalog unavailable.", parent=self)
            return None
        dlg = tk.Toplevel(self)
        dlg.title("Choose a template avatar")
        dlg.configure(bg=self.th["bg"])
        dlg.transient(self.winfo_toplevel())
        dlg.geometry("760x520")
        result = {"v": None}
        tk.Label(dlg, text="The new player starts as a copy of this avatar; the Avatar tab then edits only differences.",
                 bg=self.th["bg"], fg=self.th["muted"], wraplength=720, justify="left").pack(padx=10, pady=6, anchor="w")
        teams = {}
        for r in self.model.ratings.players() if self.model.ratings_loaded else []:
            teams[int(r[3])] = r[1]
        opts = []
        for aid in cat.template_ids():
            vals = cat.template_values(aid)
            lbl = f"{aid} {teams.get(aid, '')}".strip() or str(aid)
            if cat.avatars[aid]["avatar_type"] == "GOAT_CHAR":
                lbl = f"{aid} {cat.avatars[aid].get('name', '')}"
            opts.append((aid, lbl[:16], (lambda v=vals: rmg.render_avatar(cat, v, 64)), False, True))
        g = ThumbGrid(dlg, self.th, lambda v: result.update(v=v), cell_w=92, cell_h=96, thumb=64, max_rows=4)
        g.pack(fill="both", expand=True, padx=10, pady=4)
        g.set_options(opts, current)
        result["v"] = current
        bar = tk.Frame(dlg, bg=self.th["bg"])
        bar.pack(fill="x", pady=8)
        ok = {"ok": False}
        self._btn(bar, "Use this template", lambda: (ok.update(ok=True), dlg.destroy())).pack(side="right", padx=10)
        self._btn(bar, "Cancel", dlg.destroy).pack(side="right")
        dlg.grab_set()
        self.wait_window(dlg)
        return result["v"] if ok["ok"] and result["v"] is not None else None

    def add_player(self):
        tid = self._team_id()
        if tid is None:
            return
        tpl = self._pick_template()
        if tpl is None:
            return
        c = self.model.counts(tid)
        role = next((r for r in rmg.ROLES if c[r] < rmg.ROLE_LIMITS[r]), "bench")
        p = self.model.add_player(tid, tpl, role)
        self._fill()
        self.tree.selection_set(f"c:{p.id}")
        self.tree.see(f"c:{p.id}")
        extra = " Team is over the 11-player target." if sum(self.model.counts(tid).values()) > rmg.TARGET_SIZE else ""
        self._say(f"Added {p.name} (id {p.id}) from template {tpl}.{extra} Unsaved.", self.th["gold"])

    def duplicate(self):
        k, v = self._cur()
        tid = self._team_id()
        if k == "c":
            q = self.model.duplicate(self.model.get(int(v)))
        elif k == "b" and tid is not None and self._bundled_row is not None:
            row = self.model.ratings.items[self._bundled_row]
            q = self.model.add_player(tid, int(row[0]), "bench", ratings=[rm.clamp(x) for x in row[4:rm.NCOL]],
                                      number=int(row[2]) if row[2].strip().lstrip("-").isdigit() else None)
        else:
            return
        self._fill()
        self.tree.selection_set(f"c:{q.id}")
        self._say(f"Created {q.name} (id {q.id}). Unsaved.", self.th["gold"])

    def delete(self):
        p = self._current_player()
        if p and messagebox.askyesno("Delete player", f"Delete {p.name}?", parent=self):
            self.model.delete(p)
            self._fill()
            self._say(f"Deleted {p.name}. Unsaved.", self.th["gold"])

    def move(self, d):
        p = self._current_player()
        if p and self.model.move(p, d):
            self._fill()
            self.tree.selection_set(f"c:{p.id}")

    def change_template(self):
        p = self._current_player()
        if not p:
            return
        t = self._pick_template(p.template_id)
        if t is not None and t != p.template_id:
            p.template_id = t
            self.tpl_lbl.config(text=str(t))
            self.model.touch()
            self._fill()
            self.tree.selection_set(f"c:{p.id}")
            self.model.notify("select")

    # ------------------------------------------------------------ save / io
    def save(self):
        if not self.model.loaded:
            return
        try:
            msg = self.model.save()
        except Exception as e:
            messagebox.showerror("Save", str(e), parent=self)
            return
        self._say(msg, "#4ade80")

    def export_bundle(self):
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".json", initialfile="bounce_roster.json",
                                         filetypes=[("Roster bundle (json)", "*.json"), ("Roster bundle (zip)", "*.zip")])
        if p:
            try:
                self.model.export_bundle(p)
            except Exception as e:
                messagebox.showerror("Export roster", str(e), parent=self)
                return
            self._say(f"Exported {len(self.model.players)} players to {p}")

    def import_bundle(self):
        p = filedialog.askopenfilename(parent=self, filetypes=[("Roster bundle", "*.json *.zip"), ("All files", "*.*")])
        if not p:
            return
        if self.model.players and not messagebox.askyesno(
                "Import roster", "Replace the current custom roster with the imported one?", parent=self):
            return
        try:
            n = self.model.import_bundle(p)
        except Exception as e:
            messagebox.showerror("Import roster", str(e), parent=self)
            return
        self._say(f"Imported {n} players. Press Save to write them to the game.", self.th["gold"])

    def set_game_path(self, path):
        if path == self.game_data_path:
            return
        self.game_data_path = path or ""
        self.model.game_data_path = self.game_data_path
        self.refresh_loader()
        self._say(self.model.load(self.game_data_path))
