"""
mesh_tab.py -- the Meshes tab: browse, view, export and replace 3D models.

Self-contained in the same way as save_tab.py and slider_tab.py: it imports
nothing from app.py. All asset work lives in mesh_manager.py, all drawing in
mesh_view.py. Embed it:

    from mesh_tab import MeshTab
    MeshTab(parent, cfg, host=app, theme={...}).pack(fill="both", expand=True)

or run it on its own for a quick look:

    python mesh_tab.py

APPLY ORDER MATTERS. Textures and meshes in the same .assets file share one
.resS companion, and both append their bulk data to it after rewinding it to
its backup once per run. So the host applies both from a single pipeline with a
shared `ress_reset` set (see apply_all below); applying meshes on their own
would rewind the .resS out from under the texture mods and vice versa.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import mesh_manager as mm
from mesh_view import MODES, MeshView

DEFAULT_THEME = {
    "bg": "#08101E", "panel": "#0E1828", "ebg": "#060C16", "accent": "#1D428A",
    "hi": "#C8102E", "hi_active": "#A00D24", "soft_active": "#2550A8",
    "text": "#ECF0FF", "sub": "#5B7AA0", "gold": "#FDB927",
    "dis_bg": "#172030", "dis_fg": "#364E68",
}


class MeshTab(tk.Frame):
    """Browser + 3D viewer + import/export for every Mesh in the game."""

    def __init__(self, parent, cfg=None, host=None, theme=None):
        self.C = dict(DEFAULT_THEME)
        self.C.update(theme or {})
        super().__init__(parent, bg=self.C["bg"], highlightthickness=0)

        self.cfg  = cfg if cfg is not None else {}
        self.host = host

        self.all_meshes      = []
        self.filtered_meshes = []
        self.selected        = None
        self.orig_geo        = None
        self.mod_geo         = None
        self.write_info      = None
        self.preview_texture = None
        self._loading        = False
        self._loaded_once    = False
        self._sort_col       = None
        self._sort_asc       = True
        self._showing        = "original"

        self.mesh_mods = (mm.load_mesh_meta(self.cfg.get("mods_folder", ""))
                          if self.cfg.get("mods_folder") else {})

        self._ensure_styles()
        self._build_ui()

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _ensure_styles(self):
        """Reuse the host app's ttk styles; define them if we're standalone."""
        style = ttk.Style(self)
        # style.lookup() would answer with TButton's inherited value even when
        # Accent.TButton was never defined; style.configure() with no options
        # returns only what was actually set on that style, so it's the honest
        # test for "is the host app's palette already loaded?".
        if style.configure("Accent.TButton"):
            return
        C = self.C
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=C["bg"], foreground=C["text"],
                        font=("Segoe UI", 10))
        style.configure("TFrame", background=C["bg"])
        style.configure("Panel.TFrame", background=C["panel"])
        style.configure("TLabel", background=C["bg"], foreground=C["text"])
        style.configure("Sub.TLabel", background=C["panel"], foreground=C["sub"],
                        font=("Segoe UI", 9))
        style.configure("TEntry", fieldbackground=C["ebg"], foreground=C["text"])
        style.configure("Accent.TButton", background=C["hi"], foreground="#FFFFFF",
                        font=("Segoe UI", 10, "bold"), relief="flat", padding=6)
        style.map("Accent.TButton", background=[("active", C["hi_active"]),
                                                ("disabled", C["dis_bg"])],
                  foreground=[("disabled", C["dis_fg"])])
        style.configure("Soft.TButton", background=C["accent"], foreground="#FFFFFF",
                        font=("Segoe UI", 10), relief="flat", padding=6)
        style.map("Soft.TButton", background=[("active", C["soft_active"]),
                                              ("disabled", C["dis_bg"])],
                  foreground=[("disabled", C["dis_fg"])])
        style.configure("Treeview", background=C["ebg"], foreground=C["text"],
                        fieldbackground=C["ebg"], rowheight=24)
        style.configure("Treeview.Heading", background=C["accent"],
                        foreground="#FFFFFF", font=("Segoe UI", 10, "bold"),
                        relief="flat")
        style.map("Treeview", background=[("selected", C["hi"])],
                  foreground=[("selected", "#FFFFFF")])

    def _status(self, msg):
        if self.host is not None and hasattr(self.host, "status_var"):
            try:
                self.host.status_var.set(msg)
                return
            except Exception:
                pass
        self.local_status.set(msg)

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        C = self.C
        self.local_status = tk.StringVar(value="")

        paned = tk.PanedWindow(self, orient=tk.HORIZONTAL, sashwidth=6,
                               sashrelief="flat", bg=C["accent"], borderwidth=0)
        paned.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_list_panel(paned)
        self._build_view_panel(paned)

    def _build_list_panel(self, parent):
        C = self.C
        frame = ttk.Frame(parent, style="Panel.TFrame")
        parent.add(frame, minsize=260, width=380, stretch="always")
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        sf = ttk.Frame(frame, style="Panel.TFrame")
        sf.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        sf.columnconfigure(1, weight=1)
        ttk.Label(sf, text="🔍", background=C["panel"]).grid(row=0, column=0, padx=(0, 4))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refresh_list())
        ttk.Entry(sf, textvariable=self.search_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(sf, text="⟳", width=3, style="Soft.TButton",
                   command=lambda: self.load(force=True)).grid(row=0, column=2, padx=(6, 0))

        tf = ttk.Frame(frame, style="Panel.TFrame")
        tf.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tf, columns=("name", "verts", "tris", "mod"),
                                 show="headings", selectmode="browse")
        # only the name column stretches: the numeric columns keep their width
        # so the "Mod" tick can't be squeezed off the right edge
        for col, label, width, anchor, stretch in (("name", "Mesh Name", 150, "w", True),
                                                   ("verts", "Verts", 62, "e", False),
                                                   ("tris", "Tris", 62, "e", False),
                                                   ("mod", "Mod", 46, "center", False)):
            self.tree.heading(col, text=label, command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=width, anchor=anchor, stretch=stretch,
                             minwidth=width)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vsb.set)

        ff = ttk.Frame(frame, style="Panel.TFrame")
        ff.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 4))
        ttk.Label(ff, text="Filter by file:", background=C["panel"],
                  font=("Segoe UI", 9)).pack(side="left")
        self.file_filter_var = tk.StringVar(value="All")
        self.file_filter_cb = ttk.Combobox(ff, textvariable=self.file_filter_var,
                                           state="readonly", width=20)
        self.file_filter_cb.pack(side="left", padx=6)
        self.file_filter_cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_list())

        self.count_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.count_var, style="Sub.TLabel",
                  background=C["panel"]).grid(row=3, column=0, sticky="w",
                                              padx=10, pady=(0, 8))

    def _build_view_panel(self, parent):
        C = self.C
        frame = ttk.Frame(parent, style="Panel.TFrame")
        parent.add(frame, minsize=380, stretch="always")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        # -- viewer toolbar ---------------------------------------------------
        bar = ttk.Frame(frame, style="Panel.TFrame")
        bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))

        self.which_var = tk.StringVar(value="original")
        self.orig_btn = ttk.Button(bar, text="Original", style="Accent.TButton",
                                   command=lambda: self._show("original"))
        self.orig_btn.pack(side="left")
        self.mod_btn = ttk.Button(bar, text="Replacement", style="Soft.TButton",
                                  command=lambda: self._show("mod"))
        self.mod_btn.pack(side="left", padx=(4, 12))

        self.mode_var = tk.StringVar(value="Shaded")
        mode_cb = ttk.Combobox(bar, textvariable=self.mode_var, values=MODES,
                               state="readonly", width=13)
        mode_cb.pack(side="left")
        mode_cb.bind("<<ComboboxSelected>>",
                     lambda e: self.view.set_mode(self.mode_var.get()))

        self.grid_var = tk.BooleanVar(value=True)
        self.spin_var = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Grid", variable=self.grid_var, bg=C["panel"],
                       fg=C["text"], selectcolor=C["ebg"], activebackground=C["panel"],
                       activeforeground=C["text"], font=("Segoe UI", 9), bd=0,
                       highlightthickness=0,
                       command=self._toggle_grid).pack(side="left", padx=(10, 0))
        tk.Checkbutton(bar, text="Spin", variable=self.spin_var, bg=C["panel"],
                       fg=C["text"], selectcolor=C["ebg"], activebackground=C["panel"],
                       activeforeground=C["text"], font=("Segoe UI", 9), bd=0,
                       highlightthickness=0,
                       command=lambda: self.view.set_spin(self.spin_var.get())
                       ).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Reset View", style="Soft.TButton",
                   command=lambda: self.view.reset_view()).pack(side="right")
        ttk.Button(bar, text="Texture…", style="Soft.TButton",
                   command=self._pick_texture).pack(side="right", padx=6)
        ttk.Button(bar, text="📷", width=3, style="Soft.TButton",
                   command=self._snapshot).pack(side="right")

        # -- viewport ---------------------------------------------------------
        self.view = MeshView(frame, theme=self.C, width=560, height=380)
        self.view.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)

        # -- details ----------------------------------------------------------
        self.info_var = tk.StringVar(
            value="Select a mesh from the list.\n\n"
                  "Drag to orbit · right-drag to pan · wheel to zoom · "
                  "double-click to re-frame.")
        tk.Label(frame, textvariable=self.info_var, bg=C["panel"], fg=C["sub"],
                 font=("Segoe UI", 9), justify="left", anchor="w"
                 ).grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 2))

        opts = ttk.Frame(frame, style="Panel.TFrame")
        opts.grid(row=3, column=0, sticky="ew", padx=10)
        self.rebuild_var = tk.BooleanVar(value=False)
        self.skin_var    = tk.BooleanVar(value=True)
        tk.Checkbutton(opts, text="Transfer bone weights to skinned replacements",
                       variable=self.skin_var, bg=C["panel"], fg=C["sub"],
                       selectcolor=C["ebg"], activebackground=C["panel"],
                       activeforeground=C["text"], font=("Segoe UI", 9), bd=0,
                       highlightthickness=0).pack(side="left")
        tk.Checkbutton(opts, text="Allow file rebuild (lifts the triangle limit)",
                       variable=self.rebuild_var, bg=C["panel"], fg=C["sub"],
                       selectcolor=C["ebg"], activebackground=C["panel"],
                       activeforeground=C["text"], font=("Segoe UI", 9), bd=0,
                       highlightthickness=0,
                       command=self._explain_rebuild).pack(side="left", padx=12)

        bf = ttk.Frame(frame, style="Panel.TFrame")
        bf.grid(row=4, column=0, pady=(6, 12))
        ttk.Button(bf, text="⬇ Export Mesh", style="Soft.TButton",
                   command=self._export).pack(side="left", padx=5)
        ttk.Button(bf, text="⬇⬇ Export List", style="Soft.TButton",
                   command=self._export_all).pack(side="left", padx=5)
        ttk.Button(bf, text="⬆ Import Replacement", style="Accent.TButton",
                   command=self._import).pack(side="left", padx=5)
        ttk.Button(bf, text="✕ Remove Mod", style="Soft.TButton",
                   command=self._remove).pack(side="left", padx=5)
        ttk.Button(bf, text="▶ Apply Mesh Mods", style="Soft.TButton",
                   command=self._apply_clicked).pack(side="left", padx=5)

        tk.Label(frame, textvariable=self.local_status, bg=C["panel"], fg=C["gold"],
                 font=("Segoe UI", 9), anchor="w").grid(row=5, column=0, sticky="ew",
                                                        padx=12, pady=(0, 6))

    def _toggle_grid(self):
        self.view.show_grid = self.grid_var.get()
        self.view.render()

    def _explain_rebuild(self):
        if self.rebuild_var.get():
            messagebox.showinfo(
                "Rebuild fallback",
                "Normally a replacement is patched into the game byte-for-byte in "
                "place, which can never damage the rest of the file but limits a "
                "replacement to the original's triangle count.\n\n"
                "With rebuild allowed, a mesh that needs more triangles makes the "
                "app re-write the whole .assets file with new object offsets. "
                "Mods already in the file are carried over, and before the new "
                "file replaces the old one every other object in it is checked "
                "to be present and the same size — if anything moved, the write "
                "is abandoned and your game file is left alone.\n\n"
                "It still holds the entire file in memory while it works, which "
                "on the game's biggest .assets files is a lot, and it is far "
                "less proven than the in-place patch.\n\n"
                "Leave it off unless a mesh you want is over its budget.")

    # ── loading ──────────────────────────────────────────────────────────────
    def load(self, force=False):
        """Scan the game files for Mesh objects (once per session unless forced)."""
        if self._loading or (self._loaded_once and not force):
            return
        game_path = self.cfg.get("game_data_path", "")
        if not game_path or not os.path.isdir(game_path):
            self._status("Set your game data folder in ⚙ Settings to browse meshes.")
            return
        self._loading = True
        self.count_var.set("Scanning for meshes…")
        threading.Thread(target=self._scan_worker, args=(game_path,),
                         daemon=True).start()

    def _scan_worker(self, game_path):
        meshes, errors = [], []
        try:
            files = mm.find_mesh_files(game_path)
        except Exception as exc:
            self.after(0, lambda: self._scan_done([], [str(exc)]))
            return
        for i, path in enumerate(files):
            name = os.path.basename(path)
            self.after(0, lambda n=name, i=i, t=len(files):
                       self.count_var.set(f"Scanning {n}  ({i + 1}/{t})…"))
            try:
                meshes.extend(mm.list_meshes_in_file(path))
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        self.after(0, lambda: self._scan_done(meshes, errors))

    def _scan_done(self, meshes, errors):
        self._loading = False
        self._loaded_once = True
        self.all_meshes = sorted(meshes, key=lambda m: m["name"].lower())
        files = sorted({os.path.basename(m["assets_file"]) for m in self.all_meshes})
        self.file_filter_cb["values"] = ["All"] + files
        self._refresh_list()
        msg = f"Found {len(self.all_meshes)} meshes in {len(files)} file(s)."
        if errors:
            msg += f"  ⚠ {len(errors)} file(s) could not be read."
        self._status(msg)

    # ── list ─────────────────────────────────────────────────────────────────
    def _mod_key(self, entry):
        return mm.mesh_key(entry["assets_file"], entry["path_id"])

    def _refresh_list(self):
        query = self.search_var.get().strip().lower()
        wanted_file = self.file_filter_var.get()
        rows = []
        for m in self.all_meshes:
            if query and query not in m["name"].lower():
                continue
            if wanted_file not in ("All", "") and \
                    os.path.basename(m["assets_file"]) != wanted_file:
                continue
            rows.append(m)

        if self._sort_col:
            def key(m):
                if self._sort_col == "name":
                    return m["name"].lower()
                if self._sort_col == "mod":
                    return 0 if self._mod_key(m) in self.mesh_mods else 1
                return m.get({"verts": "vertices", "tris": "triangles"}
                             [self._sort_col], 0)
            rows.sort(key=key, reverse=not self._sort_asc)

        self.filtered_meshes = rows
        self.tree.delete(*self.tree.get_children())
        for m in rows:
            flags = ""
            if m.get("skinned"):
                flags += " 🦴"
            if m.get("compressed"):
                flags += " 🗜"
            self.tree.insert("", "end", values=(
                m["name"] + flags, f"{m['vertices']:,}", f"{m['triangles']:,}",
                "✔" if self._mod_key(m) in self.mesh_mods else ""))
        modded = sum(1 for m in rows if self._mod_key(m) in self.mesh_mods)
        self.count_var.set(f"{len(rows)} of {len(self.all_meshes)} meshes"
                           + (f" · {modded} replaced" if modded else ""))

    def _sort_by(self, col):
        self._sort_asc = not self._sort_asc if self._sort_col == col else True
        self._sort_col = col
        self._refresh_list()

    def _on_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        index = self.tree.index(sel[0])
        if index >= len(self.filtered_meshes):
            return
        entry = self.filtered_meshes[index]
        self.selected = entry
        self.orig_geo = self.mod_geo = self.write_info = None
        self._status(f"Loading {entry['name']}…")
        self.update_idletasks()
        try:
            self.orig_geo, _ = mm.load_mesh(entry["assets_file"], entry["path_id"])
        except Exception as exc:
            self.view.set_geometry(None)
            self.info_var.set(f"{entry['name']}\n\nCouldn't read this mesh: {exc}")
            self._status("")
            return
        try:
            self.write_info = mm.mesh_write_info(entry["assets_file"], entry["path_id"])
        except Exception:
            self.write_info = None

        mod = self.mesh_mods.get(self._mod_key(entry))
        if mod and os.path.exists(mod.get("source_path", "")):
            try:
                self.mod_geo = self._prepare_import(
                    mm.import_geometry(mod["source_path"]), mod)
            except Exception as exc:
                self._status(f"Replacement file could not be read: {exc}")
        self._show("mod" if self.mod_geo else "original", refit=True)
        self._status("")

    # ── viewer ───────────────────────────────────────────────────────────────
    def _show(self, which, refit=False):
        if which == "mod" and self.mod_geo is None:
            which = "original"
        self._showing = which
        geo = self.mod_geo if which == "mod" else self.orig_geo
        self.orig_btn.configure(style="Accent.TButton" if which == "original"
                                else "Soft.TButton")
        self.mod_btn.configure(style="Accent.TButton" if which == "mod"
                               else "Soft.TButton")
        self.view.set_geometry(geo, keep_camera=not refit)
        self._update_info()

    def _update_info(self):
        if self.selected is None:
            return
        entry = self.selected
        info  = self.write_info or {}
        lines = [f"{entry['name']}   ·   {os.path.basename(entry['assets_file'])}"
                 f"   ·   path_id {entry['path_id']}"]

        if self.orig_geo is not None:
            s = self.orig_geo.summary()
            size = s["size"]
            lines.append(
                f"Original: {s['vertices']:,} verts, {s['triangles']:,} tris, "
                f"{s['submeshes']} submesh(es), "
                f"{'UVs' if s['has_uvs'] else 'no UVs'}, "
                f"size {size[0]:.2f} × {size[1]:.2f} × {size[2]:.2f}")

        budget = []
        if info:
            budget.append(f"vertex data {'streamed (.resS)' if info.get('streamed') else 'inline'}")
            budget.append(f"max {info.get('max_triangles', 0):,} tris")
            if info.get("max_vertices", 0) < 2 ** 30:
                budget.append(f"max {info['max_vertices']:,} verts")
            if info.get("skinned"):
                budget.append("skinned")
            if info.get("blend_shapes"):
                budget.append("has blend shapes")
            if info.get("compressed"):
                budget.append("COMPRESSED — read-only")
            lines.append("Replacement budget: " + ", ".join(budget))

        if self.mod_geo is not None:
            s = self.mod_geo.summary()
            over = (info.get("max_triangles", 0) and
                    s["triangles"] > info["max_triangles"])
            mark = "⚠ over budget — needs the rebuild fallback" if over else "✔ fits"
            lines.append(f"Replacement: {s['vertices']:,} verts, "
                         f"{s['triangles']:,} tris  ({mark})")
            mod = self.mesh_mods.get(self._mod_key(entry), {})
            if mod.get("source_path"):
                lines.append(f"From {os.path.basename(mod['source_path'])}"
                             + (f"  · scale ×{mod['scale']}" if mod.get("scale", 1) != 1
                                else ""))
        else:
            lines.append("No replacement imported yet.")
        self.info_var.set("\n".join(lines))

    def _pick_texture(self):
        """Pick a PNG to shade the model with in 'Textured' mode -- normally the
        one you exported for this model from the Textures tab."""
        path = filedialog.askopenfilename(
            title="Preview texture", filetypes=[("Images", "*.png *.jpg *.jpeg *.tga"),
                                                ("All files", "*.*")])
        if not path:
            return
        try:
            from PIL import Image
            self.preview_texture = Image.open(path)
            self.view.set_texture(self.preview_texture)
            self.mode_var.set("Textured")
            self.view.set_mode("Textured")
            self._status(f"Preview texture: {os.path.basename(path)}")
        except Exception as exc:
            messagebox.showerror("Texture", f"Couldn't open that image:\n{exc}")

    def _snapshot(self):
        if self.view.geo is None:
            return
        path = filedialog.asksaveasfilename(
            title="Save render", defaultextension=".png",
            initialfile=f"{self.view.geo.name}.png",
            filetypes=[("PNG image", "*.png")])
        if not path:
            return
        try:
            self.view.render_image(1600, 1200).save(path)
            self._status(f"Saved render to {path}")
        except Exception as exc:
            messagebox.showerror("Snapshot", str(exc))

    # ── export ───────────────────────────────────────────────────────────────
    def _export(self):
        geo = self.view.geo
        if geo is None:
            messagebox.showinfo("Export", "Select a mesh first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export mesh", defaultextension=".obj",
            initialfile=f"{geo.name}.obj", filetypes=mm.EXPORT_FORMATS)
        if not path:
            return
        try:
            mm.export_geometry(geo, path)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self._status(f"Exported {geo.triangle_count:,} triangles to "
                     f"{os.path.basename(path)}")

    def _export_all(self):
        """Dump every mesh currently in the list to a folder.

        The practical way to work out which mesh is which: export the lot, open
        the folder in Blender, and find the model you actually want to change.
        """
        meshes = list(self.filtered_meshes)
        if not meshes:
            messagebox.showinfo("Export all", "The list is empty — nothing to export.")
            return
        folder = filedialog.askdirectory(title=f"Export {len(meshes)} meshes to…")
        if not folder:
            return
        if len(meshes) > 200 and not messagebox.askyesno(
                "Export all",
                f"That's {len(meshes)} meshes. This can take several minutes and "
                f"write a lot of files.\n\nSearch or filter the list first to "
                f"narrow it down.\n\nExport anyway?"):
            return
        threading.Thread(target=self._export_all_worker, args=(meshes, folder),
                         daemon=True).start()

    def _export_all_worker(self, meshes, folder):
        done, failed = 0, []
        for i, entry in enumerate(meshes):
            self.after(0, lambda n=entry["name"], i=i, t=len(meshes):
                       self._status(f"Exporting {n}  ({i + 1}/{t})…"))
            try:
                geo, _ = mm.load_mesh(entry["assets_file"], entry["path_id"])
                # path_id keeps two meshes that share a name from overwriting
                # each other, and ties the file back to the row in the list
                name = f"{mm._safe_name(entry['name'])}_{entry['path_id']}.obj"
                mm.export_geometry(geo, os.path.join(folder, name))
                done += 1
            except Exception as exc:
                failed.append(f"{entry['name']}: {exc}")
        self.after(0, lambda: self._status(
            f"Exported {done}/{len(meshes)} meshes to {folder}"
            + (f"  ⚠ {len(failed)} failed." if failed else "")))
        if failed:
            self.after(0, lambda: messagebox.showwarning(
                "Some meshes could not be exported", "\n".join(failed[:20])))

    # ── import ───────────────────────────────────────────────────────────────
    def _import(self):
        if self.selected is None or self.orig_geo is None:
            messagebox.showinfo("Import", "Select a mesh to replace first.")
            return
        if self.write_info and self.write_info.get("compressed"):
            messagebox.showwarning(
                "Read-only mesh",
                f"'{self.selected['name']}' is stored as a compressed mesh. It can "
                f"be viewed and exported, but not replaced.")
            return
        mods_folder = self.cfg.get("mods_folder", "")
        if not mods_folder:
            messagebox.showwarning("No mods folder",
                                   "Set your mods folder in ⚙ Settings first.")
            return

        path = filedialog.askopenfilename(title="Import replacement mesh",
                                          filetypes=mm.IMPORT_FORMATS)
        if not path:
            return
        try:
            geo = mm.import_geometry(path)
        except mm.MeshError as exc:
            messagebox.showerror("Import failed", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Import failed",
                                 f"Couldn't read {os.path.basename(path)}:\n{exc}")
            return

        options = _ImportDialog(self, geo, self.orig_geo, self.write_info,
                                self.C).result
        if options is None:
            return

        prepared = self._prepare_import(geo, options)
        try:
            stored = mm.store_replacement(mods_folder, path, self.selected["name"],
                                          self.selected["path_id"])
        except Exception as exc:
            messagebox.showerror("Import failed", f"Couldn't copy the model into "
                                                  f"your mods folder:\n{exc}")
            return

        key = self._mod_key(self.selected)
        self.mesh_mods[key] = {
            "name":        self.selected["name"],
            "assets_file": self.selected["assets_file"],
            "path_id":     self.selected["path_id"],
            "source_path": stored,
            "scale":       options["scale"],
            "recenter":    options["recenter"],
            "recompute_normals": options["recompute_normals"],
        }
        mm.save_mesh_meta(mods_folder, self.mesh_mods)
        self.mod_geo = prepared
        self._refresh_list()
        self._show("mod", refit=True)
        self._status(f"Queued replacement for {self.selected['name']} — "
                     f"press ▶ Apply to write it into the game.")

    def _prepare_import(self, geo, options):
        """Apply the per-mod import options (scale / recenter / normals)."""
        scale = float(options.get("scale", 1.0) or 1.0)
        if scale != 1.0:
            geo = geo.scaled(scale)
        if options.get("recenter") and self.orig_geo is not None:
            center, _ = self.orig_geo.center_extent()
            geo = geo.recentered(center)
        if options.get("recompute_normals"):
            geo.compute_normals()
        geo.name = (self.selected or {}).get("name", geo.name)
        return geo

    # ── remove ───────────────────────────────────────────────────────────────
    def _remove(self):
        if self.selected is None:
            return
        key = self._mod_key(self.selected)
        if key not in self.mesh_mods:
            messagebox.showinfo("Remove", "This mesh has no replacement queued.")
            return
        if not messagebox.askyesno(
                "Remove replacement",
                f"Remove the replacement for '{self.selected['name']}'?\n\n"
                f"If it was already applied, the mesh is put back to stock from "
                f"the backup. Your model file is kept in the mods folder."):
            return
        self.mesh_mods.pop(key)
        mm.save_mesh_meta(self.cfg.get("mods_folder", ""), self.mesh_mods)
        reverted = False
        try:
            reverted = mm.revert_mesh_from_backup(self.selected["assets_file"],
                                                  self.selected["path_id"])
        except Exception as exc:
            self._status(f"Removed from the queue, but the game file could not be "
                         f"reverted: {exc}")
        self.mod_geo = None
        self._refresh_list()
        self._show("original")
        if reverted:
            self._status("Replacement removed and the original mesh restored.")
        else:
            self._status("Replacement removed. (Nothing had been written to the "
                         "game yet.)")

    # ── apply ────────────────────────────────────────────────────────────────
    def _apply_clicked(self):
        """Hand off to the host so textures and meshes share one .resS pass."""
        if not self.mesh_mods:
            messagebox.showinfo("Nothing to apply",
                                "You haven't imported any mesh replacements yet.")
            return
        if self.host is not None and hasattr(self.host, "_apply_all_mods"):
            self.host._apply_all_mods()
            return
        if not messagebox.askyesno(
                "Apply mesh mods",
                f"Write {len(self.mesh_mods)} mesh replacement(s) into your game "
                f"files?\n\nThe originals are backed up first."):
            return
        applied, errors, warnings = self.apply_all()
        summary = f"Applied {applied}/{len(self.mesh_mods)} mesh mod(s)."
        if errors:
            messagebox.showwarning("Some meshes failed", "\n\n".join(errors))
            summary += f"  ⚠ {len(errors)} failed."
        if warnings:
            messagebox.showinfo("Notes", "\n\n".join(warnings))
        self._status(summary)

    def apply_all(self, ress_reset=None, ensure_backup=None):
        """Write every queued mesh mod. Returns (applied, errors, warnings).

        `ress_reset` is the shared per-run set described in the module docstring;
        `ensure_backup` is the host's backup function, called once per file
        before it is touched (a no-op fallback is used standalone).
        """
        if ress_reset is None:
            ress_reset = set()
        if ensure_backup is None:
            ensure_backup = self._ensure_backup

        applied, errors, warnings = 0, [], []
        for key, mod in list(self.mesh_mods.items()):
            assets_file = mod["assets_file"]
            source      = mod.get("source_path", "")
            if not os.path.exists(assets_file):
                errors.append(f"{mod['name']}: game file not found ({assets_file})")
                continue
            if not os.path.exists(source):
                errors.append(f"{mod['name']}: replacement file missing ({source})")
                continue
            try:
                geo = mm.import_geometry(source)
                scale = float(mod.get("scale", 1.0) or 1.0)
                if scale != 1.0:
                    geo = geo.scaled(scale)
                if mod.get("recenter"):
                    try:
                        original, _ = mm.load_mesh(assets_file, mod["path_id"])
                        center, _ = original.center_extent()
                        geo = geo.recentered(center)
                    except Exception:
                        pass
                if mod.get("recompute_normals"):
                    geo.compute_normals()
                geo.name = mod["name"]
                ensure_backup(assets_file)
                report = mm.apply_mesh_mod(
                    assets_file, mod["path_id"], geo,
                    allow_rebuild=self.rebuild_var.get(),
                    ress_reset=ress_reset,
                    transfer_skin=self.skin_var.get())
                applied += 1
                warnings += [f"{mod['name']}: {w}" for w in report["warnings"]]
            except mm.MeshError as exc:
                errors.append(f"{mod['name']}: {exc}")
            except Exception as exc:
                errors.append(f"{mod['name']}: {type(exc).__name__}: {exc}")
        return applied, errors, warnings

    @staticmethod
    def _ensure_backup(assets_path):
        """Standalone fallback for the host's ensure_backup()."""
        import shutil
        bak = assets_path + mm.BACKUP_SUFFIX
        if not os.path.exists(bak):
            shutil.copy2(assets_path, bak)
            for suffix in (".resS", ".resource"):
                companion = assets_path + suffix
                if os.path.exists(companion) and \
                        not os.path.exists(companion + mm.BACKUP_SUFFIX):
                    shutil.copy2(companion, companion + mm.BACKUP_SUFFIX)
            return True
        return False

    # ── host hooks ───────────────────────────────────────────────────────────
    def refresh_after_restore(self):
        """Called by the host after game files are rolled back to their backups."""
        self.mod_geo = None
        self._refresh_list()
        if self.selected is not None:
            self._show("original")

    def reload_config(self, cfg):
        self.cfg = cfg
        self.mesh_mods = (mm.load_mesh_meta(cfg.get("mods_folder", ""))
                          if cfg.get("mods_folder") else {})
        self.all_meshes = []
        self._loaded_once = False
        self._refresh_list()


class _ImportDialog(tk.Toplevel):
    """Confirmation + fit-up options shown between picking a file and queueing it.

    This is where a replacement's size and triangle count are checked against
    what the slot can take, so the answer arrives before an Apply run rather
    than as an error in the middle of one.
    """

    def __init__(self, parent, geo, original, info, theme):
        super().__init__(parent)
        self.result = None
        C = theme
        self.title("Import replacement mesh")
        self.configure(bg=C["bg"])
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)

        new, old = geo.summary(), original.summary()
        info = info or {}
        max_tris = info.get("max_triangles", 0)

        # A model authored in metres next to a mesh authored in Unity units is
        # the single most common import surprise, so the suggested scale is
        # offered up front rather than left to trial and error.
        old_size = max(old["size"]) or 1.0
        new_size = max(new["size"]) or 1.0
        suggested = round(old_size / new_size, 4) if new_size else 1.0

        tk.Label(self, text=os.path.basename(geo.source or geo.name), bg=C["bg"],
                 fg=C["text"], font=("Segoe UI", 12, "bold")).pack(padx=18, pady=(16, 2))

        rows = [
            ("", "Replacement", "Original"),
            ("Vertices",  f"{new['vertices']:,}",  f"{old['vertices']:,}"),
            ("Triangles", f"{new['triangles']:,}", f"{old['triangles']:,}"),
            ("Submeshes", f"{new['submeshes']}",   f"{old['submeshes']}"),
            ("UVs",       "yes" if new["has_uvs"] else "no",
                          "yes" if old["has_uvs"] else "no"),
            ("Size",      " × ".join(f"{v:.3g}" for v in new["size"]),
                          " × ".join(f"{v:.3g}" for v in old["size"])),
        ]
        table = tk.Frame(self, bg=C["bg"])
        table.pack(padx=18, pady=(6, 8))
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                tk.Label(table, text=text, bg=C["bg"],
                         fg=C["sub"] if r == 0 or c == 0 else C["text"],
                         font=("Segoe UI", 9, "bold") if r == 0 else ("Segoe UI", 9),
                         width=17 if c else 10,
                         anchor="w" if c == 0 else "e").grid(row=r, column=c,
                                                             sticky="w", padx=2)

        if max_tris and new["triangles"] > max_tris:
            verdict = (f"⚠ {new['triangles']:,} triangles is over this slot's "
                       f"in-place budget of {max_tris:,}.\nIt can only be written "
                       f"with 'Allow file rebuild' switched on.")
            color = C["gold"]
        elif info.get("max_vertices", 2 ** 30) < new["vertices"]:
            verdict = (f"⚠ {new['vertices']:,} vertices is over this slot's limit "
                       f"of {info['max_vertices']:,}.")
            color = C["gold"]
        else:
            verdict = f"✔ Fits: budget is {max_tris:,} triangles." if max_tris \
                else "✔ Ready to import."
            color = C["text"]
        tk.Label(self, text=verdict, bg=C["bg"], fg=color, font=("Segoe UI", 9),
                 justify="left", wraplength=380).pack(padx=18, pady=(0, 8))

        if info.get("skinned"):
            tk.Label(self, text="🦴 This is a skinned mesh. Bone weights are copied "
                                "from the nearest original vertex so it still "
                                "animates.", bg=C["bg"], fg=C["sub"],
                     font=("Segoe UI", 9), justify="left", wraplength=380
                     ).pack(padx=18, pady=(0, 8))

        form = tk.Frame(self, bg=C["bg"])
        form.pack(padx=18, pady=(0, 6), fill="x")
        tk.Label(form, text="Scale", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.scale_var = tk.StringVar(value="1.0")
        ttk.Entry(form, textvariable=self.scale_var, width=10).grid(row=0, column=1,
                                                                    padx=6)
        ttk.Button(form, text=f"Match original (×{suggested:g})", style="Soft.TButton",
                   command=lambda: self.scale_var.set(str(suggested))
                   ).grid(row=0, column=2, padx=4)

        self.recenter_var = tk.BooleanVar(value=False)
        self.normals_var  = tk.BooleanVar(value=not new["has_normals"])
        for text, var in (("Move to the original's centre", self.recenter_var),
                          ("Recalculate normals", self.normals_var)):
            tk.Checkbutton(self, text=text, variable=var, bg=C["bg"], fg=C["text"],
                           selectcolor=C["ebg"], activebackground=C["bg"],
                           activeforeground=C["text"], font=("Segoe UI", 9),
                           bd=0, highlightthickness=0,
                           anchor="w").pack(fill="x", padx=16)

        buttons = tk.Frame(self, bg=C["bg"])
        buttons.pack(pady=14)
        ttk.Button(buttons, text="Cancel", style="Soft.TButton",
                   command=self.destroy).pack(side="left", padx=6)
        ttk.Button(buttons, text="Queue Replacement", style="Accent.TButton",
                   command=self._ok).pack(side="left", padx=6)

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_window(self)

    def _ok(self):
        try:
            scale = float(self.scale_var.get())
            if scale <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Scale", "Scale has to be a positive number.",
                                 parent=self)
            return
        self.result = {"scale": scale, "recenter": self.recenter_var.get(),
                       "recompute_normals": self.normals_var.get()}
        self.destroy()


def open_mesh_browser(parent, cfg, theme=None):
    """Standalone window, mirroring open_save_editor()/open_gameplay_sliders()."""
    win = tk.Toplevel(parent)
    win.title("Meshes")
    win.geometry("1180x760")
    tab = MeshTab(win, cfg, theme=theme)
    tab.pack(fill="both", expand=True)
    tab.load()
    return win


if __name__ == "__main__":
    import json

    here = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(here, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {"game_data_path": "", "mods_folder": ""}
    root = tk.Tk()
    root.title("NBA Bounce Mod Manager — Meshes")
    root.geometry("1180x760")
    root.configure(bg=DEFAULT_THEME["bg"])
    tab = MeshTab(root, cfg)
    tab.pack(fill="both", expand=True)
    root.after(300, tab.load)
    root.mainloop()
