"""
roster_manager.py -- logic for the Roster / Avatar / Accessory tabs (no tkinter).

Reads and writes, in <game>/BepInEx/config/:
    BounceRoster.csv       id,name,team_id,number,role,template_id   (id <= -1000)
    BounceRosterLooks.csv  id,control,value                          (overrides vs the template only)
    BounceRatings.csv      shared with ratings_manager (custom ids get rows too)
Never touches any other game file and never calls apply_single_mod().
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import zipfile

import app_paths
import ratings_manager as rm

ROSTER_CSV = "BounceRoster.csv"
LOOKS_CSV = "BounceRosterLooks.csv"
ROSTER_HEADER = ["id", "name", "team_id", "number", "role", "template_id"]
LOOKS_HEADER = ["id", "control", "value"]
BODIES_CSV = "BounceBodies.csv"
BODIES_HEADER = ["id", "height_in", "weight_lb"]
DEFAULT_HEIGHT, DEFAULT_WEIGHT = 78.0, 215.0
HEIGHT_RANGE, WEIGHT_RANGE = (66, 96), (150, 350)
ROLES = ["starter", "bench", "legend"]
ROLE_LIMITS = {"starter": 5, "bench": 3, "legend": 3}
ROLE_TITLES = {"starter": "Starters", "bench": "Bench", "legend": "Legends"}
TARGET_SIZE = sum(ROLE_LIMITS.values())
FIRST_ID = -1001
RATING_LABELS = ["Speed", "Shooting", "Dunk", "Pass", "Steal", "Block", "Power"]
CATALOG_DIR = os.path.join(app_paths.APP_DIR, "avatar_catalog")

# Team ids are BasketTeam unique ids: Boston -1 ... ; mascot avatar ids equal team ids.
TEAMS = ["Boston Celtics", "Brooklyn Nets", "New York Knicks", "Philadelphia 76ers",
         "Toronto Raptors", "Chicago Bulls", "Cleveland Cavaliers", "Detroit Pistons",
         "Indiana Pacers", "Milwaukee Bucks", "Atlanta Hawks", "Charlotte Hornets",
         "Miami Heat", "Orlando Magic", "Washington Wizards", "Denver Nuggets",
         "Minnesota Timberwolves", "Oklahoma City Thunder", "Portland Trail Blazers",
         "Utah Jazz", "Golden State Warriors", "Los Angeles Clippers", "Los Angeles Lakers",
         "Phoenix Suns", "Sacramento Kings", "Dallas Mavericks", "Houston Rockets",
         "Memphis Grizzlies", "New Orleans Pelicans", "San Antonio Spurs"]
TEAM_BY_ID = {-(i + 1): n for i, n in enumerate(TEAMS)}
ID_BY_TEAM = {n: i for i, n in TEAM_BY_ID.items()}


# ----------------------------------------------------------------- loader
class RosterLoader(rm.Loader):
    """The ratings Loader plus the BounceRoster, BounceJersey, BouncePush and BounceStats plugins (all dlls ship in loader/).
    BouncePush ships switched off; the Gameplay Sliders tab turns it on."""
    EXTRA_DLLS = ("BounceRoster.dll", "BounceJersey.dll", "BouncePush.dll", "BounceStats.dll")

    def __init__(self, root):
        super().__init__(root)
        self.extras = [(os.path.join(rm.LOADER_DIR, n), os.path.join(self.bep, "plugins", n))
                       for n in self.EXTRA_DLLS]
        self.dll_r, self.plugin_r = self.extras[0]

    def plugin_state(self):
        states = [super().plugin_state()]
        for src, dst in self.extras:
            if not os.path.isfile(src):
                continue
            if not os.path.isfile(dst):
                states.append("missing")
                continue
            try:
                if rm._sha(dst) != rm._sha(src):
                    states.append("outdated")
            except OSError:
                pass
        for s in ("missing", "outdated"):
            if s in states:
                return s
        return "installed"

    def _copy_extras(self):
        for src, dst in self.extras:
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)

    def install(self):
        super().install()
        self._copy_extras()

    def update_plugin(self):
        super().update_plugin()
        self._copy_extras()


# ---------------------------------------------------------------- helpers
def fmt_float(v) -> str:
    s = f"{float(v):.5f}".rstrip("0").rstrip(".")
    return s or "0"


def rgba_to_hex(c) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(float(x) * 255))) for x in c[:3])


def hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _num_suffix(s: str) -> str:
    """'txt_skinBody_005' -> '005' (last run of digits)."""
    out, cur = "", ""
    for ch in s:
        if ch.isdigit():
            cur += ch
        elif cur:
            out, cur = cur, ""
    return cur or out


# ---------------------------------------------------------------- catalog
class Catalog:
    def __init__(self, folder: str = CATALOG_DIR):
        self.folder = folder
        with open(os.path.join(folder, "catalog.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.controls = {}
        for c in data["controls"]:
            self.controls.setdefault(c["name"], c)
        self.avatars = {}
        for a in data["avatars"]:
            self.avatars.setdefault(a["id"], a)
        pal = self.controls.get("hairColor", {}).get("palette", [])
        self.palette = [rgba_to_hex(c) for c in pal]

    # -- avatars
    def template_ids(self):
        """Bundled and GOAT avatars usable as a template (mascots excluded)."""
        return [i for i, a in self.avatars.items() if a["avatar_type"] != "MASCOT"]

    def template_values(self, aid: int) -> dict:
        a = self.avatars.get(aid)
        vals = {}
        for v in (a["values"] if a else []):
            x = v["value"]
            if v["type"] == 2 and isinstance(x, (list, tuple)):
                x = rgba_to_hex(x)
            vals.setdefault(v["name"], x)
        return vals

    # -- options
    def empty_value(self, name: str) -> str:
        return self.controls.get(name, {}).get("empty_asset", "")

    def options(self, name: str, vals: dict | None = None):
        """[(value, label, preview_path|None, locked, approx)] filtered by parents."""
        c = self.controls.get(name)
        if not c:
            return []
        vals = vals or {}
        res, empty = [], self.empty_value(name)
        opts = c.get("options", [])
        # GOAT-character parts (avatar_filter set) are built for the mascot/GOAT body rig and break
        # normal players in game (missing legs, stray tails), so hide them unless already selected.
        pool = [o for o in opts if not o.get("avatar_filter") or o["value"] == vals.get(name)]
        if name in ("eyesTexture", "mouthTexture") and vals.get("skinTexture"):
            f = [o for o in pool if vals["skinTexture"] in o["parents"]]
            pool = f or pool
        elif name == "hairMesh" and "hatMesh" in vals:
            f = [o for o in pool if vals["hatMesh"] in o["parents"]]
            pool = f or pool
        elif name in ("hatTexture", "gogglesTexture", "sneakersTexture"):
            mesh = self.controls.get(name.replace("Texture", "Mesh"), {})
            m = vals.get(name.replace("Texture", "Mesh"))
            for o in mesh.get("options", []):
                if m and o["value"] == m and o.get("paired_textures"):
                    f = [x for x in pool if x["value"] in o["paired_textures"]]
                    pool = f or pool
        elif name == "eyeBlink" and vals.get("skinTexture"):
            suf = "skin" + _num_suffix(vals["skinTexture"])
            f = [o for o in pool if o["value"].endswith(suf)]
            pool = f or pool
        if c.get("allow_empty") and not any(o["value"] == empty for o in pool):
            res.append((empty, "None", None, False, False))
        for o in pool:
            p = os.path.join(self.folder, o["preview"]) if o.get("preview") else None
            if o["value"] == empty and c.get("allow_empty"):
                res.append((empty, "None", p, False, False))
                continue
            res.append((o["value"], o.get("label", o["value"]), p,
                        bool(o.get("locked")), bool(o.get("approx"))))
        return res

    def preview_for(self, name: str, value: str):
        for o in self.controls.get(name, {}).get("options", []):
            if o["value"] == value and o.get("preview"):
                return os.path.join(self.folder, o["preview"])
        return None

    def float_default(self, name: str) -> float:
        return float(self.controls.get(name, {}).get("default", 0.5))


# Pairs applied automatically when the first control changes.
def pair_updates(cat: Catalog, name: str, value: str, vals: dict) -> dict:
    """Extra {control: value} to keep paired controls consistent after name=value."""
    out = {}

    def pick(target, want):
        for o in cat.controls.get(target, {}).get("options", []):
            if o["value"] == want:
                out[target] = want
                return

    if name == "skinTexture":
        n = _num_suffix(value)
        pick("skinTextureHead", f"txt_skinHead_{n}")
        pick("noseShadow", f"txt_nose_shadow_{n}")
        # eyes / mouth / blink must match the new skin
        for t in ("eyesTexture", "mouthTexture", "eyeBlink"):
            cur = vals.get(t, "")
            test = dict(vals, skinTexture=value)
            allowed = [o[0] for o in cat.options(t, test)]
            if cur not in allowed and allowed:
                # same face number on the new skin if possible
                stem = cur.rsplit("_skin", 1)[0] if "_skin" in cur else None
                alt = [a for a in allowed if stem and a.startswith(stem + "_skin")]
                out[t] = (alt or allowed)[0]
    elif name == "eyesTexture":
        stem = value.rsplit("_skin", 1)[0]
        pick("eyesIrisTexture", stem + "_iris")
        pick("eyesBrightTexture", stem + "_shine")
    elif name == "hairMesh":
        for o in cat.controls.get("hairMesh", {}).get("options", []):
            if o["value"] == value:
                pt = o.get("paired_textures") or []
                if pt:
                    out["hairTexture"] = pt[0]
                    h = pt[0] + "_head"
                    pick("hairTextureHead", h)
                elif not value:
                    out["hairTexture"] = cat.empty_value("hairTexture")
                    out["hairTextureHead"] = cat.empty_value("hairTextureHead")
    elif name == "hairTexture":
        pick("hairTextureHead", value + "_head")
    elif name in ("hatMesh", "gogglesMesh", "sneakersMesh"):
        tex = name.replace("Mesh", "Texture")
        for o in cat.controls.get(name, {}).get("options", []):
            if o["value"] == value:
                pt = o.get("paired_textures") or []
                out[tex] = pt[0] if pt else cat.empty_value(tex)
        if name == "hatMesh":
            test = dict(vals, hatMesh=value)
            allowed = [o[0] for o in cat.options("hairMesh", test)]
            if vals.get("hairMesh", "") not in allowed and allowed:
                pick_h = next((a for a in allowed if a), allowed[0])
                out["hairMesh"] = pick_h
                out.update(pair_updates(cat, "hairMesh", pick_h, vals))
    return out


def body_scale(h, w):
    """(tall, wide) in-game scale, same formula as the BounceRatings plugin."""
    h = float(h) if h and h > 0 else DEFAULT_HEIGHT
    w = float(w) if w and w > 0 else DEFAULT_WEIGHT
    hs = max(0.75, min(1.35, 1 + (h - 78) / 78))
    bmi = 703 * w / (h * h)
    ws = max(0.9, min(1.15, 1 + (bmi - 25) * 0.015))
    return hs, ws


def fmt_height(h):
    t = int(round(float(h)))
    return f"{t // 12}'{t % 12}\""


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else f"{x:.1f}"


# ----------------------------------------------------------------- player
class Player:
    def __init__(self, pid, name="", team_id=-1, number=0, role="bench",
                 template_id=-33, ratings=None, looks=None):
        self.id = int(pid)
        self.name = name
        self.team_id = int(team_id)
        self.number = int(number)
        self.role = role if role in ROLES else "bench"
        self.template_id = int(template_id)
        self.ratings = list(ratings) if ratings else [rm.DEFAULT_RATING] * 7
        self.looks = dict(looks or {})
        self.body = None              # (height_in, weight_lb) only transiently (bundle I/O)

    def to_dict(self):
        d = {"id": self.id, "name": self.name, "team_id": self.team_id, "number": self.number,
             "role": self.role, "template_id": self.template_id,
             "ratings": list(self.ratings), "looks": dict(self.looks)}
        if self.body:
            d["height_in"], d["weight_lb"] = self.body
        return d

    @classmethod
    def from_dict(cls, d):
        p = cls(d["id"], d.get("name", ""), d.get("team_id", -1), d.get("number", 0),
                d.get("role", "bench"), d.get("template_id", -33),
                [rm.clamp(x) for x in d.get("ratings", [])][:7] or None, d.get("looks"))
        try:
            if d.get("height_in") is not None and d.get("weight_lb") is not None:
                p.body = (float(d["height_in"]), float(d["weight_lb"]))
        except (TypeError, ValueError):
            pass
        return p


# ------------------------------------------------------------------ model
class RosterData:
    """Shared state for the three tabs: players, ratings table, catalog, selection."""

    def __init__(self):
        self.game_data_path = ""
        self.catalog = None
        self.catalog_error = ""
        self.players: list[Player] = []
        self.ratings = rm.Ratings()
        self.ratings_loaded = False
        self.bodies = {}              # avatar id -> (height_in, weight_lb); missing = default
        self.loaded = False
        self.dirty = False
        self.selected = None          # custom player id, or None
        self.listeners = []           # callables(kind) kind in {"select","change","load"}
        self.order_extra = []         # untouched lines (never expected)

    # -- paths
    def root(self):
        return rm.game_root(self.game_data_path)

    def cfg_dir(self):
        return os.path.join(self.root(), "BepInEx", "config") if self.root() else ""

    def path(self, name):
        return os.path.join(self.cfg_dir(), name)

    # -- events
    def notify(self, kind):
        for fn in list(self.listeners):
            try:
                fn(kind)
            except Exception:
                pass

    def touch(self):
        self.dirty = True
        self.notify("change")

    def select(self, pid):
        self.selected = pid
        self.notify("select")

    def get(self, pid):
        for p in self.players:
            if p.id == pid:
                return p
        return None

    # -- catalog
    def ensure_catalog(self):
        if self.catalog is None and not self.catalog_error:
            try:
                self.catalog = Catalog()
            except Exception as e:
                self.catalog_error = f"avatar_catalog could not be read: {e}"
        return self.catalog

    # -- looks
    def template_vals(self, p):
        return self.catalog.template_values(p.template_id) if self.ensure_catalog() else {}

    def effective(self, p):
        v = self.template_vals(p)
        v.update(p.looks)
        return v

    def set_look(self, p, control, value):
        """Store only when different from the template; return True if changed."""
        tv = self.template_vals(p).get(control)
        if isinstance(value, float) or isinstance(tv, float):
            same = tv is not None and abs(float(tv) - float(value)) < 5e-5
            value = fmt_float(value)
        else:
            same = tv == value
        if str(value).startswith("#") and tv is not None:
            same = str(tv).upper() == str(value).upper()
        old = p.looks.get(control)
        if same:
            if control in p.looks:
                del p.looks[control]
                return True
            return False
        if old == value:
            return False
        p.looks[control] = value
        return True

    def set_choice(self, p, control, value):
        """Set a string control and its paired controls; True if anything changed."""
        vals = self.effective(p)
        changed = self.set_look(p, control, value)
        vals[control] = value
        for k, v in pair_updates(self.catalog, control, value, vals).items():
            changed |= self.set_look(p, k, v)
        if changed:
            self.touch()
        return changed

    def reset_controls(self, p, names):
        n = 0
        for c in names:
            if c in p.looks:
                del p.looks[c]
                n += 1
        if n:
            self.touch()
        return n

    # -- load / save
    def load(self, game_data_path=None):
        if game_data_path is not None:
            self.game_data_path = game_data_path
        self.players, self.loaded, self.dirty = [], False, False
        self.ratings, self.ratings_loaded = rm.Ratings(), False
        self.bodies = self._load_bodies()
        self.ensure_catalog()
        # BepInEx only makes config/ on the game's first launch with it; make it ourselves.
        if self.cfg_dir() and os.path.isdir(os.path.dirname(self.cfg_dir())):
            os.makedirs(self.cfg_dir(), exist_ok=True)
        if not self.cfg_dir() or not os.path.isdir(self.cfg_dir()):
            self.selected = None
            self.notify("load")
            return "Set your game folder in Settings (BepInEx/config not found; install the loader)."
        msg = ""
        rp = self.path(rm.CSV_NAME)
        if os.path.isfile(rp):
            try:
                self.ratings.load(rp)
                self.ratings_loaded = True
            except Exception as e:
                msg = f"Could not read BounceRatings.csv: {e}"
        looks = {}
        for r in self._read_csv(self.path(LOOKS_CSV)):
            if len(r) >= 3 and r[0].strip().lstrip("-").isdigit():
                looks.setdefault(int(r[0]), {})[r[1].strip()] = r[2].strip()
        for r in self._read_csv(self.path(ROSTER_CSV)):
            if len(r) < 6 or not r[0].strip().lstrip("-").isdigit() or int(r[0]) > -1000:
                continue
            pid = int(r[0])
            self.players.append(Player(pid, r[1].strip(), _int(r[2], -1), _int(r[3], 0),
                                       r[4].strip().lower(), _int(r[5], -33),
                                       self.ratings_for(pid), looks.get(pid)))
        self.loaded = True
        if self.selected is not None and not self.get(self.selected):
            self.selected = None
        self.notify("load")
        return msg or f"Loaded {len(self.players)} custom players."

    @staticmethod
    def _read_csv(path):
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        return rows[1:]

    def _load_bodies(self):
        out = {}
        try:
            rows = self._read_csv(self.path(BODIES_CSV))
        except Exception:
            return out
        for r in rows:
            if len(r) >= 3 and r[0].strip().lstrip("-").isdigit():
                try:
                    out[int(r[0])] = (float(r[1]), float(r[2]))
                except ValueError:
                    pass
        return out

    def body_for(self, pid):
        """(height_in, weight_lb, is_default)."""
        b = self.bodies.get(int(pid))
        return (b[0], b[1], False) if b else (DEFAULT_HEIGHT, DEFAULT_WEIGHT, True)

    def set_body(self, pid, h, w):
        self.bodies[int(pid)] = (float(h), float(w))
        self.touch()

    def clear_body(self, pid):
        if self.bodies.pop(int(pid), None) is not None:
            self.touch()

    def render_bodies(self):
        ids = {p.id for p in self.players}
        rows = [[i, _num(h), _num(w)] for i, (h, w) in self.bodies.items()
                if i > -1000 or i in ids]
        return self._csv_text(BODIES_HEADER, rows)

    def ratings_for(self, pid):
        for it in self.ratings.items:
            if not isinstance(it, str) and it[0].strip() == str(pid):
                return [rm.clamp(x) for x in it[4:rm.NCOL]]
        return None

    def bundled_players(self, team_name):
        """[(row index, number, avatar id, ratings)] for a team's bundled avatars."""
        return [(i, n, a, v) for (i, t, n, a, v) in self.ratings.players() if t == team_name]

    def team_players(self, team_id):
        return [p for p in self.players if p.team_id == team_id]

    def counts(self, team_id):
        c = {r: 0 for r in ROLES}
        for p in self.team_players(team_id):
            c[p.role] = c.get(p.role, 0) + 1
        return c

    def next_id(self):
        used = {p.id for p in self.players}
        i = FIRST_ID
        while i in used:
            i -= 1
        return i

    def add_player(self, team_id, template_id, role="bench", name=None, number=None,
                   ratings=None, looks=None):
        pid = self.next_id()
        tv = self.catalog.avatars.get(template_id, {}) if self.ensure_catalog() else {}
        p = Player(pid, name or f"Player {abs(pid) - 1000}", team_id,
                   tv.get("number", 0) if number is None else number, role, template_id,
                   ratings, looks)
        self.players.append(p)
        self.touch()
        return p

    def duplicate(self, p):
        q = self.add_player(p.team_id, p.template_id, p.role, p.name + " copy", p.number,
                            p.ratings, dict(p.looks))
        if p.id in self.bodies:
            self.bodies[q.id] = self.bodies[p.id]
        return q

    def delete(self, p):
        self.players.remove(p)
        self.bodies.pop(p.id, None)
        if self.selected == p.id:
            self.selected = None
            self.notify("select")
        self.touch()

    def move(self, p, delta):
        """Reorder within the same team + role (affects picker order)."""
        grp = [x for x in self.players if x.team_id == p.team_id and x.role == p.role]
        i = grp.index(p)
        j = i + delta
        if not 0 <= j < len(grp):
            return False
        a, b = self.players.index(grp[i]), self.players.index(grp[j])
        self.players[a], self.players[b] = self.players[b], self.players[a]
        self.touch()
        return True

    # -- writing
    @staticmethod
    def _csv_text(header, rows):
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
        return buf.getvalue()

    @staticmethod
    def _atomic(path, text):
        if os.path.isfile(path):
            shutil.copy2(path, path + ".bak")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)

    def render_roster(self):
        return self._csv_text(ROSTER_HEADER, [[p.id, p.name, p.team_id, p.number, p.role,
                                               p.template_id] for p in self.players])

    def render_looks(self):
        return self._csv_text(LOOKS_HEADER, [[p.id, c, v] for p in self.players
                                             for c, v in p.looks.items()])

    def sync_ratings(self):
        """Upsert one BounceRatings row per custom player; drop rows of deleted ones."""
        keep = {str(p.id): p for p in self.players}
        items = []
        for it in self.ratings.items:
            if not isinstance(it, str):
                aid = it[0].strip()
                if aid.lstrip("-").isdigit() and int(aid) <= -1000:
                    p = keep.pop(aid, None)
                    if p is None:
                        continue
                    it = self._row(p)
            items.append(it)
        for p in keep.values():
            items.append(self._row(p))
        self.ratings.items = items

    @staticmethod
    def _row(p):
        team = TEAM_BY_ID.get(p.team_id, "").replace(",", " ")
        return [str(p.id), team, str(p.number), "BUNDLED"] + [str(rm.clamp(v)) for v in p.ratings]

    def save(self):
        """Write all three CSVs (each atomic with .bak). Returns a status message."""
        if not self.cfg_dir() or not os.path.isdir(self.cfg_dir()):
            raise OSError("BepInEx/config not found; install the loader first.")
        self._atomic(self.path(ROSTER_CSV), self.render_roster())
        self._atomic(self.path(LOOKS_CSV), self.render_looks())
        if self.bodies or os.path.isfile(self.path(BODIES_CSV)):
            self._atomic(self.path(BODIES_CSV), self.render_bodies())
        note = ""
        if self.ratings_loaded:
            self.sync_ratings()
            self.ratings.save(self.path(rm.CSV_NAME))
        else:
            note = " BounceRatings.csv not found: launch the game once, Reload, then Save again for ratings."
        self.dirty = False
        self.notify("change")
        return "Saved roster, looks, ratings and body sizes (previous files kept as .bak)." + note

    # -- bundle export / import
    def bundle(self):
        ps = []
        for p in self.players:
            d = p.to_dict()
            if p.id in self.bodies:
                d["height_in"], d["weight_lb"] = self.bodies[p.id]
            ps.append(d)
        return {"format": "bounce-roster-bundle", "version": 1, "players": ps}

    def export_bundle(self, path):
        text = json.dumps(self.bundle(), indent=1)
        if path.lower().endswith(".zip"):
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("roster.json", text)
                z.writestr(ROSTER_CSV, self.render_roster())
                z.writestr(LOOKS_CSV, self.render_looks())
                z.writestr(BODIES_CSV, self.render_bodies())
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

    def import_bundle(self, path):
        if path.lower().endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                text = z.read("roster.json").decode("utf-8-sig")
        else:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
        d = json.loads(text)
        if d.get("format") != "bounce-roster-bundle":
            raise ValueError("Not a Bounce roster bundle.")
        new = [Player.from_dict(x) for x in d["players"]]
        seen = set()
        for p in new:
            if p.id > -1000 or p.id in seen:
                p.id = None
            else:
                seen.add(p.id)
        for p in new:
            if p.id is None:
                i = FIRST_ID
                while i in seen:
                    i -= 1
                p.id = i
                seen.add(i)
        for pid in [i for i in self.bodies if i <= -1000]:
            del self.bodies[pid]
        for p in new:
            if p.body:
                self.bodies[p.id] = p.body
        self.players = new
        self.selected = None
        self.dirty = True
        self.notify("load")
        self.notify("change")
        return len(new)


def _int(s, d):
    try:
        return int(str(s).strip())
    except ValueError:
        return d


# ----------------------------------------------------------- composite preview
_img_cache = {}


def _load(path):
    from PIL import Image
    if path not in _img_cache:
        try:
            _img_cache[path] = Image.open(path).convert("RGBA")
        except Exception:
            _img_cache[path] = None
    return _img_cache[path]


def _tint(im, rgb):
    """Recolour a neutral flat-shaded layer, keeping its shading."""
    from PIL import Image, ImageChops
    gray = im.convert("L")
    px = [v for v, a in zip(gray.getdata(), im.getchannel("A").getdata()) if a > 200]
    mean = (sum(px) / len(px)) if px else 128
    k = 128.0 / max(mean, 1)
    shade = gray.point(lambda v: min(255, int(v * k)))
    solid = Image.new("RGB", im.size, rgb)
    out = ImageChops.multiply(solid, Image.merge("RGB", (shade, shade, shade)))
    out = ImageChops.screen(out, Image.new("RGB", im.size, tuple(int(c * 0.25) for c in rgb)))
    out.putalpha(im.getchannel("A"))
    return out


def skin_rgb(cat, vals):
    from PIL import Image
    for ctl, key in (("skinTextureHead", "skinTextureHead"), ("skinTexture", "skinTexture")):
        p = cat.preview_for(ctl, vals.get(key, ""))
        im = _load(p) if p else None
        if im is not None:
            small = im.resize((1, 1), Image.BOX).getpixel((0, 0))
            if min(small[:3]) < 240:
                return small[:3]
    return (200, 160, 130)


_render_lru = {}
_render_lock = __import__("threading").Lock()


def render_avatar(cat, vals, size=128, bg="#16213e", quality="high"):
    """Head portrait for `vals`.

    Uses avatar_render (a replay of how the game assembles the head: face quads
    composited into the head texture, head/nose/hair/hat/goggles meshes) when
    avatar_catalog/render_cache/ is present; otherwise falls back to the old
    layered-thumbnail approximation. Results are memoised (small LRU)."""
    try:
        import avatar_render
        if avatar_render.available():
            key = (tuple(sorted((k, str(v)) for k, v in vals.items())), size, str(bg), quality)
            with _render_lock:
                im = _render_lru.pop(key, None)
            if im is None:
                im = avatar_render.render_head(vals, size, bg=bg, quality=quality)
            with _render_lock:
                _render_lru[key] = im
                while len(_render_lru) > 96:
                    _render_lru.pop(next(iter(_render_lru)))
            return im.copy()
    except Exception:
        pass
    return render_avatar_layers(cat, vals, size, bg)


def render_avatar_layers(cat, vals, size=128, bg="#16213e"):
    """Fallback portrait: skin swatch + head mesh + hair tinted by hairColor + hat."""
    from PIL import Image, ImageDraw
    canvas = Image.new("RGBA", (128, 128), bg)
    d = ImageDraw.Draw(canvas)
    srgb = skin_rgb(cat, vals)
    d.rectangle((0, 108, 127, 127), fill=srgb + (255,))        # skin swatch strip
    layers = [("headMesh", None), ("hairMesh", "hairColor"), ("hatMesh", None),
              ("gogglesMesh", None)]
    for ctl, tint in layers:
        v = vals.get(ctl, "")
        if not v:
            continue
        p = cat.preview_for(ctl, v)
        im = _load(p) if p else None
        if im is None:
            continue
        if ctl == "headMesh":
            im = _tint(im, srgb)
        elif tint:
            im = _tint(im, hex_to_rgb(vals.get(tint, "#222222")))
        canvas.alpha_composite(im.resize((128, 128)))
    if size != 128:
        canvas = canvas.resize((size, size), Image.LANCZOS)
    return canvas
