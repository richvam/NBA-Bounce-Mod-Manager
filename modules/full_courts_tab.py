"""
full_courts_tab.py -- the Full Courts tab.

Self-contained, like camera_tab.py: imports nothing from app.py. All byte work
is in full_courts.py; this file is only tkinter.

    from full_courts_tab import FullCourtsTab
    frame = FullCourtsTab(tab, game_data_path, host=app, theme={...})

Per team, per court variant: import a court picture (the exported template or
any image), check the court rectangle the app found -- drag its edges in the
preview or type the numbers -- and pick what happens to the centre logo.
Apply writes the team; Revert puts the team's stock court back.
"""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import full_courts as fc

DEFAULT_THEME = {
    "bg": "#1a1a2e", "panel": "#16213e", "accent": "#0f3460",
    "text": "#eaeaea", "muted": "#a0a0b0", "entry": "#0d1b2a",
    "gold": "#FDB927", "red": "#e94560",
}
OK_GREEN = "#4ade80"
RECT_COL = "#00e5ff"
PREVIEW_W, PREVIEW_H = 640, 320
LOGO_PX = 150
STRIP_PX = (240, 30)

INFO_TEXT = (
    "Put one picture on a team's whole floor: wood, apron, keys, lines and logos. "
    "Start from the template (Export Template) or any court image. The app finds "
    "the court lines in it; drag the cyan edges if they're off. Teams with retro "
    "courts can have a different picture for each one. A team with a full court "
    "ignores Floor Patterns and Court Colors until you revert it.")

STATUS_TEXT = {"applied": "✓ in game", "undone": "⚠ undone", "pending": "● not applied",
               "none": ""}


def _is_light(hexcol):
    h = hexcol.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (r * 299 + g * 587 + b * 114) / 1000 > 140


class FullCourtsTab(tk.Frame):
    def __init__(self, master, game_data_path: str, host=None,
                 theme: dict | None = None, **kw):
        self.th = dict(DEFAULT_THEME)
        if theme:
            self.th.update(theme)
        super().__init__(master, bg=self.th["bg"], **kw)
        self.host = host
        self.game_data_path = game_data_path or ""
        self.courts = {}           # token -> discovery entry
        self.status = {}           # token -> applied/undone/pending/none
        self.order = []            # tokens in listbox order
        self.state = fc.load_state()
        self.token = None
        self.variant = None
        self._busy = False
        self._loaded = False
        self._src = None           # PIL image of the current court picture
        self._scale = 1.0
        self._drag = None
        self._tk_preview = None
        self._tk_logo = None
        self._tk_strip = None
        self.ok = "#15803d" if _is_light(self.th["bg"]) else OK_GREEN
        self._init_styles()
        self._build()
        self._set_enabled(False)

    # ------------------------------------------------------------------ styles
    def _init_styles(self):
        t = self.th
        s = ttk.Style(self)
        s.configure("FC.TLabelframe", background=t["bg"], bordercolor=t["accent"],
                    relief="solid", borderwidth=1)
        s.configure("FC.TLabelframe.Label", background=t["bg"], foreground=t["gold"],
                    font=("Segoe UI", 10, "bold"))
        s.configure("FC.TButton", background=t["accent"], foreground="#FFFFFF",
                    font=("Segoe UI", 10), relief="flat", padding=6)
        s.map("FC.TButton", background=[("active", "#1a4a80"), ("disabled", "#555")],
              foreground=[("disabled", "#bbbbbb")])
        s.configure("FCAccent.TButton", background=t["red"], foreground="#FFFFFF",
                    font=("Segoe UI", 10, "bold"), relief="flat", padding=6)
        s.map("FCAccent.TButton", background=[("active", "#c73652"), ("disabled", "#555")],
              foreground=[("disabled", "#bbbbbb")])
        s.configure("FC.TRadiobutton", background=t["bg"], foreground=t["text"])
        s.map("FC.TRadiobutton", background=[("active", t["bg"])])

    # ---------------------------------------------------------------------- UI
    def _build(self):
        t = self.th
        head = tk.Frame(self, bg=t["bg"])
        head.pack(fill="x", padx=12, pady=(12, 2))
        tk.Label(head, text="🏟  Full Courts", bg=t["bg"], fg=t["red"],
                 font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Button(head, text="📐 Export Template…", style="FC.TButton",
                   command=self._export_template).pack(side="right")
        tk.Label(self, text=INFO_TEXT, bg=t["bg"], fg=t["muted"], font=("Segoe UI", 9),
                 anchor="w", justify="left", wraplength=1100).pack(fill="x", padx=12)

        self.banner = tk.Label(self, text="", bg=t["gold"], fg="#111111",
                               font=("Segoe UI", 9, "bold"), anchor="w", justify="left")

        body = tk.Frame(self, bg=t["bg"])
        body.pack(fill="both", expand=True, padx=12, pady=6)
        self._body = body

        # -- left: teams ---------------------------------------------------
        left = ttk.Labelframe(body, text=" Teams ", style="FC.TLabelframe")
        left.pack(side="left", fill="y", padx=(0, 8))
        self.listbox = tk.Listbox(left, width=30, height=22, activestyle="none",
                                  bg=t["panel"], fg=t["text"], relief="flat",
                                  highlightthickness=0, selectbackground=t["accent"],
                                  selectforeground="#FFFFFF", exportselection=False,
                                  font=("Segoe UI", 10))
        self.listbox.pack(fill="both", expand=True, padx=8, pady=8)
        self.listbox.bind("<<ListboxSelect>>", self._on_team)

        # -- middle: court picture -----------------------------------------
        mid = ttk.Labelframe(body, text=" Court picture ", style="FC.TLabelframe")
        mid.pack(side="left", fill="both", expand=True, padx=(0, 8))
        row = tk.Frame(mid, bg=t["bg"])
        row.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(row, text="Court:", bg=t["bg"], fg=t["text"]).pack(side="left")
        self.variant_var = tk.StringVar()
        self.variant_cb = ttk.Combobox(row, textvariable=self.variant_var,
                                       state="readonly", width=46)
        self.variant_cb.pack(side="left", padx=6)
        self.variant_cb.bind("<<ComboboxSelected>>", self._on_variant)
        self.variant_note = tk.Label(mid, text="", bg=t["bg"], fg=t["muted"],
                                     font=("Segoe UI", 9), anchor="w", justify="left",
                                     wraplength=PREVIEW_W)
        self.variant_note.pack(fill="x", padx=8)

        self.canvas = tk.Canvas(mid, width=PREVIEW_W, height=PREVIEW_H, bg=t["panel"],
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack(padx=8, pady=6)
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)

        nums = tk.Frame(mid, bg=t["bg"])
        nums.pack(fill="x", padx=8)
        self.rect_vars = []
        for label in ("Left", "Top", "Right", "Bottom"):
            tk.Label(nums, text=label, bg=t["bg"], fg=t["muted"],
                     font=("Segoe UI", 9)).pack(side="left", padx=(0, 2))
            v = tk.StringVar()
            e = tk.Entry(nums, textvariable=v, width=6, bg=t["entry"], fg=t["text"],
                         insertbackground=t["text"], relief="flat")
            e.pack(side="left", padx=(0, 10))
            e.bind("<Return>", self._on_rect_typed)
            e.bind("<FocusOut>", self._on_rect_typed)
            self.rect_vars.append(v)
        tk.Label(nums, text="px (court lines, in the original image)", bg=t["bg"],
                 fg=t["muted"], font=("Segoe UI", 8)).pack(side="left")

        btns = tk.Frame(mid, bg=t["bg"])
        btns.pack(fill="x", padx=8, pady=8)
        self.import_btn = ttk.Button(btns, text="📂 Import Picture…", style="FC.TButton",
                                     command=self._import_court)
        self.import_btn.pack(side="left")
        self.detect_btn = ttk.Button(btns, text="🔍 Auto-detect Court", style="FC.TButton",
                                     command=self._auto_detect)
        self.detect_btn.pack(side="left", padx=6)
        self.clear_btn = ttk.Button(btns, text="✖ Remove Picture", style="FC.TButton",
                                    command=self._clear_court)
        self.clear_btn.pack(side="left")

        # -- right: centre logo + actions ----------------------------------
        right = tk.Frame(body, bg=t["bg"])
        right.pack(side="left", fill="y")
        logo = ttk.Labelframe(right, text=" Centre logo ", style="FC.TLabelframe")
        logo.pack(fill="x")
        self.logo_mode = tk.StringVar(value="replace")
        self.logo_source = tk.StringVar(value="court")
        ttk.Radiobutton(logo, text="Replace the logo", value="replace",
                        variable=self.logo_mode, style="FC.TRadiobutton",
                        command=self._on_logo_change).pack(anchor="w", padx=8, pady=(6, 0))
        src = tk.Frame(logo, bg=t["bg"])
        src.pack(fill="x", padx=24, pady=(2, 4))
        self.src_court_rb = ttk.Radiobutton(src, text="with the court's centre circle",
                                            value="court", variable=self.logo_source,
                                            style="FC.TRadiobutton",
                                            command=self._on_logo_change)
        self.src_court_rb.pack(anchor="w")
        self.src_file_rb = ttk.Radiobutton(src, text="with an image file",
                                           value="file", variable=self.logo_source,
                                           style="FC.TRadiobutton",
                                           command=self._on_logo_change)
        self.src_file_rb.pack(anchor="w")
        self.logo_btn = ttk.Button(src, text="Choose Logo…", style="FC.TButton",
                                   command=self._choose_logo)
        self.logo_btn.pack(anchor="w", pady=4)
        size_row = tk.Frame(src, bg=t["bg"])
        size_row.pack(fill="x")
        tk.Label(size_row, text="Size", bg=t["bg"], fg=t["text"],
                 font=("Segoe UI", 9)).pack(side="left")
        self.logo_scale = tk.DoubleVar(value=round(fc.DECAL_CIRCLE_FRACTION * 100))
        self.scale_w = ttk.Scale(size_row, from_=5, to=100, variable=self.logo_scale,
                                 orient="horizontal", length=110,
                                 command=lambda _v: self._on_scale_drag())
        self.scale_w.pack(side="left", padx=4)
        self.scale_w.bind("<ButtonRelease-1>", lambda _e: self._on_scale_done())
        self.scale_lbl = tk.Label(size_row, text="", bg=t["bg"], fg=t["muted"],
                                  font=("Segoe UI", 8), width=4)
        self.scale_lbl.pack(side="left")
        self.scale_reset = ttk.Button(src, text="Fit to centre circle", style="FC.TButton",
                                      command=self._reset_scale)
        self.scale_reset.pack(anchor="w", pady=(2, 4))
        for val, text in (("hide", "Hide it"), ("keep", "Keep the game's logo")):
            ttk.Radiobutton(logo, text=text, value=val, variable=self.logo_mode,
                            style="FC.TRadiobutton", command=self._on_logo_change
                            ).pack(anchor="w", padx=8)
        self.logo_canvas = tk.Canvas(logo, width=LOGO_PX, height=LOGO_PX, bg=t["panel"],
                                     highlightthickness=0)
        self.logo_canvas.pack(pady=4)
        self.logo_note = tk.Label(logo, text="", bg=t["bg"], fg=t["muted"],
                                  font=("Segoe UI", 8), wraplength=230, justify="left")
        self.logo_note.pack(fill="x", padx=8, pady=(0, 8))

        base = ttk.Labelframe(right, text=" Baseline text ", style="FC.TLabelframe")
        base.pack(fill="x", pady=(8, 0))
        self.strip_mode = tk.StringVar(value="picture")
        for val, text in (("picture", "Use the picture's (cut from its apron)"),
                          ("none", "None (NBA logo only)"),
                          ("keep", "Keep the game's")):
            ttk.Radiobutton(base, text=text, value=val, variable=self.strip_mode,
                            style="FC.TRadiobutton", command=self._on_strip_change
                            ).pack(anchor="w", padx=8)
        self.strip_canvas = tk.Canvas(base, width=STRIP_PX[0], height=STRIP_PX[1],
                                      bg=t["panel"], highlightthickness=0)
        self.strip_canvas.pack(padx=8, pady=6)

        act = tk.Frame(right, bg=t["bg"])
        act.pack(fill="x", pady=10)
        self.apply_btn = ttk.Button(act, text="✅ Apply to Game", style="FCAccent.TButton",
                                    command=self._apply)
        self.apply_btn.pack(fill="x")
        self.revert_btn = ttk.Button(act, text="↩ Revert Team", style="FC.TButton",
                                     command=self._revert)
        self.revert_btn.pack(fill="x", pady=6)

        self.status_lbl = tk.Label(self, text="", bg=t["bg"], fg=t["muted"],
                                   font=("Segoe UI", 9), anchor="w")
        self.status_lbl.pack(fill="x", padx=12, pady=(0, 8))

    def _say(self, text, color=None):
        self.status_lbl.configure(text=text, fg=color or self.th["muted"])

    def _set_enabled(self, on):
        st = "normal" if on else "disabled"
        for w in (self.import_btn, self.detect_btn, self.clear_btn, self.apply_btn,
                  self.revert_btn, self.logo_btn):
            w.configure(state=st)
        self.variant_cb.configure(state="readonly" if on else "disabled")

    # ----------------------------------------------------------------- loading
    def load(self, silent=False):
        """Called when the tab is selected: (re)read the teams and what's live."""
        if not self.game_data_path or not os.path.isdir(self.game_data_path):
            self._say("Set your game data folder in ⚙ Settings to use Full Courts.",
                      self.th["red"])
            return
        if self._busy:
            return
        self._run_bg("Reading the courts…", self._bg_load, self._after_load)

    def _bg_load(self, _progress=None):
        courts = fc.discover(self.game_data_path)
        return courts, fc.status(self.game_data_path, courts)

    def _after_load(self, res):
        self.courts, self.status = res
        self.state = fc.load_state()
        self._loaded = True
        self._refresh_list()
        undone = [self.courts[t]["display"] for t, s in self.status.items() if s == "undone"]
        if undone:
            self.banner.configure(text=f"  ⚠ Full courts for {', '.join(undone)} were undone "
                                       f"by a restore. Select the team and click Apply "
                                       f"to put them back.")
            self.banner.pack(fill="x", padx=12, pady=(4, 0), before=self._body)
        else:
            self.banner.pack_forget()
        n = sum(1 for s in self.status.values() if s == "applied")
        self._say(f"{len(self.courts)} courts. {n} with a full court in the game.", self.ok)

    def _run_bg(self, msg, work, done):
        self._busy = True
        self._set_enabled(False)
        self._say(msg, self.th["gold"])

        def progress(text):
            self.after(0, lambda: self._say(text, self.th["gold"]))

        def run():
            try:
                res = work(progress)
                self.after(0, lambda: self._bg_done(done, res, None))
            except Exception as exc:
                self.after(0, lambda e=exc: self._bg_done(done, None, e))
        threading.Thread(target=run, daemon=True).start()

    def _bg_done(self, done, res, err):
        self._busy = False
        self._set_enabled(self.token is not None)
        if err is not None:
            self._say("Full Courts: " + str(err).splitlines()[0], self.th["red"])
            messagebox.showerror("Full Courts", str(err), parent=self)
            return
        done(res)

    def _refresh_list(self):
        keep = self.token
        self.order = sorted(self.courts, key=lambda t: self.courts[t]["display"])
        self.listbox.delete(0, "end")
        for t in self.order:
            mark = STATUS_TEXT.get(self.status.get(t, "none"), "")
            self.listbox.insert("end", f"{self.courts[t]['display']}   {mark}".rstrip())
        if keep in self.order:
            i = self.order.index(keep)
            self.listbox.selection_set(i)
            self.listbox.see(i)

    # ------------------------------------------------------------- selections
    def _team_cfg(self, create=False):
        teams = self.state.setdefault("teams", {})
        if create and self.token not in teams:
            teams[self.token] = {"variants": {}, "logos": {}, "applied": False}
        return teams.get(self.token, {"variants": {}, "logos": {}})

    def _variant_cfg(self):
        return self._team_cfg().get("variants", {}).get(self.variant)

    def _save(self):
        fc.save_state(self.state)
        if self.token:
            had = self.status.get(self.token)
            if had in ("none", "pending"):
                pending = any(v.get("image") for v in self._team_cfg()["variants"].values())
                self.status[self.token] = "pending" if pending else "none"
            self._refresh_list()

    def _on_team(self, _e=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        self.token = self.order[sel[0]]
        team = self.courts[self.token]
        self._choices = [(v, team["labels"].get(v, v)) for v in team["variants"]]
        self.variant_cb.configure(values=[lbl for _v, lbl in self._choices])
        self.variant_cb.current(0)
        self._set_enabled(not self._busy)
        self._on_variant()

    def _on_variant(self, _e=None):
        i = self.variant_cb.current()
        self.variant = self._choices[max(0, i)][0]
        team = self.courts[self.token]
        n = len(team["variants"])
        notes = []
        if n > 1:
            notes.append(f"{team['display']} has {n} courts, one per retro logo. Each can "
                         f"have its own picture; a court left empty uses the "
                         f"default court's picture.")
        logos = team["logos"].get(self.variant, [])
        if logos:
            notes.append("Logos on this court: " + ", ".join(x["texture"] for x in logos) + ".")
        self.variant_note.configure(text=" ".join(notes))
        self._load_picture()
        self._load_logo_controls()

    # ---------------------------------------------------------------- picture
    def _load_picture(self):
        vc = self._variant_cfg()
        self._src = None
        if vc and vc.get("image") and os.path.isfile(vc["image"]):
            try:
                self._src = Image.open(vc["image"]).convert("RGB")
            except OSError:
                self._src = None
        self._draw_preview()

    def _rect(self):
        vc = self._variant_cfg()
        return list(vc["rect"]) if vc else None

    def _draw_preview(self):
        c = self.canvas
        c.delete("all")
        if self._src is None:
            c.create_text(PREVIEW_W / 2, PREVIEW_H / 2, fill=self.th["muted"],
                          font=("Segoe UI", 11), justify="center",
                          text="No picture for this court.\n\nImport Picture… to add one, "
                               "or Export Template… for a starting point.")
            for v in self.rect_vars:
                v.set("")
            return
        w, h = self._src.size
        self._scale = min(PREVIEW_W / w, PREVIEW_H / h)
        pw, ph = max(1, int(w * self._scale)), max(1, int(h * self._scale))
        self._tk_preview = ImageTk.PhotoImage(self._src.resize((pw, ph), Image.BILINEAR))
        self._ox, self._oy = (PREVIEW_W - pw) // 2, (PREVIEW_H - ph) // 2
        c.create_image(self._ox, self._oy, image=self._tk_preview, anchor="nw")
        r = self._rect()
        if r:
            x0, y0, x1, y1 = (self._ox + r[0] * self._scale, self._oy + r[1] * self._scale,
                              self._ox + r[2] * self._scale, self._oy + r[3] * self._scale)
            c.create_rectangle(x0, y0, x1, y1, outline=RECT_COL, width=2)
            mx = (x0 + x1) / 2
            c.create_line(mx, y0, mx, y1, fill=RECT_COL, dash=(4, 4))
            rr = fc.CENTRE_CIRCLE_M * (x1 - x0) / fc.COURT_LEN
            my = (y0 + y1) / 2
            c.create_oval(mx - rr, my - rr, mx + rr, my + rr, outline=RECT_COL, dash=(4, 4))
            for v, val in zip(self.rect_vars, r):
                v.set(str(int(round(val))))

    def _import_court(self):
        path = filedialog.askopenfilename(
            parent=self, title="Court picture",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.tga"), ("All files", "*.*")])
        if not path:
            return
        try:
            dest, size = fc.store_court_image(self.token, self.variant, path)
            rect, found = fc.detect_court_rect(Image.open(dest))
        except Exception as e:
            messagebox.showerror("Full Courts", f"Could not read that image:\n{e}", parent=self)
            return
        team = self._team_cfg(create=True)
        team["variants"][self.variant] = {"image": dest, "size": list(size),
                                          "rect": list(rect), "source": path}
        team.setdefault("logos", {}).setdefault(self.variant,
                                                {"mode": "replace", "source": "court"})
        self._save()
        self._load_picture()
        self._load_logo_controls()
        if found:
            self._say("Court found. Check the cyan box sits on the court's outer lines.",
                      self.ok)
        else:
            self._say("Couldn't find a hardwood court, so the box assumes the template "
                      "layout. Drag its edges onto the court lines.", self.th["gold"])

    def _auto_detect(self):
        vc = self._variant_cfg()
        if not vc or self._src is None:
            return
        rect, found = fc.detect_court_rect(self._src)
        vc["rect"] = list(rect)
        self._save()
        self._draw_preview()
        self._say("Court found." if found else "No hardwood court found; using the "
                  "template layout.", self.ok if found else self.th["gold"])

    def _clear_court(self):
        team = self._team_cfg()
        if self.variant in team.get("variants", {}):
            del team["variants"][self.variant]
            self._save()
        self._load_picture()
        self._load_logo_controls()

    def _on_rect_typed(self, _e=None):
        vc = self._variant_cfg()
        if not vc:
            return
        try:
            r = [float(v.get()) for v in self.rect_vars]
        except ValueError:
            self._draw_preview()
            return
        w, h = vc["size"]
        r = [max(0, min(w, r[0])), max(0, min(h, r[1])), max(0, min(w, r[2])),
             max(0, min(h, r[3]))]
        if r[2] - r[0] < 16 or r[3] - r[1] < 16:
            self._draw_preview()
            return
        vc["rect"] = r
        self._save()
        self._draw_preview()

    def _drag_start(self, e):
        r = self._rect()
        if not r or self._src is None:
            return
        sx = [self._ox + r[0] * self._scale, self._ox + r[2] * self._scale]
        sy = [self._oy + r[1] * self._scale, self._oy + r[3] * self._scale]
        cands = [(abs(e.x - sx[0]), 0), (abs(e.x - sx[1]), 2),
                 (abs(e.y - sy[0]), 1), (abs(e.y - sy[1]), 3)]
        d, idx = min(cands)
        self._drag = idx if d <= 10 else None

    def _drag_move(self, e):
        if self._drag is None:
            return
        vc = self._variant_cfg()
        w, h = vc["size"]
        if self._drag in (0, 2):
            val = max(0, min(w, (e.x - self._ox) / self._scale))
        else:
            val = max(0, min(h, (e.y - self._oy) / self._scale))
        r = vc["rect"]
        r[self._drag] = val
        if r[2] - r[0] < 16 or r[3] - r[1] < 16:
            return
        self._draw_preview()

    def _drag_end(self, _e):
        if self._drag is not None:
            self._drag = None
            self._save()
            self._load_logo_controls()

    def _export_template(self):
        path = filedialog.asksaveasfilename(parent=self, title="Save court template",
                                            defaultextension=".png",
                                            initialfile="full_court_template.png",
                                            filetypes=[("PNG", "*.png")])
        if not path:
            return
        fc.make_template().save(path)
        self._say(f"Template saved: {path}", self.ok)

    # ------------------------------------------------------------------- logo
    def _logo_cfg(self):
        return self._team_cfg().get("logos", {}).get(self.variant) or \
            {"mode": "replace", "source": "court"}

    def _on_strip_change(self):
        if not self.token:
            return
        team = self._team_cfg(create=True)
        team.setdefault("strips", {})[self.variant] = {"mode": self.strip_mode.get()}
        self._save()
        self._draw_strip()

    def _draw_strip(self):
        c = self.strip_canvas
        c.delete("all")
        if not self.token:
            return
        sc = self._team_cfg().get("strips", {}).get(self.variant) or {}
        self.strip_mode.set(sc.get("mode", "picture"))
        if self.strip_mode.get() == "keep":
            c.create_text(STRIP_PX[0] / 2, STRIP_PX[1] / 2, text="the game's own text",
                          fill=self.th["muted"])
            return
        try:
            img = fc.strip_image_for(self._team_cfg(), self.variant)
        except Exception:
            img = None
        if img is None:
            return
        bg = Image.new("RGBA", img.size, (40, 40, 40, 255))
        bg.alpha_composite(img)
        self._tk_strip = ImageTk.PhotoImage(bg.resize(STRIP_PX, Image.LANCZOS))
        c.create_image(0, 0, image=self._tk_strip, anchor="nw")

    def _load_logo_controls(self):
        self._draw_strip()
        lc = self._logo_cfg()
        self.logo_mode.set(lc.get("mode", "replace"))
        self.logo_source.set(lc.get("source", "court"))
        self.logo_scale.set(round(float(lc.get("scale", fc.DECAL_CIRCLE_FRACTION)) * 100))
        self._draw_logo()

    def _on_scale_drag(self):
        """Live preview while dragging; saved on release."""
        if not self.token:
            return
        lc = self._team_cfg(create=True).setdefault("logos", {}).setdefault(self.variant, {})
        lc["scale"] = round(self.logo_scale.get()) / 100
        self._draw_logo()

    def _on_scale_done(self):
        self._on_scale_drag()
        if self.token:
            self._save()

    def _reset_scale(self):
        self.logo_scale.set(round(fc.DECAL_CIRCLE_FRACTION * 100))
        self._on_scale_done()

    def _on_logo_change(self):
        if not self.token:
            return
        team = self._team_cfg(create=True)
        lc = team.setdefault("logos", {}).setdefault(self.variant, {})
        lc["mode"] = self.logo_mode.get()
        lc["source"] = self.logo_source.get()
        self._save()
        self._draw_logo()

    def _choose_logo(self):
        path = filedialog.askopenfilename(
            parent=self, title="Centre logo (PNG with transparency works best)",
            filetypes=[("Images", "*.png *.webp *.jpg *.jpeg"), ("All files", "*.*")])
        if not path:
            return
        try:
            dest = fc.store_logo_image(self.token, self.variant, path)
        except Exception as e:
            messagebox.showerror("Full Courts", f"Could not read that image:\n{e}", parent=self)
            return
        team = self._team_cfg(create=True)
        lc = team.setdefault("logos", {}).setdefault(self.variant, {})
        lc.update(mode="replace", source="file", image=dest)
        self._save()
        self._load_logo_controls()

    def _draw_logo(self):
        c = self.logo_canvas
        c.delete("all")
        replace = self.logo_mode.get() == "replace"
        for w in (self.src_court_rb, self.src_file_rb, self.scale_w, self.scale_reset):
            w.configure(state="normal" if replace else "disabled")
        self.scale_lbl.configure(text=f"{round(self.logo_scale.get())}%")
        self.logo_btn.configure(state="normal" if (replace and self.token and not self._busy)
                                else "disabled")
        team = self.courts.get(self.token) if self.token else None
        notes = []
        if self.logo_mode.get() == "hide":
            notes.append("No centre logo. The team-select panel and scoreboard logo "
                         "go blank too.")
        elif self.logo_mode.get() == "keep":
            notes.append("The game draws its own logo over the centre of your picture.")
        else:
            notes.append("Size: about 32% fills the centre circle; 100% is the game's "
                         "full decal, about three times wider. Also shown on the "
                         "team-select panel and the scoreboard.")
            try:
                img = fc.logo_image_for(team, self._team_cfg(), self.variant, (LOGO_PX, LOGO_PX))
            except Exception:
                img = None
            if img is not None:
                bg = Image.new("RGBA", img.size, (60, 60, 60, 255))
                bg.alpha_composite(img)
                self._tk_logo = ImageTk.PhotoImage(bg)
                c.create_image(0, 0, image=self._tk_logo, anchor="nw")
            else:
                c.create_text(LOGO_PX / 2, LOGO_PX / 2, text="no image yet",
                              fill=self.th["muted"])
        if team and team["shared_logos"]:
            notes.append("Another team uses the same logo texture ("
                         + ", ".join(sorted(set(team["shared_logos"]))[:3])
                         + "), so it changes there too.")
        self.logo_note.configure(text=" ".join(notes))

    # ---------------------------------------------------------------- actions
    def _apply(self):
        if not self.token:
            return
        cfg = self._team_cfg()
        if not any(v.get("image") for v in cfg.get("variants", {}).values()):
            messagebox.showinfo("Full Courts", "Import a court picture first.", parent=self)
            return
        token = self.token
        self._run_bg("Applying…",
                     lambda progress: fc.apply(self.game_data_path, [token], progress),
                     lambda _n: self._after_write(f"Applied. Start a {self.courts[token]['display']} "
                                                  f"home game to see it."))

    def _revert(self):
        if not self.token:
            return
        if self.status.get(self.token) not in ("applied", "undone"):
            self._say("This team has no full court in the game.")
            return
        if not messagebox.askyesno("Full Courts",
                                   f"Put {self.courts[self.token]['display']}'s stock court "
                                   f"and logos back? Your pictures stay saved.", parent=self):
            return
        token = self.token
        self._run_bg("Reverting…",
                     lambda progress: fc.revert(self.game_data_path, token, progress),
                     lambda _ok: self._after_write("Reverted to the stock court."))

    def _after_write(self, msg):
        self.state = fc.load_state()
        self._run_bg("Checking the game…", self._bg_load,
                     lambda res: (self._after_load(res), self._say(msg, self.ok)))

    def set_game_path(self, path):
        if path == self.game_data_path:
            return
        self.game_data_path = path or ""
        self.courts, self.status, self.token = {}, {}, None
        self.listbox.delete(0, "end")
        self._set_enabled(False)
        if self._loaded:
            self.load(silent=True)


if __name__ == "__main__":
    import game_layout
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    if path:
        game_layout.configure(path, fc, fc.tm)
    root = tk.Tk()
    root.title("Full Courts")
    root.geometry("1280x760")
    tab = FullCourtsTab(root, path)
    tab.pack(fill="both", expand=True)
    root.after(100, tab.load)
    root.mainloop()
