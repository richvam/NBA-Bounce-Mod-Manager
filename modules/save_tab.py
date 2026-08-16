"""
save_tab.py -- Save Editor UI for the NBA Bounce Mod Manager.

Self-contained, exactly like slider_tab.py: imports nothing from app.py or
audio_manager.py, and never calls apply_single_mod(). All save parsing lives in
save_manager.py. Entry point mirrors open_gameplay_sliders():

    from save_tab import open_save_editor
    open_save_editor(parent, game_data_path, theme={...})

Can also be embedded as a frame:

    SaveTab(parent, game_data_path, theme=...).pack(fill="both", expand=True)

or run standalone for testing:

    python save_tab.py
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import save_manager as sm

DEFAULT_THEME = {
    "bg": "#1a1a2e", "panel": "#16213e", "accent": "#0f3460",
    "text": "#eaeaea", "muted": "#a0a0b0", "entry": "#0d1b2a",
    "gold": "#FDB927", "red": "#e94560",
}


# ---------------------------------------------------------------------------
# Contrast helpers.
#
# Colours cannot be hard-coded here: C["accent"] is the SAME navy in both the
# dark and light themes (it's a brand colour), while C["text"] and C["gold"]
# flip to near-black and dark-brown in light mode. Painting either of those on
# accent gives dark-on-dark. So every foreground is chosen at build time by
# measuring WCAG contrast against the surface it actually sits on.
# ---------------------------------------------------------------------------

def _luminance(c):
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        r, g, b = (int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return 0.0
    def lin(x):
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(fg, bg):
    a, b = sorted((_luminance(fg), _luminance(bg)))
    return (b + 0.05) / (a + 0.05)


def _best_on(bg, candidates, minimum=4.5):
    """First candidate clearing `minimum` contrast on `bg`, else the best one."""
    best, best_ratio = candidates[0], -1.0
    for c in candidates:
        r = _contrast(c, bg)
        if r >= minimum:
            return c
        if r > best_ratio:
            best, best_ratio = c, r
    return best

CATEGORIES = [
    ("All", None),
    ("Mascots", "MASCOT_"),
    ("Jerseys", "JERSEY_"),
    ("Logos", "LOGO_"),
    ("Balls", "BALL_"),
    ("Trails", "TRAIL_"),
    ("Achievements", "ACH_"),
    ("DLC", "DLC_"),
]


class SaveTab(ttk.Frame):
    def __init__(self, parent, game_data_path="", theme=None, host=None):
        super().__init__(parent, style="TFrame")
        self.C = dict(DEFAULT_THEME)
        if theme:
            self.C.update({k: v for k, v in theme.items() if v})
        self._derive_colors()
        self.host = host
        self.game_data_path = game_data_path or ""

        self.catalog = {"main": [], "goat": []}
        self.current = {"main": [], "goat": [], "seen": []}
        self.save_paths = []
        self._busy = False

        self._build()
        self.after(150, self._refresh_saves)

    def _derive_colors(self):
        """Pick every foreground by measured contrast, so both themes stay legible."""
        C = self.C
        WHITE, INK = "#FFFFFF", "#07142A"
        # accent is navy in BOTH themes -> light text either way, but measure it
        C["on_accent"] = _best_on(C["accent"], [WHITE, INK])
        # gold is #FDB927 in dark but #8A5A00 in light, which fails on navy
        C["on_accent_warn"] = _best_on(C["accent"], [C["gold"], "#FDB927", WHITE])
        # list rows sit on the entry colour, which flips light/dark, so each
        # list needs both a light-on-dark and a dark-on-light option
        C["ok"] = _best_on(C["entry"], ["#22C55E", "#15803D", "#14532D"])
        C["lock"] = _best_on(C["entry"], ["#9AA8BD", "#5A6B82", "#3F4C5F"])
        # keep the theme's muted tone when it's legible; step one notch brighter
        # before falling back to full-strength text, so hierarchy survives
        C["on_bg_muted"] = _best_on(C["bg"], [C["muted"], "#7E9BC0", C["text"]])
        C["on_panel"] = _best_on(C["panel"], [C["text"], INK, WHITE])

    # ── status helper (mirrors app.py's status bar behaviour) ─────────────────
    def _status(self, msg):
        self.status_var.set(msg)
        if self.host is not None and hasattr(self.host, "status_var"):
            try:
                self.host.status_var.set(msg)
            except Exception:
                pass

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build(self):
        C = self.C
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        # Steam Cloud warning -- the single biggest cause of "nothing happened"
        warn = tk.Frame(self, bg=C["accent"], highlightthickness=1,
                        highlightbackground=C["gold"])
        warn.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        tk.Label(warn, text="⚠  Close Steam and turn OFF Steam Cloud for NBA BOUNCE "
                            "before applying — otherwise Steam restores the old save.",
                 bg=C["accent"], fg=C["on_accent_warn"], font=("Segoe UI", 9, "bold"),
                 anchor="w", padx=10, pady=6).pack(side="left", fill="x", expand=True)
        tk.Button(warn, text="Why?", bd=0, bg=C["accent"], fg=C["on_accent"],
                  activebackground=C["accent"], activeforeground=C["on_accent_warn"],
                  highlightthickness=0, font=("Segoe UI", 9, "underline"),
                  cursor="hand2",
                  command=self._explain_cloud).pack(side="right", padx=10)

        # save picker
        top = ttk.Frame(self, style="Panel.TFrame")
        top.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Save file:", style="Sub.TLabel").grid(
            row=0, column=0, padx=(8, 6), pady=8)
        self.save_var = tk.StringVar()
        self.save_combo = ttk.Combobox(top, textvariable=self.save_var,
                                       state="readonly", values=[])
        self.save_combo.grid(row=0, column=1, sticky="ew", pady=8)
        self.save_combo.bind("<<ComboboxSelected>>", lambda e: self._load_current())
        ttk.Button(top, text="↻ Refresh", style="Soft.TButton",
                   command=self._refresh_saves).grid(row=0, column=2, padx=4)
        ttk.Button(top, text="\U0001f4c1 Browse…", style="Soft.TButton",
                   command=self._browse).grid(row=0, column=3, padx=(0, 8))

        # summary
        self.summary_var = tk.StringVar(value="Locating your save…")
        tk.Label(self, textvariable=self.summary_var, bg=C["bg"], fg=C["on_bg_muted"],
                 font=("Segoe UI", 9), anchor="w").grid(
            row=2, column=0, sticky="ew", padx=14, pady=(2, 6))

        # list
        body = ttk.Frame(self, style="Panel.TFrame")
        body.grid(row=3, column=0, sticky="nsew", padx=8)
        body.rowconfigure(1, weight=1)
        body.columnconfigure(0, weight=1)

        filt = ttk.Frame(body, style="Panel.TFrame")
        filt.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        filt.columnconfigure(2, weight=1)
        self.cat_var = tk.StringVar(value="All")
        cat = ttk.Combobox(filt, textvariable=self.cat_var, state="readonly",
                           width=14, values=[c[0] for c in CATEGORIES])
        cat.grid(row=0, column=0, padx=(0, 8))
        cat.bind("<<ComboboxSelected>>", lambda e: self._populate())
        ttk.Label(filt, text="\U0001f50d", style="Sub.TLabel").grid(row=0, column=1, padx=(0, 4))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._populate())
        ttk.Entry(filt, textvariable=self.search_var).grid(row=0, column=2, sticky="ew")

        cols = ("id", "state")
        self.tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("id", text="Unlockable ID")
        self.tree.heading("state", text="Status")
        self.tree.column("id", width=340, anchor="w")
        self.tree.column("state", width=110, anchor="center")
        self.tree.grid(row=1, column=0, sticky="nsew", padx=(8, 0), pady=(0, 8))
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        sb.grid(row=1, column=1, sticky="ns", padx=(0, 8), pady=(0, 8))
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.tag_configure("unlocked", foreground=C["ok"])
        self.tree.tag_configure("locked", foreground=C["lock"])

        # actions
        act = ttk.Frame(self, style="TFrame")
        act.grid(row=4, column=0, sticky="ew", padx=8, pady=8)
        self.btn_all = ttk.Button(act, text="⭐ Unlock Everything",
                                  style="Accent.TButton", command=self._unlock_all)
        self.btn_all.pack(side="left", padx=4)
        self.btn_sel = ttk.Button(act, text="✓ Unlock Selected",
                                  style="Soft.TButton", command=self._unlock_selected)
        self.btn_sel.pack(side="left", padx=4)
        ttk.Button(act, text="\U0001f4be Back Up Save", style="Soft.TButton",
                   command=self._backup_only).pack(side="left", padx=4)
        ttk.Button(act, text="♻ Restore Backup…", style="Soft.TButton",
                   command=self._restore).pack(side="left", padx=4)
        ttk.Button(act, text="\U0001f4e4 Export Copy…", style="Soft.TButton",
                   command=self._export).pack(side="right", padx=4)

        self.status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.status_var, bg=C["accent"], fg=C["on_accent"],
                 font=("Segoe UI", 9), anchor="w", padx=10).grid(
            row=5, column=0, sticky="ew")

    # ── data ──────────────────────────────────────────────────────────────────
    def set_game_data_path(self, path):
        """Called by app.py when the game folder changes in Settings."""
        if path == self.game_data_path:
            return
        self.game_data_path = path or ""
        self.catalog = {"main": [], "goat": []}
        self._load_current()

    def _refresh_saves(self):
        self.save_paths = sm.find_save_files()
        if not self.save_paths:
            self.save_combo["values"] = []
            self.save_var.set("")
            d = sm.find_save_dir()
            self.summary_var.set(
                f"Save folder found at {d}, but it holds no NBABounce*.sav yet — "
                f"run the game once, then hit Refresh."
                if d else
                "No save found. Expected it in "
                "%USERPROFILE%\\AppData\\LocalLow\\UnfinishedPixel\\NBA Bounce\\ "
                "— run the game once, or use Browse… if yours lives elsewhere.")
            return
        self.save_combo["values"] = [os.path.basename(p) for p in self.save_paths]
        self.save_var.set(os.path.basename(self.save_paths[0]))
        self._load_current()

    def _browse(self):
        # start in the game's own save folder; fall back to LocalLow, then home
        start = sm.find_save_dir()
        if not start:
            roots = sm.localow_roots()
            start = roots[0] if roots else os.path.expanduser("~")
        p = filedialog.askopenfilename(
            title="Select an NBA Bounce save",
            initialdir=start,
            filetypes=[("NBA Bounce save", "*.sav"), ("All files", "*.*")])
        if not p:
            return
        self.save_paths.insert(0, p)
        self.save_combo["values"] = [os.path.basename(x) for x in self.save_paths]
        self.save_var.set(os.path.basename(p))
        self._load_current()

    def _selected_path(self):
        name = self.save_var.get()
        for p in self.save_paths:
            if os.path.basename(p) == name:
                return p
        return None

    def _load_current(self):
        path = self._selected_path()
        if not path:
            return
        try:
            self.current = sm.read_unlocks(path)
        except sm.SaveError as e:
            self.current = {"main": [], "goat": [], "seen": []}
            self.summary_var.set(str(e))
            self._populate()
            return
        if not self.catalog["main"]:
            self._load_catalog_async()
        else:
            self._populate()

    def _load_catalog_async(self):
        gp = self.game_data_path
        if not gp or not os.path.isdir(gp):
            self.summary_var.set("Set your game data folder in Settings to see "
                                 "the full unlockable list.")
            self._populate()
            return
        self._status("Reading unlockable catalog from the game…")

        def work():
            try:
                cat = sm.extract_catalog(gp)
                self.after(0, lambda: self._catalog_done(cat, None))
            except Exception as e:
                self.after(0, lambda e=e: self._catalog_done(None, e))

        threading.Thread(target=work, daemon=True).start()

    def _catalog_done(self, cat, err):
        if err is not None:
            self.summary_var.set(f"Could not read the unlockable catalog: {err}")
            self._status("")
            self._populate()
            return
        self.catalog = cat
        self._status("")
        self._populate()

    def _populate(self):
        self.tree.delete(*self.tree.get_children())
        owned = set(self.current.get("main", []))
        cat = self.catalog.get("main", [])
        ids = list(dict.fromkeys(list(cat) + sorted(owned - set(cat))))

        pref = dict(CATEGORIES).get(self.cat_var.get())
        q = self.search_var.get().strip().upper()
        shown = 0
        for i in ids:
            if pref and not i.startswith(pref):
                continue
            if q and q not in i.upper():
                continue
            is_on = i in owned
            self.tree.insert("", "end", values=(i, "Unlocked" if is_on else "Locked"),
                             tags=("unlocked" if is_on else "locked",))
            shown += 1

        if cat:
            self.summary_var.set(
                f"{len(owned)} unlocked • {len(cat)} available in this game build "
                f"• {shown} shown"
                + (f"  • {len(owned - set(cat))} runtime-generated item(s) kept as-is"
                   if owned - set(cat) else ""))
        elif owned:
            self.summary_var.set(f"{len(owned)} unlocked (catalog unavailable)")

    # ── actions ───────────────────────────────────────────────────────────────
    def _explain_cloud(self):
        messagebox.showinfo("Steam Cloud and save edits", sm.STEAM_CLOUD_WARNING)

    def _guard(self):
        if self._busy:
            return None
        path = self._selected_path()
        if not path:
            messagebox.showwarning("No save selected", "Pick a save file first.")
            return None
        if not self.catalog.get("main"):
            messagebox.showwarning(
                "Catalog unavailable",
                "The unlockable list could not be read from the game, so edits "
                "are blocked. Set your game data folder in Settings.")
            return None
        return path

    def _unlock_all(self):
        path = self._guard()
        if not path:
            return
        n = len(self.catalog["main"])
        if not messagebox.askyesno(
                "Unlock everything?",
                f"Unlock all {n} items available in this game build?\n\n"
                f"Your save is backed up first, and nothing you already own is "
                f"removed.\n\n{sm.STEAM_CLOUD_WARNING.splitlines()[0]}"):
            return
        self._apply(path, None)

    def _unlock_selected(self):
        path = self._guard()
        if not path:
            return
        picks = [self.tree.item(i, "values")[0] for i in self.tree.selection()]
        if not picks:
            messagebox.showinfo("Nothing selected",
                                "Select one or more rows in the list first.")
            return
        keep = set(self.current.get("main", []))
        ids = list(dict.fromkeys(list(keep) + picks))
        self._apply(path, ids)

    def _apply(self, path, ids):
        self._busy = True
        self.btn_all.state(["disabled"]); self.btn_sel.state(["disabled"])
        self._status("Backing up and writing save…")

        def work():
            try:
                bk = sm.backup_save(path)
                info = sm.build_unlocked(path, path, self.catalog, ids=ids)
                self.after(0, lambda: self._apply_done(info, bk, None))
            except Exception as e:
                self.after(0, lambda e=e: self._apply_done(None, None, e))

        threading.Thread(target=work, daemon=True).start()

    def _apply_done(self, info, backup, err):
        self._busy = False
        self.btn_all.state(["!disabled"]); self.btn_sel.state(["!disabled"])
        if err is not None:
            self._status("")
            messagebox.showerror("Save not modified",
                                 f"{err}\n\nYour save was left untouched.")
            return
        self._status(f"✅ Unlocks {info['main_before']} → {info['main_after']}. "
                     f"Backup: {os.path.basename(backup)}")
        self._load_current()
        messagebox.showinfo(
            "Save updated",
            f"Unlockables: {info['main_before']} → {info['main_after']}\n"
            f"Validated {info['objects']:,} objects, no id collisions.\n\n"
            f"Backup saved to:\n{backup}\n\n"
            f"Start Steam and launch the game to check. If a Steam Cloud "
            f"conflict appears, choose the LOCAL copy.")

    def _backup_only(self):
        path = self._selected_path()
        if not path:
            return
        try:
            bk = sm.backup_save(path)
        except Exception as e:
            messagebox.showerror("Backup failed", str(e)); return
        self._status(f"Backup saved: {bk}")
        messagebox.showinfo("Backed up", bk)

    def _restore(self):
        path = self._selected_path()
        if not path:
            return
        p = filedialog.askopenfilename(
            title="Choose a backup to restore",
            initialdir=sm.default_backup_dir(),
            filetypes=[("NBA Bounce save", "*.sav"), ("All files", "*.*")])
        if not p:
            return
        try:
            sm.load_save(p)                       # refuse to restore junk
        except sm.SaveError as e:
            messagebox.showerror("Not a valid save", str(e)); return
        if not messagebox.askyesno("Restore backup?",
                                   f"Overwrite\n{path}\n\nwith\n{p}?"):
            return
        try:
            import shutil
            shutil.copy2(p, path)
        except Exception as e:
            messagebox.showerror("Restore failed", str(e)); return
        self._status("Backup restored.")
        self._load_current()

    def _export(self):
        path = self._selected_path()
        if not path:
            return
        dst = filedialog.asksaveasfilename(
            title="Export a copy", defaultextension=".sav",
            initialfile=os.path.basename(path),
            filetypes=[("NBA Bounce save", "*.sav")])
        if not dst:
            return
        try:
            import shutil
            shutil.copy2(path, dst)
        except Exception as e:
            messagebox.showerror("Export failed", str(e)); return
        messagebox.showinfo(
            "Exported", f"{dst}\n\nNote: a save carries the owner's avatars, "
            f"teams, seasons and display settings — it is a personal profile, "
            f"not a clean unlock-all file. There is no account binding, so it "
            f"will load for anyone, but it replaces their progress.")


def open_save_editor(parent, game_data_path, theme=None):
    """Open the save editor in its own window (mirrors open_gameplay_sliders)."""
    win = tk.Toplevel(parent)
    win.title("NBA Bounce — Save Editor")
    win.geometry("880x620")
    C = dict(DEFAULT_THEME)
    if theme:
        C.update({k: v for k, v in theme.items() if v})
    win.configure(bg=C["bg"])
    SaveTab(win, game_data_path, theme=C, host=parent).pack(fill="both", expand=True)
    win.transient(parent)
    return win


if __name__ == "__main__":
    import json
    cfg = {}
    import app_paths
    cfgp = app_paths.user("config.json")
    if os.path.exists(cfgp):
        try:
            with open(cfgp, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
    root = tk.Tk()
    root.title("Save Editor (standalone)")
    root.geometry("880x620")
    root.configure(bg=DEFAULT_THEME["bg"])
    s = ttk.Style(root); s.theme_use("clam")
    C = DEFAULT_THEME
    s.configure(".", background=C["bg"], foreground=C["text"], font=("Segoe UI", 10))
    s.configure("TFrame", background=C["bg"])
    s.configure("Panel.TFrame", background=C["panel"])
    s.configure("TLabel", background=C["bg"], foreground=C["text"])
    s.configure("Sub.TLabel", background=C["panel"], foreground=C["muted"])
    s.configure("TEntry", fieldbackground=C["entry"], foreground=C["text"])
    s.configure("Accent.TButton", background=C["red"], foreground="white",
                font=("Segoe UI", 10, "bold"), relief="flat", padding=6)
    s.configure("Soft.TButton", background=C["accent"], foreground=C["text"],
                relief="flat", padding=6)
    s.configure("Treeview", background=C["entry"], foreground=C["text"],
                fieldbackground=C["entry"], rowheight=24)
    SaveTab(root, cfg.get("game_data_path", ""), theme=C).pack(fill="both", expand=True)
    root.mainloop()
