"""
avatar_tab.py -- Avatar and Accessory tabs for the selected custom roster player.

    AvatarTab(parent, model, theme=..., host=app)       skin / face / hair / body / fine-tune
    AccessoryTab(parent, model, theme=..., host=app)    hats, goggles, sneakers, FX, limbs

Both edit the shared roster_manager.RosterData; every change is stored only as an
override against the player's template (BounceRosterLooks.csv). Imports nothing from
app.py and never calls apply_single_mod().
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import colorchooser, ttk

import roster_manager as rmg

DEFAULT_THEME = {
    "bg": "#1a1a2e", "panel": "#16213e", "accent": "#0f3460",
    "text": "#eaeaea", "muted": "#a0a0b0", "entry": "#0d1b2a",
    "gold": "#FDB927", "red": "#e94560",
}
_photo_cache = {}
PREVIEW_SIZE = 220          # avatar preview canvas (px)


def init_styles(widget, th):
    s = ttk.Style(widget)
    s.configure("Ros.TButton", background=th["accent"], foreground="#FFFFFF",
                font=("Segoe UI", 10), relief="flat", padding=5)
    s.map("Ros.TButton", background=[("active", "#1a4a80"), ("disabled", "#555")],
          foreground=[("disabled", "#bbbbbb")])


def thumb_photo(src, size):
    """PhotoImage for a preview path or a callable returning a PIL image; cached."""
    from PIL import Image, ImageTk
    key = (src if isinstance(src, str) else id(src), size)
    if key in _photo_cache:
        return _photo_cache[key]
    ph = None
    try:
        im = Image.open(src).convert("RGBA") if isinstance(src, str) else src()
        if im.size != (size, size):
            im = im.resize((size, size), Image.LANCZOS)
        ph = ImageTk.PhotoImage(im)
    except Exception:
        ph = None
    _photo_cache[key] = ph
    return ph


# ------------------------------------------------------------ scroll frame
class ScrollFrame(tk.Frame):
    def __init__(self, master, bg, **kw):
        super().__init__(master, bg=bg, **kw)
        self.cv = tk.Canvas(self, bg=bg, highlightthickness=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview)
        self.cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.cv.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.cv, bg=bg)
        self._win = self.cv.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self._win, width=e.width))
        for w in (self.cv, self.inner):
            w.bind("<MouseWheel>", self._wheel)
            w.bind("<Button-4>", self._wheel)
            w.bind("<Button-5>", self._wheel)

    def _wheel(self, e):
        d = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
        self.cv.yview_scroll(d * 2, "units")


# -------------------------------------------------------------- thumb grid
class ThumbGrid(tk.Frame):
    """Scrollable, lazily drawn grid of thumbnails; click selects.

    options: [(value, label, preview, locked, approx)]; preview = path | callable | None.
    """

    def __init__(self, master, th, on_pick, cell_w=88, cell_h=92, thumb=64, max_rows=2):
        super().__init__(master, bg=th["panel"])
        self.th, self.on_pick = th, on_pick
        self.cw, self.ch, self.thumb, self.max_rows = cell_w, cell_h, thumb, max_rows
        self.options, self.selected = [], None
        self.cv = tk.Canvas(self, bg=th["entry"], highlightthickness=0, height=cell_h)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.cv.configure(yscrollcommand=self.sb.set)
        self.cv.pack(side="left", fill="both", expand=True)
        self._sb_shown = False
        self.cv.bind("<Configure>", lambda e: self._relayout())
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<MouseWheel>", self._wheel)
        self.cv.bind("<Button-4>", self._wheel)
        self.cv.bind("<Button-5>", self._wheel)

    def _yview(self, *a):
        self.cv.yview(*a)
        self._draw()

    def _wheel(self, e):
        d = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
        if self._rows() > self.max_rows:
            self.cv.yview_scroll(d, "units")
            self._draw()
        return "break"

    def _cols(self):
        return max(1, self.cv.winfo_width() // self.cw)

    def _rows(self):
        return -(-len(self.options) // self._cols()) if self.options else 1

    def set_options(self, options, selected):
        self.options, self.selected = options, selected
        self._relayout()

    def set_selected(self, selected):
        self.selected = selected
        self._draw()

    def _relayout(self):
        rows = self._rows()
        self.cv.configure(height=min(rows, self.max_rows) * self.ch,
                          scrollregion=(0, 0, 1, rows * self.ch))
        need = rows > self.max_rows
        if need and not self._sb_shown:
            self.sb.pack(side="right", fill="y")
            self._sb_shown = True
        elif not need and self._sb_shown:
            self.sb.pack_forget()
            self._sb_shown = False
        if need:
            sel = next((i for i, o in enumerate(self.options) if o[0] == self.selected), 0)
            self.cv.yview_moveto(max(0, (sel // self._cols()) * self.ch / (rows * self.ch)))
        else:
            self.cv.yview_moveto(0)
        self._draw()

    def _draw(self):
        c = self.cv
        c.delete("all")
        cols = self._cols()
        top = int(c.canvasy(0))
        r0 = max(0, top // self.ch)
        r1 = (top + c.winfo_height()) // self.ch + 1
        pad = (self.cw - self.thumb) // 2
        for i in range(r0 * cols, min(len(self.options), (r1 + 1) * cols)):
            val, label, prev, locked, approx = self.options[i][:5]
            x, y = (i % cols) * self.cw, (i // cols) * self.ch
            sel = val == self.selected
            c.create_rectangle(x + 2, y + 2, x + self.cw - 2, y + self.ch - 2,
                               outline=self.th["gold"] if sel else self.th["accent"],
                               width=3 if sel else 1,
                               fill=self.th["accent"] if sel else self.th["panel"])
            ph = thumb_photo(prev, self.thumb) if prev else None
            if ph is not None:
                c.create_image(x + pad, y + 5, image=ph, anchor="nw")
            else:
                c.create_text(x + self.cw // 2, y + 5 + self.thumb // 2, text="none" if val == "" else "no preview",
                              fill=self.th["muted"], font=("Segoe UI", 8))
            c.create_text(x + self.cw // 2, y + self.ch - 14, text=str(label)[:14],
                          fill=self.th["text"], font=("Segoe UI", 7))
            if locked:
                c.create_rectangle(x + 4, y + 4, x + 38, y + 17, fill=self.th["red"], outline="")
                c.create_text(x + 21, y + 10, text="DLC/lock", fill="#fff", font=("Segoe UI", 6, "bold"))
            elif approx and prev:
                c.create_text(x + self.cw - 8, y + 10, text="~", fill=self.th["muted"], font=("Segoe UI", 8))

    def _click(self, e):
        cols = self._cols()
        col, row = e.x // self.cw, int(self.cv.canvasy(e.y)) // self.ch
        if col >= cols:
            return
        i = row * cols + col
        if 0 <= i < len(self.options):
            self.selected = self.options[i][0]
            self._draw()
            self.on_pick(self.selected)


# ----------------------------------------------------------- colour swatches
class ColorGrid(tk.Frame):
    def __init__(self, master, th, palette, on_pick, size=18, per_row=18):
        super().__init__(master, bg=th["panel"])
        self.th, self.palette, self.on_pick, self.size = th, palette, on_pick, size
        self.per_row, self.cur = per_row, None
        rows = -(-len(palette) // per_row)
        self.cv = tk.Canvas(self, bg=th["panel"], highlightthickness=0,
                            width=per_row * (size + 2) + 2, height=rows * (size + 2) + 2)
        self.cv.pack(side="left")
        self.cv.bind("<Button-1>", self._click)
        side = tk.Frame(self, bg=th["panel"])
        side.pack(side="left", padx=8, anchor="n")
        self.chip = tk.Label(side, width=6, height=2, relief="solid", bd=1)
        self.chip.pack(anchor="w")
        self.lbl = tk.Label(side, bg=th["panel"], fg=th["muted"], font=("Segoe UI", 8))
        self.lbl.pack(anchor="w")
        ttk.Button(side, text="Custom...", style="Ros.TButton", command=self._custom).pack(anchor="w", pady=2)
        self._draw()

    def set_color(self, hexv):
        self.cur = (hexv or "").upper()
        try:
            self.chip.config(bg=self.cur)
        except tk.TclError:
            pass
        self.lbl.config(text=self.cur)
        self._draw()

    def _draw(self):
        c, s = self.cv, self.size + 2
        c.delete("all")
        for i, hx in enumerate(self.palette):
            x, y = 2 + (i % self.per_row) * s, 2 + (i // self.per_row) * s
            on = hx.upper() == self.cur
            c.create_rectangle(x, y, x + self.size, y + self.size, fill=hx,
                               outline=self.th["gold"] if on else self.th["bg"], width=3 if on else 1)

    def _click(self, e):
        s = self.size + 2
        i = ((e.y - 2) // s) * self.per_row + (e.x - 2) // s
        if (e.x - 2) // s < self.per_row and 0 <= i < len(self.palette):
            self.on_pick(self.palette[i].upper())

    def _custom(self):
        rgb, hx = colorchooser.askcolor(color=self.cur or "#808080", parent=self)
        if hx:
            self.on_pick(hx.upper())


# --------------------------------------------------------------- base class
class _EditorTab(tk.Frame):
    """Shared plumbing: header, preview panel, sub-notebook of control pages."""
    PAGES: list = []        # [(title, [spec...])]  spec = (kind, name, title)

    def __init__(self, master, model, theme=None, host=None, on_save=None, **kw):
        self.th = dict(DEFAULT_THEME)
        if theme:
            self.th.update(theme)
        super().__init__(master, bg=self.th["bg"], **kw)
        self.model, self.host, self.on_save = model, host, on_save
        self._filling = False
        self._prev_job = None
        self._hq_job = None
        self._prev_lock = threading.Lock()
        self._prev_busy = False
        self._prev_want = None
        self._prev_seq = 0
        self._prev_shown = 0
        self._photo = None
        self.widgets = {}      # name -> (kind, widget, info label, var)
        self.page_controls = {}
        init_styles(self, self.th)
        self._build()
        model.listeners.append(self._on_model)
        self.refresh()

    def say(self, msg, color=None):
        if self.host is not None and hasattr(self.host, "status"):
            pass
        self.msg.config(text=msg, fg=color or self.th["muted"])

    # ----------------------------------------------------------------- build
    def _build(self):
        t = self.th
        head = tk.Frame(self, bg=t["panel"])
        head.pack(fill="x", padx=8, pady=(8, 4))
        self.who = tk.Label(head, bg=t["panel"], fg=t["gold"], font=("Segoe UI", 11, "bold"), anchor="w")
        self.who.pack(side="left", padx=10, pady=6)
        self.dirty_lbl = tk.Label(head, bg=t["panel"], fg=t["gold"], font=("Segoe UI", 9))
        self.dirty_lbl.pack(side="right", padx=8)
        ttk.Button(head, text="Save", style="Ros.TButton", command=self._save).pack(side="right", padx=4, pady=4)
        self.msg = tk.Label(self, bg=t["bg"], fg=t["muted"], anchor="w", font=("Segoe UI", 9))
        self.msg.pack(fill="x", padx=12)

        self.empty = tk.Label(self, text="Select a player in the Roster tab.", bg=t["bg"], fg=t["muted"],
                              font=("Segoe UI", 12))
        self.body = tk.Frame(self, bg=t["bg"])
        right = tk.Frame(self.body, bg=t["panel"], width=230)
        right.pack(side="right", fill="y", padx=(8, 0))
        right.pack_propagate(False)
        tk.Label(right, text="Preview", bg=t["panel"], fg=t["text"],
                 font=("Segoe UI", 10, "bold")).pack(pady=(10, 2))
        self.prev_cv = tk.Canvas(right, width=PREVIEW_SIZE, height=PREVIEW_SIZE, bg=t["entry"],
                                 highlightthickness=0)
        self.prev_cv.pack(pady=4)
        tk.Label(right, text="Built like the game builds it: face layers placed by the sliders, "
                             "head / hair / hat meshes, hair tinted by hairColor. Lighting is "
                             "simplified, so colours can differ slightly from the in-game picker.",
                 bg=t["panel"], fg=t["muted"], font=("Segoe UI", 8), justify="left",
                 wraplength=210).pack(padx=8, pady=4)
        self.nb = ttk.Notebook(self.body)
        self.nb.pack(side="left", fill="both", expand=True)
        for title, specs in self.PAGES:
            page = tk.Frame(self.nb, bg=t["bg"])
            self.nb.add(page, text=title)
            bar = tk.Frame(page, bg=t["bg"])
            bar.pack(fill="x", pady=(4, 0))
            ttk.Button(bar, text="Reset to template", style="Ros.TButton",
                       command=lambda sp=specs: self._reset_page(sp)).pack(side="right", padx=8)
            sf = ScrollFrame(page, t["bg"])
            sf.pack(fill="both", expand=True)
            for spec in specs:
                self._add_control(sf.inner, spec)

    def _add_control(self, parent, spec):
        t, kind, name, title = self.th, spec[0], spec[1], spec[2]
        box = tk.Frame(parent, bg=t["panel"])
        box.pack(fill="x", padx=6, pady=4)
        hdr = tk.Frame(box, bg=t["panel"])
        hdr.pack(fill="x", padx=6, pady=(4, 2))
        tk.Label(hdr, text=title, bg=t["panel"], fg=t["text"], font=("Segoe UI", 10, "bold")).pack(side="left")
        info = tk.Label(hdr, bg=t["panel"], fg=t["muted"], font=("Segoe UI", 8))
        info.pack(side="left", padx=8)
        if kind == "grid":
            g = ThumbGrid(box, t, lambda v, n=name: self._pick(n, v))
            g.pack(fill="x", padx=6, pady=(0, 6))
            self.widgets[name] = ("grid", g, info, None)
        elif kind == "color":
            cg = ColorGrid(box, t, self.model.ensure_catalog().palette if self.model.ensure_catalog() else [],
                           lambda v, n=name: self._pick_color(n, v))
            cg.pack(fill="x", padx=6, pady=(0, 6))
            self.widgets[name] = ("color", cg, info, None)
        elif kind == "choice":
            # spec[3] = [(label, value), ...]; stored as the float the game expects.
            choices = spec[3]
            var = tk.DoubleVar(value=choices[0][1])
            cb = ttk.Combobox(box, state="readonly", width=12, values=[c[0] for c in choices])
            cb.pack(anchor="w", padx=6, pady=(0, 6))

            def picked(_e, n=name, cb=cb, var=var, choices=choices):
                var.set(dict(choices)[cb.get()])
                self._slide(n)
            cb.bind("<<ComboboxSelected>>", picked)
            cb.choices = choices
            self.widgets[name] = ("choice", cb, info, var)
        else:
            var = tk.DoubleVar(value=0.5)
            sc = tk.Scale(box, from_=0, to=1, resolution=0.005, orient="horizontal", variable=var,
                          showvalue=True, bg=t["panel"], fg=t["text"], troughcolor=t["entry"],
                          highlightthickness=0, command=lambda _v, n=name: self._slide(n))
            sc.pack(fill="x", padx=6, pady=(0, 6))
            self.widgets[name] = ("float", sc, info, var)
        self.widgets[name] += (box,)

    # ---------------------------------------------------------------- events
    def _player(self):
        pid = self.model.selected
        return self.model.get(pid) if pid is not None else None

    def _on_model(self, kind):
        if kind in ("select", "load"):
            self.refresh()
        else:
            self._update_dirty()

    def _update_dirty(self):
        self.dirty_lbl.config(text="* Unsaved changes" if self.model.dirty else "")

    def _save(self):
        if self.on_save:
            self.on_save()

    def _pick(self, name, value):
        p = self._player()
        if p and self.model.set_choice(p, name, value):
            self.refresh(keep_msg=True)
            self.say(f"{name} = {value or 'None'} (unsaved).", self.th["gold"])

    def _pick_color(self, name, hexv):
        p = self._player()
        if p and self.model.set_look(p, name, hexv):
            self.model.touch()
            self.refresh(keep_msg=True)

    def _slide(self, name):
        if self._filling:
            return
        p = self._player()
        if not p:
            return
        v = self.widgets[name][3].get()
        if self.model.set_look(p, name, float(v)):
            self.model.touch()
            self._schedule_preview()

    def _reset_page(self, specs):
        p = self._player()
        if not p:
            return
        names = [s[1] for s in specs]
        extra = {"skinTexture": ["skinTextureHead", "noseShadow"], "eyesTexture": ["eyesIrisTexture", "eyesBrightTexture"],
                 "hairMesh": ["hairTexture", "hairTextureHead"], "hatMesh": ["hatTexture"],
                 "gogglesMesh": ["gogglesTexture"], "sneakersMesh": ["sneakersTexture"]}
        for n in list(names):
            names += extra.get(n, [])
            names += [k for k in p.looks if k.startswith(n + "_LINK_")]
        n = self.model.reset_controls(p, names)
        self.refresh()
        self.say(f"Reset {n} override(s) to the template." if n else "Already matches the template.")

    # --------------------------------------------------------------- refresh
    def refresh(self, keep_msg=False):
        p = self._player()
        if p is None or not self.model.ensure_catalog():
            self.body.pack_forget()
            self.empty.config(text="Select a player in the Roster tab." if self.model.catalog
                              else self.model.catalog_error or "Catalog unavailable.")
            self.empty.pack(pady=40)
            self.who.config(text="")
            self._update_dirty()
            return
        self.empty.pack_forget()
        self.body.pack(fill="both", expand=True, padx=8, pady=4)
        cat, vals = self.model.catalog, self.model.effective(p)
        tvals = self.model.template_vals(p)
        self.who.config(text=f"{p.name}  #{p.number}  ({rmg.TEAM_BY_ID.get(p.team_id, '?')}, {p.role}; "
                             f"template {p.template_id})")
        self._filling = True
        for name, w in self.widgets.items():
            kind, widget, info = w[0], w[1], w[2]
            ov = name in p.looks
            note = "overridden" if ov else "template"
            if kind == "grid":
                opts = cat.options(name, vals)
                widget.set_options(opts, vals.get(name, ""))
                tv = tvals.get(name)
                if tv is not None and not isinstance(tv, str):
                    note += "; template stores a number here, the plugin may ignore text"
                cur = vals.get(name, "")
                note = f"{cur or 'None'}  ({note}, {len(opts)} options)"
            elif kind == "color":
                widget.set_color(vals.get(name, "#808080") if isinstance(vals.get(name), str) else "#808080")
            else:
                v = vals.get(name)
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    v = cat.float_default(name)
                w[3].set(v)
                if kind == "choice":
                    widget.set(min(widget.choices, key=lambda c: abs(c[1] - v))[0])
                note = f"{note}; default {cat.float_default(name):g}"
            info.config(text=note, fg=self.th["gold"] if ov else self.th["muted"])
            self._toggle_visibility(name, vals, tvals)
        self._filling = False
        self._update_dirty()
        self._schedule_preview()

    def _toggle_visibility(self, name, vals, tvals):
        pass

    def _schedule_preview(self):
        """Quick draft shortly after a change, full quality once input settles.
        Rendering runs on a worker thread so sliders stay responsive."""
        for job in (self._prev_job, self._hq_job):
            if job:
                self.after_cancel(job)
        self._prev_job = self.after(30, lambda: self._request_preview("fast"))
        self._hq_job = self.after(260, lambda: self._request_preview("high"))

    def _draw_preview(self):
        self._request_preview("high")

    def _request_preview(self, quality):
        if quality == "fast":
            self._prev_job = None
        else:
            self._hq_job = None
        p = self._player()
        if not p or not self.model.catalog:
            return
        vals = dict(self.model.effective(p))
        with self._prev_lock:
            self._prev_seq += 1
            self._prev_want = (self._prev_seq, vals, quality)
            if self._prev_busy:
                return
            self._prev_busy = True
        threading.Thread(target=self._preview_worker, daemon=True).start()

    def _preview_worker(self):
        while True:
            with self._prev_lock:
                req, self._prev_want = self._prev_want, None
                if req is None:
                    self._prev_busy = False
                    return
            seq, vals, quality = req
            try:
                im, err = rmg.render_avatar(self.model.catalog, vals, PREVIEW_SIZE, self.th["entry"],
                                            quality=quality), None
            except Exception as e:  # shown in the panel
                im, err = None, e
            try:
                self.after(0, self._show_preview, seq, im, err)
            except (RuntimeError, tk.TclError):   # tab closed
                with self._prev_lock:
                    self._prev_busy = False
                return

    def _show_preview(self, seq, im, err):
        if seq < self._prev_shown:
            return                              # an older render finished late
        self._prev_shown = seq
        self.prev_cv.delete("all")
        if im is None:
            self.prev_cv.create_text(PREVIEW_SIZE // 2, PREVIEW_SIZE // 2, text=f"preview unavailable\n{err}",
                                     fill=self.th["muted"], width=PREVIEW_SIZE - 10)
            return
        from PIL import ImageTk
        self._photo = ImageTk.PhotoImage(im)
        self.prev_cv.create_image(0, 0, image=self._photo, anchor="nw")


# --------------------------------------------------------------------- tabs
class AvatarTab(_EditorTab):
    PAGES = [
        ("Skin & Head", [("grid", "skinTexture", "Skin (also sets head skin and nose shadow)"),
                         ("grid", "headMesh", "Head shape"),
                         ("float", "headSizeX", "Head width"), ("float", "headSizeY", "Head height")]),
        ("Face", [("grid", "eyesTexture", "Eyes (matched to skin)"),
                  ("grid", "eyesIrisTexture", "Iris"), ("grid", "eyesBrightTexture", "Eye shine"),
                  ("grid", "eyeBlink", "Blink"), ("grid", "eyebrowsTexture", "Eyebrows"),
                  ("grid", "mouthTexture", "Mouth (matched to skin)"), ("grid", "noseMesh", "Nose"),
                  ("color", "eyesColor", "Eye colour"), ("color", "eyebrowsColor", "Eyebrow colour")]),
        ("Hair", [("grid", "hairMesh", "Hair style (filtered by hat)"),
                  ("grid", "hairTexturePattern", "Hair pattern"),
                  ("color", "hairColor", "Hair colour")]),
        ("Body", [("float", "slimFat", "Slim .. fat"), ("float", "weakStrong", "Weak .. strong"),
                  ("float", "handsSize", "Hand size"), ("choice", "maleOrFemale", "Gender", [("Male", 0.0), ("Female", 1.0)])]),
        ("Fine-tune", [("float", n, t) for n, t in [
            ("eyesScale", "Eye size"), ("eyesSeparate", "Eye distance"),
            ("eyesMoveVertical", "Eye height"), ("eyesRotate", "Eye tilt"),
            ("eyebrowsScale", "Eyebrow size"), ("eyebrowsSeparate", "Eyebrow distance"),
            ("eyebrowsMoveVertical", "Eyebrow height"), ("eyebrowsRotate", "Eyebrow tilt"),
            ("mouthScale", "Mouth size"), ("mouthMoveVertical", "Mouth height"),
            ("mouthRotate", "Mouth tilt"), ("noseScale", "Nose size"),
            ("noseMoveVertical", "Nose height")]]),
    ]


class AccessoryTab(_EditorTab):
    PAGES = [
        ("Headwear", [("grid", "hatMesh", "Hat (None = no hat)"),
                      ("grid", "hatTexture", "Hat texture (matches hat)"),
                      ("grid", "gogglesMesh", "Goggles"), ("grid", "gogglesTexture", "Goggles texture")]),
        ("Feet & FX", [("grid", "sneakersMesh", "Sneakers"),
                       ("grid", "sneakersTexture", "Sneaker texture (matches sneakers)"),
                       ("grid", "sprintFX", "Sprint FX"), ("grid", "footprints", "Footprints")]),
        ("Limbs", [("grid", "armLeft", "Left arm"), ("grid", "armRight", "Right arm"),
                   ("grid", "legLeft", "Left leg"), ("grid", "legRight", "Right leg"),
                   ("color", "armLeftColor", "Left arm colour"), ("color", "armRightColor", "Right arm colour"),
                   ("color", "legLeftColor", "Left leg colour"), ("color", "legRightColor", "Right leg colour"),
                   ("float", "numbersMove", "Jersey number position")]),
    ]

    def _toggle_visibility(self, name, vals, tvals):
        # Limb colours are always editable: the Bounce Roster plugin (1.1+) adds the control
        # to templates that don't carry it.
        if name.endswith("Color") and name.startswith(("arm", "leg")):
            box = self.widgets[name][4]
            box.pack(fill="x", padx=6, pady=4)
