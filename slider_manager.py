"""
slider_manager.py -- NBA Bounce gameplay sliders (2K-style) for the Mod Manager.

Self-contained. Does NOT import from app.py or audio_manager.py, and never
touches apply_single_mod(). All edits are fixed-width in-place float writes, so
file size never changes.

Why the hand-rolled deserializer: this build ships with no Unity type trees
(SerializedType.node is None for every MonoBehaviour), so UnityPy.read_typetree()
cannot read or write these fields. Layout is reconstructed from
Assembly-CSharp.dll metadata instead -- Unity serializes fields in declaration
order, including a field when it is public and not [NonSerialized], or private
with [SerializeField]. Offsets are recomputed from the shipped DLL on every run,
so a game update that reorders fields is handled automatically, and validate()
refuses to write if any object fails to decode exactly to its byte_size.

Dependencies:  pip install UnityPy dnfile attrs

Typical use:
    sm = SliderManager(game_data_path)
    sm.load()
    print(sm.validate())
    sm.apply_preset("All-Star")            # or sm.set_slider("cpu_steal_success", 80)
    print(sm.commit())                     # writes + verifies by read-back
    sm.revert()
"""

from __future__ import annotations

import json
import os
import shutil
import struct
from dataclasses import dataclass
from typing import Any

import UnityPy


def _ensure_dnfile():
    """dnfile reads the field layout out of Assembly-CSharp.dll.

    app.py's startup self-installer returns early once UnityPy and Pillow
    import, so on an install that predates this feature dnfile will be missing.
    Fetch it on demand rather than making the user do it by hand. This runs on
    a worker thread (see SliderTab._load_async), so it never blocks the UI.
    """
    try:
        import dnfile
        return dnfile
    except ImportError:
        pass
    import subprocess
    import sys
    subprocess.run([sys.executable, "-m", "pip", "install", "dnfile", "--upgrade"],
                   capture_output=True, text=True)
    try:
        import dnfile
        return dnfile
    except ImportError as exc:
        raise ImportError(
            "Gameplay Sliders needs the 'dnfile' package, and automatic "
            "installation failed.\n\nInstall it manually, then reopen this "
            "window:\n\n    py -m pip install dnfile\n\n(original error: %s)" % exc
        ) from exc


dnfile = _ensure_dnfile()

UNITY_FALLBACK_VERSION = "6000.0.37f1"
BACKUP_SUFFIX = ".sliders_backup"

PRIM = {
    "float": ("f", 4), "double": ("d", 8), "int": ("i", 4), "uint": ("I", 4),
    "long": ("q", 8), "ulong": ("Q", 8), "short": ("h", 2), "ushort": ("H", 2),
    "byte": ("B", 1), "sbyte": ("b", 1), "bool": ("?", 1), "char": ("H", 2),
}
FIXED = {
    "UnityEngine.Vector2": ("ff", 8), "UnityEngine.Vector3": ("fff", 12),
    "UnityEngine.Vector4": ("ffff", 16), "UnityEngine.Quaternion": ("ffff", 16),
    "UnityEngine.Color": ("ffff", 16), "UnityEngine.Rect": ("ffff", 16),
    "UnityEngine.Vector2Int": ("ii", 8), "UnityEngine.Vector3Int": ("iii", 12),
}
ET = {0x01: "void", 0x02: "bool", 0x03: "char", 0x04: "sbyte", 0x05: "byte",
      0x06: "short", 0x07: "ushort", 0x08: "int", 0x09: "uint", 0x0A: "long",
      0x0B: "ulong", 0x0C: "float", 0x0D: "double", 0x0E: "string",
      0x18: "IntPtr", 0x19: "UIntPtr", 0x1C: "object"}

# Files and classes we care about
TARGETS = {
    "level2": ("AIDifficulty", "AIDifficultyManager", "PlayerStats"),
    "sharedassets2.assets": ("AIDifficultyProbabilityBars", "ShootBarSystem"),
}
BASE_SHOOT_BAR_NAME = "BaseBounceShootingBar"


# --------------------------------------------------------------------------
# .NET metadata
# --------------------------------------------------------------------------
class Assembly:
    def __init__(self, dll_path: str):
        self.pe = dnfile.dnPE(dll_path)
        self.T = self.pe.net.mdtables
        self.NT = len(self.T.TypeDef.rows)
        self._names: dict[int, str] = {}
        self._sf: dict[int, list] = {}
        self._short: dict[str, int] = {}
        for ti in range(1, self.NT + 1):
            self._short.setdefault(self.tdname(ti).split(".")[-1], ti)
        self._fattr = self._field_attrs()

    @staticmethod
    def _s(h):
        return h.value if h is not None else ""

    def tdname(self, i: int) -> str:
        if i not in self._names:
            r = self.T.TypeDef.rows[i - 1]
            ns = self._s(r.TypeNamespace)
            self._names[i] = (ns + "." if ns else "") + self._s(r.TypeName)
        return self._names[i]

    def trname(self, i: int) -> str:
        r = self.T.TypeRef.rows[i - 1]
        ns = self._s(r.TypeNamespace)
        return (ns + "." if ns else "") + self._s(r.TypeName)

    def resolve(self, name: str):
        for ti in range(1, self.NT + 1):
            if self.tdname(ti) == name:
                return ti
        return self._short.get(name)

    @staticmethod
    def _uncomp(b, o):
        x = b[o]
        if x < 0x80:
            return x, o + 1
        if x < 0xC0:
            return ((x & 0x3F) << 8) | b[o + 1], o + 2
        return ((x & 0x1F) << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3], o + 4

    def _cudec(self, b, o):
        v, o = self._uncomp(b, o)
        tag, idx = v & 3, v >> 2
        if tag == 0:
            return self.tdname(idx), o
        if tag == 1:
            return self.trname(idx), o
        return "TypeSpec%d" % idx, o

    def _sig(self, b, o):
        e = b[o]; o += 1
        if e in ET:
            return ET[e], o
        if e in (0x11, 0x12):
            return self._cudec(b, o)
        if e == 0x1D:
            n, o = self._sig(b, o); return n + "[]", o
        if e == 0x14:
            n, o = self._sig(b, o); return n + "[,]", o
        if e in (0x0F, 0x10):
            n, o = self._sig(b, o); return n + ("*" if e == 0x0F else "&"), o
        if e == 0x15:
            g, o = self._sig(b, o)
            c, o = self._uncomp(b, o)
            args = []
            for _ in range(c):
                a, o = self._sig(b, o); args.append(a)
            return "%s<%s>" % (g, ",".join(args)), o
        if e in (0x13, 0x1E):
            n, o = self._uncomp(b, o)
            return ("!" if e == 0x13 else "!!") + str(n), o
        if e == 0x45:
            return self._sig(b, o)
        return "et0x%x" % e, o

    def fieldtype(self, row) -> str:
        try:
            return self._sig(bytes(row.Signature.value), 1)[0]
        except Exception:
            return "?"

    def basename(self, ti: int) -> str:
        ci = self.T.TypeDef.rows[ti - 1].struct.Extends_CodedIndex
        tag, idx = ci & 3, ci >> 2
        if idx == 0:
            return ""
        if tag == 0:
            return self.tdname(idx)
        if tag == 1:
            return self.trname(idx)
        try:
            return self._sig(bytes(self.T.TypeSpec.rows[idx - 1].Signature.value), 0)[0]
        except Exception:
            return "TypeSpec"

    def frange(self, ti: int):
        a = self.T.TypeDef.rows[ti - 1].struct.FieldList_Index
        b = (self.T.TypeDef.rows[ti].struct.FieldList_Index
             if ti < self.NT else len(self.T.Field.rows) + 1)
        return a, b

    def _field_attrs(self):
        out = {}
        for r in self.T.CustomAttribute.rows:
            try:
                p = r.struct.Parent_CodedIndex
                if (p & 0x1F) != 1:
                    continue
                t = r.struct.Type_CodedIndex
                if (t & 7) != 3:
                    continue
                ci = self.T.MemberRef.rows[(t >> 3) - 1].struct.Class_CodedIndex
                ctag, cidx = ci & 7, ci >> 3
                nm = (self.tdname(cidx) if ctag == 0
                      else self.trname(cidx) if ctag == 1 else "?")
                out.setdefault(p >> 5, []).append(nm)
            except Exception:
                continue
        return out

    def sfields(self, ti: int):
        """[(name, type, serialized)] in declaration order."""
        if ti in self._sf:
            return self._sf[ti]
        a, b = self.frange(ti)
        out = []
        for i in range(a, b):
            r = self.T.Field.rows[i - 1]
            fl = r.Flags
            if getattr(fl, "fdStatic", False) or getattr(fl, "fdLiteral", False):
                continue
            attrs = self._fattr.get(i, [])
            ser = (not getattr(fl, "fdNotSerialized", False)) and (
                getattr(fl, "fdPublic", False)
                or any("SerializeField" in x for x in attrs))
            out.append((self._s(r.Name), self.fieldtype(r), ser))
        self._sf[ti] = out
        return out

    def is_unity_object(self, tn: str) -> bool:
        ti = self.resolve(tn)
        for _ in range(20):
            if ti is None:
                return False
            b = self.basename(ti)
            if b.startswith("UnityEngine."):
                return True
            nxt = self.resolve(b)
            if nxt is None or nxt == ti:
                return False
            ti = nxt
        return False

    def is_enum(self, tn: str) -> bool:
        ti = self.resolve(tn)
        return ti is not None and self.basename(ti) == "System.Enum"


# --------------------------------------------------------------------------
# Binary walker with nested path tracking
# --------------------------------------------------------------------------
@dataclass
class Slot:
    path: str
    type: str
    rel: int
    size: int
    value: Any


class _Cur:
    __slots__ = ("b", "o", "map")

    def __init__(self, b):
        self.b, self.o, self.map = b, 0, {}

    def take(self, fmt, n):
        v = struct.unpack_from("<" + fmt, self.b, self.o); self.o += n; return v

    def i32(self):
        return self.take("i", 4)[0]

    def align(self):
        self.o = (self.o + 3) & ~3


class Walker:
    def __init__(self, asm: Assembly):
        self.asm = asm

    @staticmethod
    def _args(s):
        out, d, cur = [], 0, ""
        for ch in s:
            if ch == "<":
                d += 1
            elif ch == ">":
                d -= 1
            elif ch == "," and d == 0:
                out.append(cur); cur = ""; continue
            cur += ch
        if cur:
            out.append(cur)
        return out

    def read(self, c, tn, path, gargs=None, depth=0):
        if gargs and tn.startswith("!") and tn[1:].isdigit():
            tn = gargs[int(tn[1:])]
        if tn in PRIM:
            f, n = PRIM[tn]; off = c.o; v = c.take(f, n)[0]
            if n < 4:
                c.align()
            c.map[path] = Slot(path, tn, off, n, v); return v
        if tn in FIXED:
            f, n = FIXED[tn]; off = c.o; v = list(c.take(f, n))
            c.map[path] = Slot(path, tn, off, n, v); return v
        if tn == "string":
            n = c.i32(); s = c.b[c.o:c.o + n].decode("utf8", "replace")
            c.o += n; c.align(); return s
        LIST = "System.Collections.Generic.List`1<"
        if tn.endswith("[]") or tn.startswith(LIST):
            inner = tn[:-2] if tn.endswith("[]") else tn[len(LIST):-1]
            cnt = c.i32()
            return [self.read(c, inner, "%s[%d]" % (path, i), None, depth + 1)
                    for i in range(cnt)]
        if tn.startswith("System.Nullable`1"):
            raise ValueError("Nullable is not Unity-serialized: " + tn)
        if self.asm.is_unity_object(tn):
            fid = c.i32(); pid = c.take("q", 8)[0]
            return {"PPtr": [fid, pid]}
        if self.asm.is_enum(tn):
            off = c.o; v = c.i32()
            c.map[path] = Slot(path, "enum", off, 4, v); return v
        if depth > 8:
            raise ValueError("nesting too deep at " + tn)
        if "`" in tn and tn.endswith(">"):
            base = tn[:tn.index("<")]
            ga = self._args(tn[tn.index("<") + 1:-1])
            ti = self.asm.resolve(base)
            if ti is None:
                raise ValueError("unknown generic " + tn)
            return self.obj(c, ti, path, ga, depth + 1)
        ti = self.asm.resolve(tn)
        if ti is None:
            raise ValueError("unknown type " + tn)
        return self.obj(c, ti, path, None, depth + 1)

    def obj(self, c, ti, path, gargs=None, depth=0):
        out = {}
        for n, t, ser in self.asm.sfields(ti):
            if not ser:
                continue
            out[n] = self.read(c, t, "%s.%s" % (path, n) if path else n, gargs, depth)
        return out

    def monobehaviour(self, raw: bytes, cls: str):
        c = _Cur(raw)
        c.take("i", 4); c.take("q", 8)          # m_GameObject
        c.take("?", 1); c.align()               # m_Enabled
        c.take("i", 4); c.take("q", 8)          # m_Script
        n = c.i32(); name = c.b[c.o:c.o + n].decode("utf8", "replace")
        c.o += n; c.align()                     # m_Name
        ti = self.asm.resolve(cls)
        if ti is None:
            raise ValueError("class not in assembly: " + cls)
        data = self.obj(c, ti, "", None, 0)
        return data, name, c.map, c.o


@dataclass
class GameObj:
    file: str
    cls: str
    name: str
    path_id: int
    byte_start: int
    byte_size: int
    consumed: int
    data: dict
    slots: dict

    @property
    def ok(self):
        return self.consumed == self.byte_size


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------
class SliderManager:
    def __init__(self, game_data_path: str, catalog_path: str | None = None,
                 presets_path: str | None = None):
        UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_FALLBACK_VERSION
        self.root = game_data_path
        dll = os.path.join(self.root, "Managed", "Assembly-CSharp.dll")
        if not os.path.isfile(dll):
            raise FileNotFoundError(dll)
        self.asm = Assembly(dll)
        self.walker = Walker(self.asm)
        here = os.path.dirname(os.path.abspath(__file__))
        self.catalog = json.load(open(catalog_path or os.path.join(here, "sliders_catalog.json"), encoding="utf-8"))
        self.presets = json.load(open(presets_path or os.path.join(here, "presets.json"), encoding="utf-8"))
        self.objs: dict[str, list[GameObj]] = {}
        self.roles: dict[str, GameObj] = {}
        self.values: dict[str, float] = {}      # slider_id -> ui 0..100
        self._loaded = False

    # ---------- loading ----------
    def _read_file(self, fname, use_backup=False):
        path = os.path.join(self.root, fname)
        if use_backup and os.path.exists(path + BACKUP_SUFFIX):
            path = path + BACKUP_SUFFIX
        env = UnityPy.load(path)
        want = set(TARGETS[fname])
        out = []
        for o in env.objects:
            if o.type.name != "MonoBehaviour":
                continue
            try:
                cls = o.read(check_read=False).m_Script.read().m_ClassName
            except Exception:
                continue
            if cls not in want:
                continue
            raw = o.get_raw_data()
            try:
                data, name, slots, consumed = self.walker.monobehaviour(raw, cls)
            except Exception:
                data, name, slots, consumed = {}, "", {}, -1
            out.append(GameObj(fname, cls, name, o.path_id, o.byte_start,
                               o.byte_size, consumed, data, slots))
        return out

    def load(self, from_backup=False):
        for f in TARGETS:
            self.objs[f] = self._read_file(f, use_backup=from_backup)
        self._map_roles()
        self._loaded = True
        return self

    def _map_roles(self):
        """Resolve semantic roles -> objects, never hardcoding path_ids."""
        self.roles = {}
        mgr = [o for o in self.objs["level2"] if o.cls == "AIDifficultyManager"]
        if not mgr:
            raise RuntimeError("AIDifficultyManager not found in level2")
        m = mgr[0].data
        tiers = [p["PPtr"][1] for p in m["m_aoDifficultyTiers"]]
        human = m["m_oHumanDifficulty"]["PPtr"][1]
        scratch = m["m_oCurrentAIDifficulty"]["PPtr"][1]
        by_id = {o.path_id: o for o in self.objs["level2"]}
        for i, pid in enumerate(tiers):
            self.roles["tier%d" % i] = by_id[pid]
        self.roles["human"] = by_id[human]
        self._scratch_id = scratch
        self._scratch_bars = by_id[scratch].data.get(
            "m_oAIDifficultyProbabilityBars", {}).get("PPtr", [0, 0])[1]
        # each tier's probability-bars asset
        sa2 = {o.path_id: o for o in self.objs["sharedassets2.assets"]}
        for i, pid in enumerate(tiers):
            b = by_id[pid].data.get("m_oAIDifficultyProbabilityBars", {}).get("PPtr", [0, 0])[1]
            if b in sa2:
                self.roles["bars%d" % i] = sa2[b]
        for o in self.objs["sharedassets2.assets"]:
            if o.cls == "ShootBarSystem" and o.name == BASE_SHOOT_BAR_NAME:
                self.roles["shootbar"] = o
        self.roles["playerstats"] = [o for o in self.objs["level2"] if o.cls == "PlayerStats"]

    @property
    def tier_count(self):
        return sum(1 for k in self.roles if k.startswith("tier"))

    # ---------- validation ----------
    def validate(self):
        rep = {"objects": 0, "decoded_ok": 0, "bad": [], "roles": {}}
        for f, lst in self.objs.items():
            for o in lst:
                rep["objects"] += 1
                if o.ok:
                    rep["decoded_ok"] += 1
                else:
                    rep["bad"].append({"file": f, "class": o.cls,
                                       "path_id": o.path_id,
                                       "consumed": o.consumed, "size": o.byte_size})
        for k, v in self.roles.items():
            if isinstance(v, list):
                rep["roles"][k] = "%d objects" % len(v)
            else:
                rep["roles"][k] = "%s #%d %s" % (v.cls, v.path_id,
                                                 v.data.get("m_sLevelIdentifier", v.name))
        rep["ok"] = not rep["bad"]
        return rep

    # ---------- slider values ----------
    def set_slider(self, sid: str, ui_value: float):
        if sid not in self.catalog["sliders"]:
            raise KeyError("unknown slider: " + sid)
        self.values[sid] = max(0.0, min(100.0, float(ui_value)))

    def apply_preset(self, name: str):
        p = self.presets["presets"].get(name)
        if p is None:
            raise KeyError("unknown preset: %s (have %s)"
                           % (name, ", ".join(self.presets["presets"])))
        self.values = dict(p["sliders"])
        return self

    def preset_names(self):
        return list(self.presets["presets"])

    # ---------- resolving edits ----------
    @staticmethod
    def _lerp(a, b, t):
        if isinstance(a, list):
            return [a[i] + (b[i] - a[i]) * t for i in range(len(a))]
        return a + (b - a) * t

    @classmethod
    def _curve(cls, anchors, ui):
        """Piecewise-linear through anchor points {ui_string: game_value},
        with linear extrapolation beyond the outermost anchors. Lets a preset
        land exactly on a shipped difficulty tier instead of near it."""
        pts = sorted((float(k), v) for k, v in anchors.items())
        if ui <= pts[0][0]:
            (x0, v0), (x1, v1) = pts[0], pts[1]
        elif ui >= pts[-1][0]:
            (x0, v0), (x1, v1) = pts[-2], pts[-1]
        else:
            for i in range(len(pts) - 1):
                if pts[i][0] <= ui <= pts[i + 1][0]:
                    (x0, v0), (x1, v1) = pts[i], pts[i + 1]
                    break
        t = (ui - x0) / (x1 - x0) if x1 != x0 else 0.0
        return cls._lerp(v0, v1, t)

    @staticmethod
    def _clamp(v, rng):
        """Clamp into a legal range. Vector2 fields are [min,max] random ranges,
        so also keep the pair ordered after clamping."""
        if rng is None:
            return v
        lo, hi = rng
        if isinstance(v, list):
            out = [min(hi, max(lo, x)) for x in v]
            if len(out) == 2 and out[0] > out[1]:
                out = [out[1], out[0]]
            return out
        return min(hi, max(lo, v))

    def _targets_for(self, spec):
        """role string -> list of GameObj"""
        role = spec["role"]
        if role == "tiers":
            return [self.roles["tier%d" % i] for i in range(self.tier_count)]
        if role == "bars":
            return [self.roles["bars%d" % i] for i in range(self.tier_count)
                    if "bars%d" % i in self.roles]
        if role == "playerstats":
            return self.roles["playerstats"]
        return [self.roles[role]]

    def planned_edits(self):
        """[(GameObj, slot_path, new_value)] for the current slider values."""
        edits = []
        for sid, ui in self.values.items():
            s = self.catalog["sliders"][sid]
            t = ui / 100.0
            for spec in s["fields"]:
                # Convention (2K): higher UI = MORE of that thing for that side.
                if "anchors" in spec:
                    # Piecewise curve through the game's own shipped tiers:
                    # UI 25 = Beginner, UI 50 = Advanced, UI 75 = Legend,
                    # linearly extrapolated outside that range.
                    val = self._curve(spec["anchors"], ui)
                else:
                    val = self._lerp(spec["at0"], spec["at100"], t)
                val = self._clamp(val, spec.get("clamp"))
                for obj in self._targets_for(spec):
                    if spec["path"] not in obj.slots:
                        continue
                    edits.append((obj, spec["path"], val))
        edits += self._shot_window_edits()
        return edits

    def _shot_window_edits(self):
        """Regenerate BaseBounceShootingBar zone geometry from the window sliders.

        Zone order is [5%, 10%, 30%, 100%, 30%, 10%, 5%]. Each zone is scaled to
        a FRACTION of its shipped width rather than an absolute target. That
        matters for two reasons:
          * it scales correctly per distance band -- the 9 m green window is only
            0.05 wide in stock, so an absolute target of 0.08 would never bite,
            whereas a 0.3x fraction tightens every band proportionally;
          * zones that are zero-width in stock stay zero-width (0 * f == 0), so
            deep shots never gain a 100% window the shipped game never gave them.
        With fractions <= 1.0 no band can become easier than the shipped game.
        """
        cfg = self.catalog.get("shot_window")
        if not cfg:
            return []
        bar = self.roles.get("shootbar")
        if bar is None:
            return []
        stock = self._stock_shootbar()
        f100 = self._win(cfg["slider_100"], cfg["frac100_at0"], cfg["frac100_at100"])
        f30 = self._win(cfg["slider_30"], cfg["frac30_at0"], cfg["frac30_at100"])
        f10 = self._win(cfg["slider_10"], cfg["frac10_at0"], cfg["frac10_at100"])
        edits = []
        for bi, sb in enumerate(stock["m_aoChargingBars"]):
            ch = sb["m_aoChunks"]
            sw = [c["m_fPercentageEnd"] - c["m_fPercentageStart"] for c in ch]
            e3 = ch[3]["m_fPercentageEnd"]
            n3, n2, n4 = sw[3] * f100, sw[2] * f30, sw[4] * f30
            n1, n5 = sw[1] * f10, sw[5] * f10
            z3 = (e3 - n3, e3); z2 = (z3[0] - n2, z3[0]); z1 = (z2[0] - n1, z2[0])
            z0 = (0.0, z1[0]); z4 = (e3, e3 + n4)
            z5 = (z4[1], z4[1] + n5); z6 = (z5[1], 1.0)
            prev = 0.0
            for ci, (a, b) in enumerate((z0, z1, z2, z3, z4, z5, z6)):
                a = min(1.0, max(0.0, a)); b = min(1.0, max(0.0, b))
                a = max(a, prev); b = max(b, a); prev = b
                base = "m_aoChargingBars[%d].m_aoChunks[%d]" % (bi, ci)
                edits.append((bar, base + ".m_fPercentageStart", a))
                edits.append((bar, base + ".m_fPercentageEnd", b))
        return edits

    def _win(self, sid, easy, hard):
        ui = self.values.get(sid, 50.0)
        return easy + (hard - easy) * (ui / 100.0)

    def _stock_shootbar(self):
        if not hasattr(self, "_stock_sb"):
            objs = self._read_file("sharedassets2.assets", use_backup=True)
            self._stock_sb = next(o.data for o in objs
                                  if o.cls == "ShootBarSystem" and o.name == BASE_SHOOT_BAR_NAME)
        return self._stock_sb

    # ---------- writing ----------
    def commit(self, dry_run=False):
        if not self._loaded:
            raise RuntimeError("call load() first")
        edits = self.planned_edits()
        by_file = {}
        for obj, path, val in edits:
            if obj.path_id == getattr(self, "_scratch_id", None) or \
               obj.path_id == getattr(self, "_scratch_bars", None):
                continue                     # rewritten every match; pointless
            by_file.setdefault(obj.file, []).append((obj, path, val))

        rep = {"written": 0, "verified": 0, "failed": [], "files": [], "dry_run": dry_run}
        for fname, items in by_file.items():
            path = os.path.join(self.root, fname)
            bak = path + BACKUP_SUFFIX
            if not os.path.exists(bak) and not dry_run:
                shutil.copy2(path, bak)
            size0 = os.path.getsize(path)
            writes = []
            for obj, spath, val in items:
                slot = obj.slots.get(spath)
                if slot is None:
                    continue
                if slot.type == "float":
                    blob = struct.pack("<f", float(val))
                elif slot.type == "UnityEngine.Vector2":
                    blob = struct.pack("<ff", float(val[0]), float(val[1]))
                else:
                    continue
                if len(blob) != slot.size:
                    raise RuntimeError("width mismatch on " + spath)
                writes.append((obj.byte_start + slot.rel, blob))
            if dry_run:
                rep["files"].append({"file": fname, "edits": len(writes), "dry_run": True})
                continue
            with open(path, "r+b") as fh:
                for off, blob in writes:
                    fh.seek(off); fh.write(blob); rep["written"] += 1
            with open(path, "rb") as fh:
                for off, blob in writes:
                    fh.seek(off)
                    if fh.read(len(blob)) == blob:
                        rep["verified"] += 1
                    else:
                        rep["failed"].append({"file": fname, "offset": off})
            rep["files"].append({"file": fname, "edits": len(writes),
                                 "size_unchanged": os.path.getsize(path) == size0})
        if not dry_run:
            self.load()
        return rep

    def revert(self):
        done = []
        for fname in TARGETS:
            path = os.path.join(self.root, fname)
            bak = path + BACKUP_SUFFIX
            if os.path.exists(bak):
                shutil.copy2(bak, path); done.append(fname)
        if done:
            self.load()
        return done

    def has_backup(self):
        return all(os.path.exists(os.path.join(self.root, f) + BACKUP_SUFFIX)
                   for f in TARGETS)

    # ---------- share / persist ----------
    def export_set(self, path, label="custom"):
        json.dump({"label": label, "unity": UNITY_FALLBACK_VERSION,
                   "sliders": self.values},
                  open(path, "w", encoding="utf-8"), indent=1)
        return path

    def import_set(self, path):
        d = json.load(open(path, encoding="utf-8"))
        self.values = dict(d.get("sliders", {}))
        return d.get("label", "custom")
