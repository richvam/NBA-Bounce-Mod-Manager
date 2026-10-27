"""Build avatar_catalog/ (catalog.json + previews/) from a NBA Bounce install.

Usage:
    python tools/build_avatar_catalog.py <game install or *_Data folder> [out_dir]
        [--no-previews] [--size 128]

READ-ONLY on the game folder.  Requires UnityPy (>=1.25), Pillow, numpy.

WHERE THE DATA COMES FROM
  * Option lists   : the AvatarResources MonoBehaviour (level0, also copied in
                     level1/level2).  m_aAssetResources = one AvatarResource per
                     control (m_sAvatarControlName); each holds m_aoResources
                     (PPtrs into sharedassets0.assets).  The string the game
                     stores in AvatarData is the referenced object's m_Name.
                     Controls with m_bParent (eyesTexture, mouthTexture,
                     hairMesh) keep per-parent lists in m_dependantResources.
  * Lock flags     : AvatarResourceItem.m_sUnlockable (DLC_*, ACH_*, GOAT_*,
                     RANDOM_*), m_bSpecial and m_aiAvatarFilter.
  * Colour palette : AvatarResources.m_aoPickerColors.
  * Float ranges   : AvatarRandomizer (randomiser min/max, NOT editor limits;
                     editor sliders are 0..1).  Parsed from raw bytes.
  * Bundled avatars: AvatarManager.m_aoBundledAvatarContainers.
"""
import json, os, re, struct, sys, argparse, collections

import numpy as np
from PIL import Image
import UnityPy
from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator

UNITY_VERSION = "6000.0.37f1"
MESH_CONTROLS = {"hairMesh", "headMesh", "noseMesh", "gogglesMesh", "hatMesh", "sneakersMesh"}
# mesh control -> texture control paired by option index
PAIRS = {"hatMesh": "hatTexture", "gogglesMesh": "gogglesTexture", "sneakersMesh": "sneakersTexture"}
# (yaw, pitch) degrees per mesh control
VIEWS = {"hairMesh": (25, -12), "headMesh": (25, -8), "noseMesh": (25, -8), "gogglesMesh": (25, -8),
         "hatMesh": (25, -14), "sneakersMesh": (305, -16)}
FLAT_COLORS = {"hairMesh": (70, 48, 36), "headMesh": (198, 146, 108), "noseMesh": (198, 146, 108)}
CENTER_CROP = {"skinTexture": .6, "skinTextureHead": .6, "eyesTexture": .5, "eyesBrightTexture": .5,
               "eyesIrisTexture": .5, "eyeBlink": .5, "mouthTexture": .5, "eyebrowsTexture": .6,
               "noseShadow": .6}
FLOAT_DESC = {
    "eyesScale": "Eye size", "eyesSeparate": "Distance between the eyes", "eyesMoveVertical": "Eye height on face",
    "eyebrowsScale": "Eyebrow size", "eyebrowsSeparate": "Distance between eyebrows",
    "eyebrowsMoveVertical": "Eyebrow height", "eyebrowsRotate": "Eyebrow tilt",
    "mouthScale": "Mouth size", "mouthMoveVertical": "Mouth height", "mouthRotate": "Mouth tilt",
    "noseScale": "Nose size", "noseMoveVertical": "Nose height", "headSizeX": "Head width",
    "headSizeY": "Head height", "handsSize": "Hand size", "weakStrong": "Body build (weak..strong)",
    "slimFat": "Body build (slim..fat)"}
COLOR_DESC = {"hairColor": "Hair tint", "eyebrowsColor": "Eyebrow tint", "eyesColor": "Eye colour"}


def log(*a):
    print(*a, flush=True)


def safe(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name) or "_"


def label_of(v):
    if not v:
        return "None"
    s = re.sub(r"^(mesh_|txt_)", "", v).replace("_", " ").strip()
    return s


def lock_info(unl):
    if not unl:
        return None
    k = ("dlc" if unl.startswith("DLC_") else "achievement" if unl.startswith("ACH_")
         else "goat" if unl.startswith("GOAT_") else "random_only" if unl.startswith("RANDOM_") else "other")
    return k


# ───────────────────────── asset helpers ─────────────────────────
class Game:
    def __init__(self, path):
        data = path
        if not os.path.isdir(os.path.join(data, "globalgamemanagers.assets" if False else ".")) or True:
            if not os.path.exists(os.path.join(data, "globalgamemanagers")):
                cands = [d for d in os.listdir(data) if d.endswith("_Data")]
                if cands:
                    data = os.path.join(data, cands[0])
        self.data = data
        self.root = os.path.dirname(data)
        self.gen = TypeTreeGenerator(UNITY_VERSION)
        self.gen.load_local_game(self.root)
        self.envs = {}

    def env(self, fname):
        if fname not in self.envs:
            e = UnityPy.load(os.path.join(self.data, fname))
            e.typetree_generator = self.gen
            self.envs[fname] = (e, {o.path_id: o for o in e.objects})
        return self.envs[fname]

    def script_names(self):
        e, objs = self.env("globalgamemanagers.assets")
        return {pid: o.read_typetree()["m_ClassName"] for pid, o in objs.items() if o.type.name == "MonoScript"}

    def find_mono(self, class_name, files):
        names = self.script_names()
        want = {pid for pid, n in names.items() if n == class_name}
        for f in files:
            p = os.path.join(self.data, f)
            if not os.path.exists(p):
                continue
            e, objs = self.env(f)
            for pid, o in objs.items():
                if o.type.name != "MonoBehaviour":
                    continue
                try:
                    raw = o.get_raw_data()
                    if struct.unpack_from("<iq", raw, 16)[1] in want:
                        return f, o
                except Exception:
                    pass
        return None, None


def externals(env_name_file, game):
    e, _ = game.env(env_name_file)
    sf = list(e.files.values())[0]
    return [x.path.split("/")[-1] for x in sf.externals]


# ───────────────────────── randomizer parsing ─────────────────────────
class Raw:
    def __init__(self, b, p=0):
        self.b, self.p = b, p

    def i(self):
        v = struct.unpack_from("<i", self.b, self.p)[0]; self.p += 4; return v

    def f(self):
        v = struct.unpack_from("<f", self.b, self.p)[0]; self.p += 4; return round(v, 4)

    def s(self):
        n = self.i()
        if not 0 <= n < 200:
            raise ValueError("bad string")
        v = self.b[self.p:self.p + n].decode("ascii"); self.p += n; self.p = (self.p + 3) & ~3; return v


def parse_randomizer(raw):
    out = {"floats": [], "colors": []}
    def find(name, start=0):
        pat = struct.pack("<i", len(name)) + name.encode()
        return raw.find(pat, start)
    pos = 0
    for _ in range(2):  # male / female sets
        k = find("eyesScale", pos)
        if k < 0:
            break
        try:
            r = Raw(raw, k - 4); n = r.i(); fl = {}
            for _ in range(n):
                name = r.s(); lo = r.f(); hi = r.f(); nl = r.i()
                linked = [r.s() for _ in range(nl)]
                fl[name] = {"min": lo, "max": hi, "linked": linked}
            out["floats"].append(fl); pos = r.p
        except Exception:
            break
    pos = 0
    for _ in range(2):
        k = find("hairColor", pos)
        if k < 0:
            break
        try:
            r = Raw(raw, k - 4); nn = r.i(); names = [r.s() for _ in range(nn)]
            nc = r.i(); cols = [[r.f() for _ in range(4)] for _ in range(nc)]
            out["colors"].append({"controls": names, "colors": cols}); pos = r.p
        except Exception:
            break
    k = find("eyesColor")
    if k >= 0:
        try:
            r = Raw(raw, k - 4); nn = r.i(); names = [r.s() for _ in range(nn)]
            nc = r.i(); cols = [[r.f() for _ in range(4)] for _ in range(nc)]
            out["colors"].append({"controls": names, "colors": cols})
        except Exception:
            pass
    return out


# ───────────────────────── rendering ─────────────────────────
def thumb_texture(img, control, size):
    img = img.convert("RGBA")
    w, h = img.size
    frac = CENTER_CROP.get(control)
    if frac:
        s = int(min(w, h) * frac)
        cx, cy = w // 2, h // 2
        img = img.crop((cx - s // 2, cy - s // 2, cx + s // 2, cy + s // 2))
    else:
        s = max(w, h)
        sq = Image.new("RGBA", (s, s), (0, 0, 0, 0)); sq.paste(img, ((s - w) // 2, (s - h) // 2)); img = sq
    img = img.resize((size, size), Image.LANCZOS)
    bg = Image.new("RGBA", (size, size), (110, 110, 118, 255))
    bg.alpha_composite(img)
    return bg


def decode_mesh(obj):
    from UnityPy.helpers.MeshHelper import MeshHandler
    mesh = obj.read()
    h = MeshHandler(mesh, version=obj.version); h.process()
    V = np.array([v[:3] for v in h.m_Vertices], dtype=np.float32)
    N = np.array([v[:3] for v in h.m_Normals], dtype=np.float32) if h.m_Normals else None
    UV = np.array([v[:2] for v in h.m_UV0], dtype=np.float32) if h.m_UV0 else None
    T = np.array([t for sm in h.get_triangles() for t in sm if len(t) == 3], dtype=np.int64)
    return V, N, UV, T


def render_mesh(V, N, UV, T, tex, color, yaw, pitch, size, ss=3):
    S = size * ss
    ya, pa = np.radians(yaw), np.radians(pitch)
    Ry = np.array([[np.cos(ya), 0, np.sin(ya)], [0, 1, 0], [-np.sin(ya), 0, np.cos(ya)]])
    Rx = np.array([[1, 0, 0], [0, np.cos(pa), -np.sin(pa)], [0, np.sin(pa), np.cos(pa)]])
    R = Rx @ Ry
    P = V @ R.T
    if N is None or len(N) != len(V):
        N = np.zeros_like(V); tri = V[T]; fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        for k in range(3):
            np.add.at(N, T[:, k], fn)
    Nn = N @ R.T
    Nn /= (np.linalg.norm(Nn, axis=1, keepdims=True) + 1e-9)
    lo, hi = P.min(0), P.max(0)
    c = (lo + hi) / 2; ext = max(hi[0] - lo[0], hi[1] - lo[1], 1e-6)
    sc = 0.9 * S / ext
    X = (P[:, 0] - c[0]) * sc + S / 2
    Y = S / 2 - (P[:, 1] - c[1]) * sc
    Z = P[:, 2]
    L = np.array([-0.4, 0.6, 0.7]); L /= np.linalg.norm(L)
    zb = np.full((S, S), -1e9, np.float32)
    out = np.zeros((S, S, 4), np.float32)
    tx = None
    if tex is not None:
        tx = np.asarray(tex.convert("RGBA"), np.float32) / 255.0
    base = np.array(color, np.float32) / 255.0
    for t in T:
        a, b, cc = t
        x0, x1, x2 = X[a], X[b], X[cc]; y0, y1, y2 = Y[a], Y[b], Y[cc]
        minx = max(int(np.floor(min(x0, x1, x2))), 0); maxx = min(int(np.ceil(max(x0, x1, x2))), S - 1)
        miny = max(int(np.floor(min(y0, y1, y2))), 0); maxy = min(int(np.ceil(max(y0, y1, y2))), S - 1)
        if minx > maxx or miny > maxy:
            continue
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(den) < 1e-9:
            continue
        gx, gy = np.meshgrid(np.arange(minx, maxx + 1) + .5, np.arange(miny, maxy + 1) + .5)
        w0 = ((y1 - y2) * (gx - x2) + (x2 - x1) * (gy - y2)) / den
        w1 = ((y2 - y0) * (gx - x2) + (x0 - x2) * (gy - y2)) / den
        w2 = 1 - w0 - w1
        m = (w0 >= -1e-4) & (w1 >= -1e-4) & (w2 >= -1e-4)
        if not m.any():
            continue
        z = w0 * Z[a] + w1 * Z[b] + w2 * Z[cc]
        sub = zb[miny:maxy + 1, minx:maxx + 1]
        m &= z > sub
        if not m.any():
            continue
        n = w0[..., None] * Nn[a] + w1[..., None] * Nn[b] + w2[..., None] * Nn[cc]
        n /= (np.linalg.norm(n, axis=2, keepdims=True) + 1e-9)
        lam = np.abs(n @ L)  # two-sided
        shade = 0.35 + 0.65 * lam
        if tx is not None and UV is not None:
            u = w0 * UV[a, 0] + w1 * UV[b, 0] + w2 * UV[cc, 0]
            v = w0 * UV[a, 1] + w1 * UV[b, 1] + w2 * UV[cc, 1]
            th, tw = tx.shape[:2]
            px = np.clip(((u % 1.0) * tw).astype(int), 0, tw - 1)
            py = np.clip((((1 - v) % 1.0) * th).astype(int), 0, th - 1)
            tc = tx[py, px]
            m &= tc[..., 3] > 0.5
            col = tc[..., :3] * shade[..., None]
        else:
            col = base[None, None, :] * shade[..., None]
        if not m.any():
            continue
        sub[m] = z[m]
        reg = out[miny:maxy + 1, minx:maxx + 1]
        reg[m, :3] = col[m]; reg[m, 3] = 1.0
    img = Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8), "RGBA")
    return img.resize((size, size), Image.BOX)


# ───────────────────────── main build ─────────────────────────
def build(game_path, out_dir, previews=True, size=128):
    G = Game(game_path)
    log("data folder:", G.data)
    lvl = ["level%d" % i for i in range(0, 6)]
    f_res, o_res = G.find_mono("AvatarResources", lvl)
    if o_res is None:
        raise SystemExit("AvatarResources not found")
    log("AvatarResources in", f_res, o_res.path_id)
    res = o_res.read_typetree()
    ext = externals(f_res, G)

    def resolve(ptr):
        fid, pid = ptr["m_FileID"], ptr["m_PathID"]
        if pid == 0:
            return None
        fname = os.path.basename(G.data) and (ext[fid - 1] if fid > 0 else f_res)
        e, objs = G.env(fname)
        return objs.get(pid)

    name_cache = {}

    def obj_name(o):
        if o is None:
            return ""
        k = (id(o.assets_file), o.path_id)
        if k not in name_cache:
            name_cache[k] = o.peek_name() if hasattr(o, "peek_name") else o.read().m_Name
        return name_cache[k]

    controls = collections.OrderedDict()
    objmap = {}  # (control, value) -> first object
    for r in res["m_aAssetResources"]:
        cname = r["m_sAvatarControlName"]
        ctl = {"name": cname, "type": "string", "kind": None, "allow_empty": bool(r["m_allowEmptyOption"]),
               "depends_on": r["m_sParentControl"] or None, "options": []}
        idx = {}
        order = []

        def add(items, parent):
            for pos, it in enumerate(items):
                o = resolve(it["m_oResource"])
                val = obj_name(o)
                key = val
                if key not in idx:
                    idx[key] = {"value": val, "label": label_of(val), "parents": [], "preview": None,
                                "special": False, "unlockables": [], "lock": None, "avatar_filter": [],
                                "inactive": False, "_pos": []}
                    order.append(key)
                    objmap[(cname, val)] = o
                d = idx[key]
                if parent is not None and parent not in d["parents"]:
                    d["parents"].append(parent)
                d["_pos"].append(pos)
                d["special"] |= bool(it["m_bSpecial"])
                d["inactive"] |= not it["m_bActive"]
                u = it["m_sUnlockable"]
                if u and u not in d["unlockables"]:
                    d["unlockables"].append(u)
                for a in it["m_aiAvatarFilter"]:
                    if a not in d["avatar_filter"]:
                        d["avatar_filter"].append(a)
                d["_item"] = it

        add(r["m_aoResources"], None)
        for dep in r["m_dependantResources"]:
            add(dep["m_aoResources"], dep["m_keyValue"])
        for k in order:
            d = idx[k]
            kinds = [lock_info(u) for u in d["unlockables"]]
            d["lock"] = kinds[0] if kinds else None
            d["locked"] = bool(d["unlockables"]) and d["lock"] in ("dlc", "achievement", "goat", "other")
            d.pop("_item", None)
            ctl["options"].append(d)
        ctl["kind"] = "mesh" if cname in MESH_CONTROLS else "texture"
        if r["m_oEmptyAsset"]["m_PathID"]:
            ctl["empty_asset"] = obj_name(resolve(r["m_oEmptyAsset"]))
        controls[cname] = ctl

    # parallel mesh/texture pairing by index (first occurrence)
    for mc, tc in PAIRS.items():
        if mc in controls and tc in controls:
            for d in controls[mc]["options"]:
                tex = []
                for p in d["_pos"]:
                    try:
                        t = None
                        # parallel list: find the tc option that has same position
                        for o2 in controls[tc]["options"]:
                            if p in o2["_pos"]:
                                t = o2["value"]; break
                        if t and t not in tex:
                            tex.append(t)
                    except Exception:
                        pass
                d["paired_textures"] = tex
    if "hairMesh" in controls and "hairTexture" in controls:
        tv = {d["value"] for d in controls["hairTexture"]["options"]}
        for d in controls["hairMesh"]["options"]:
            t = re.sub(r"^mesh_", "txt_", d["value"]).replace("_hat", "")
            d["paired_textures"] = [t] if t in tv else []
    for c in controls.values():
        for d in c["options"]:
            d.pop("_pos", None)

    # bundled avatars
    f_mgr, o_mgr = G.find_mono("AvatarManager", lvl)
    mgr = o_mgr.read_typetree()
    avatars = []
    ctrl_types = collections.defaultdict(lambda: collections.defaultdict(list))
    for ptr in mgr["m_aoBundledAvatarContainers"]:
        ext_m = externals(f_mgr, G)
        fname = ext_m[ptr["m_FileID"] - 1] if ptr["m_FileID"] > 0 else f_mgr
        _, objs = G.env(fname)
        a = objs[ptr["m_PathID"]].read_typetree()["m_oAvatarData"]
        vals = []
        for v in a["values"]:
            t = v["m_controlValueType"]
            e = {"name": v["m_name"], "type": t}
            if t == 0: e["value"] = v["m_string"]
            elif t == 1: e["value"] = round(v["m_float"], 5)
            else:
                c = v["m_color"]; e["value"] = [round(c[k], 5) for k in ("r", "g", "b", "a")] if isinstance(c, dict) else list(c)
            ctrl_types[v["m_name"]][t].append(e["value"])
            vals.append(e)
        avatars.append({"id": a["m_iAvatarUniqueId"], "name": a["m_sName"], "number": a["m_iNumber"],
                        "avatar_type": ["BUNDLED", "MASCOT", "EDITOR", "GOAT_CHAR"][a["m_eAvatarType"]]
                        if 0 <= a["m_eAvatarType"] < 4 else a["m_eAvatarType"],
                        "team_exclusive": a["m_iTeamExclusive"], "unlock": a.get("m_sAvatarUnlock", ""),
                        "super_fan": bool(a["m_bSuperFan"]), "values": vals})

    # randomizer
    f_rnd, o_rnd = G.find_mono("AvatarRandomizer", lvl)
    rnd = parse_randomizer(o_rnd.get_raw_data()[28:]) if o_rnd else {"floats": [], "colors": []}

    # float / colour controls
    palette = [[round(c[k], 4) for k in ("r", "g", "b", "a")] for c in res["m_aoPickerColors"]]
    cat_controls = [c for c in controls.values()]
    for name, tys in sorted(ctrl_types.items()):
        if name in controls:
            continue
        if 1 in tys:
            fl = tys[1]
            ent = {"name": name, "type": "float", "min": 0.0, "max": 1.0,
                   "default": collections.Counter(fl).most_common(1)[0][0],
                   "observed_min": min(fl), "observed_max": max(fl),
                   "description": FLOAT_DESC.get(name, name)}
            if rnd["floats"]:
                ent["randomizer_range"] = [fs[name] for fs in rnd["floats"] if name in fs]
            cat_controls.append(ent)
        elif 2 in tys:
            ent = {"name": name, "type": "color", "palette": palette, "palette_note":
                   "editor colour-picker palette (AvatarResources.m_aoPickerColors); free colour also accepted by the game",
                   "description": COLOR_DESC.get(name, name),
                   "observed": [list(x) for x in {tuple(x) for x in tys[2]}][:30]}
            ent["randomizer_palettes"] = [c["colors"] for c in rnd["colors"] if name in c["controls"]]
            cat_controls.append(ent)
    for name in ("eyesColor",):
        if name not in [c["name"] for c in cat_controls]:
            pass

    catalog = {"game_unity_version": UNITY_VERSION,
               "source": {"option_lists": "AvatarResources in " + f_res, "avatars": "AvatarManager in " + f_mgr},
               "notes": ["option.value is the exact string the game stores (the referenced asset's m_Name).",
                         "lock: dlc/achievement/goat = unlockable; random_only = hidden from editor, used by the randomizer (special).",
                         "previews flagged approx=true are rendered approximations."],
               "controls": cat_controls, "avatars": avatars}

    # previews
    pdir = os.path.join(out_dir, "previews")
    stats = collections.Counter()
    if previews:
        tex_cache = {}
        def get_tex(ctl, val):
            o = objmap.get((ctl, val))
            if o is None: return None
            k = (ctl, val)
            if k not in tex_cache:
                try: tex_cache[k] = o.read().image
                except Exception as ex: log("tex fail", ctl, val, ex); tex_cache[k] = None
            return tex_cache[k]
        for c in controls.values():
            cd = os.path.join(pdir, safe(c["name"])); os.makedirs(cd, exist_ok=True)
            for d in c["options"]:
                if not d["value"]:
                    continue
                fn = safe(d["value"]) + ".png"; fp = os.path.join(cd, fn)
                try:
                    if c["kind"] == "texture":
                        im = get_tex(c["name"], d["value"])
                        if im is None: continue
                        thumb_texture(im, c["name"], size).save(fp); d["approx"] = c["name"] in CENTER_CROP
                    else:
                        V, N, UV, T = decode_mesh(objmap[(c["name"], d["value"])])
                        tex = None
                        for tv in d.get("paired_textures", []):
                            tctl = PAIRS.get(c["name"], "hairTexture")
                            tex = get_tex(tctl, tv)
                            if tex is not None: break
                        yaw, pitch = VIEWS[c["name"]]
                        col = FLAT_COLORS.get(c["name"], (170, 170, 175))
                        use_tex = tex if c["name"] != "hairMesh" else None
                        render_mesh(V, N, UV, T, use_tex, col, yaw, pitch, size).save(fp)
                        d["approx"] = True
                        d["preview_note"] = ("textured" if use_tex is not None else "flat shaded, neutral colour")
                    d["preview"] = "previews/%s/%s" % (safe(c["name"]), fn); stats[c["name"]] += 1
                except Exception as ex:
                    log("preview fail", c["name"], d["value"], ex)
        log("previews:", dict(stats))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "catalog.json"), "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=1, ensure_ascii=False)
    for c in cat_controls:
        log(c["name"], c["type"], len(c.get("options", [])) if c["type"] == "string" else "")
    return catalog


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("game"); ap.add_argument("out", nargs="?")
    ap.add_argument("--no-previews", action="store_true"); ap.add_argument("--size", type=int, default=128)
    a = ap.parse_args()
    out = a.out or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "avatar_catalog")
    build(a.game, out, not a.no_previews, a.size)
