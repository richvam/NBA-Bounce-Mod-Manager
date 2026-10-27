"""
avatar_render.py -- draw an avatar's head the way the game builds it, offline.

How NBA Bounce assembles a head (see tools/build_render_cache.py for where
each piece comes from):

1. FACE TEXTURE. A small rig (avatarHeadMaster) of unlit, alpha-blended quads
   is filmed by an orthographic camera (size 0.5) into a RenderTexture:
   skin head texture (opaque, at the back), nose shadow, hairTextureHead
   (tinted by hairColor), then eyes (white + iris tinted by eyesColor + shine),
   eyebrows (tinted by eyebrowsColor) and the mouth. The face sliders do not
   touch shaders: SuperAvatar's CustomizationControls move / scale / rotate
   the parent Transforms of those quads:
       Scale        localScale[axis] = LerpUnclamped(v2.x, v2.y, value)
       MoveRelative Translate(v3 * Lerp(v2.x, v2.y, value), Space.Self)
                    from the rest position
       RotateAxis   Rotate(v3 * Lerp(v2.x, v2.y, value), Space.Self)
                    (rest pose == value 0.5)
   We replay exactly that and composite the quads back-to-front with
   SrcAlpha/OneMinusSrcAlpha, like the URP Particles/Unlit material does.
2. HEAD. headScale (scaled by headSizeX on X/Z, headSizeY on Y) holds the head
   mesh and the 3D nose, both textured with that RenderTexture (URP/Lit,
   _BaseColor 0.717), plus hair (hairTexture x hairColor), hat and goggles.

Public API:
    available() -> bool
    render_head(values, size=256, bg=None, quality="high") -> PIL.Image (RGBA)
        values: control -> value (strings, floats, "#rrggbb" or [r,g,b,a])
        quality "fast" renders without supersampling (for slider drags).
"""

from __future__ import annotations

import io
import json
import math
import os
import threading
import zipfile

import numpy as np
from PIL import Image

try:
    import app_paths
    CACHE_DIR = os.path.join(app_paths.APP_DIR, "avatar_catalog", "render_cache")
except Exception:  # pragma: no cover - standalone use
    CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "avatar_catalog", "render_cache")

SCALE, BLENDSHAPE, ANIMLAYER, MESHCHANGE, TEXCHANGE, COLOR, ROTATE, MOVE = 0, 1, 2, 3, 4, 5, 6, 7

# camera: a little to the side and slightly above, like the character picker
YAW_DEG, PITCH_DEG, FOV_DEG = 14.0, 6.0, 22.0
LIGHT = np.array([-0.45, 0.55, 0.70], np.float32)
LIGHT /= np.linalg.norm(LIGHT)
AMBIENT, DIFFUSE = 0.58, 0.55
FILL = 1.0
FRAME_SPAN = 1.34                 # frame height / default head height (room for hair, hats)
FRAME_BELOW = 0.04                # margin under the chin, in head heights
SKIN_TINT = 0.717 * 1.35          # material _BaseColor, brightened for an unlit-looking UI preview


# ------------------------------------------------------------------ math
def _quat_mat(q):
    x, y, z, w = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]], np.float64)


def _euler_mat(e):
    """Unity Quaternion.Euler (Z, then X, then Y) as a 3x3 matrix."""
    x, y, z = (math.radians(a) for a in e)
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Ry @ Rx @ Rz


def _lerp(a, b, t, clamp=True):
    if clamp:
        t = min(1.0, max(0.0, t))
    return a + (b - a) * t


def _to_float(v, default=0.5):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_rgb(v, default=(1.0, 1.0, 1.0)):
    if v is None or v == "":
        return default
    if isinstance(v, (list, tuple)):
        return tuple(float(c) for c in v[:3])
    s = str(v).strip().lstrip("#")
    if len(s) >= 6:
        try:
            return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        except ValueError:
            pass
    return default


# ------------------------------------------------------------------ cache
class _Cache:
    def __init__(self, folder):
        with open(os.path.join(folder, "scene.json"), encoding="utf-8") as fh:
            self.scene = json.load(fh)
        self.nodes = {int(k): v for k, v in self.scene["nodes"].items()}
        self.controls = self.scene["controls"]
        self.index = self.scene["index"]
        self._npz = np.load(os.path.join(folder, "meshes.npz"))
        self._zip = zipfile.ZipFile(os.path.join(folder, "textures.zip"))
        self._meshes, self._tex = {}, {}
        self._lock = threading.Lock()

    def mesh(self, name):
        if name not in self._meshes:
            if name + "|V" not in self._npz.files:
                self._meshes[name] = None
            else:
                g = lambda k: self._npz[name + "|" + k]
                uv = g("UV").astype(np.float32)
                uv2 = g("UV2").astype(np.float32) if name + "|UV2" in self._npz.files else uv
                self._meshes[name] = (g("V").astype(np.float32), g("N").astype(np.float32),
                                      uv, uv2, g("T").astype(np.int64))
        return self._meshes[name]

    def texture(self, control, name):
        key = (control, name)
        if key not in self._tex:
            path = self.index["textures"].get(control, {}).get(name)
            im = None
            if path:
                with self._lock:
                    data = self._zip.read(path)
                im = Image.open(io.BytesIO(data)).convert("RGBA")
                im.load()
            self._tex[key] = im
        return self._tex[key]

    def tex_array(self, control, name):
        key = ("arr", control, name)
        if key not in self._tex:
            im = self.texture(control, name)
            self._tex[key] = None if im is None else np.asarray(im, np.float32) / 255.0
        return self._tex[key]


_CACHE = None
_CACHE_ERR = None


def _cache():
    global _CACHE, _CACHE_ERR
    if _CACHE is None and _CACHE_ERR is None:
        try:
            _CACHE = _Cache(CACHE_DIR)
        except Exception as ex:          # missing / damaged cache -> caller falls back
            _CACHE_ERR = ex
    return _CACHE


def available():
    return _cache() is not None


# ------------------------------------------------------------------ rig
class _Rig:
    """Local TRS per node, with SuperAvatar's controls replayed on top."""

    def __init__(self, cache, values):
        self.c = cache
        self.p = {k: np.array(n["p"], np.float64) for k, n in cache.nodes.items()}
        self.r = {k: _quat_mat(n["r"]) for k, n in cache.nodes.items()}
        self.s = {k: np.array(n["s"], np.float64) for k, n in cache.nodes.items()}
        self.tex, self.color, self.mesh = {}, {}, {}
        moves = []
        for ctl in cache.controls:
            name, typ = ctl["name"], ctl["type"]
            if name not in values or values[name] is None:
                continue
            v = values[name]
            a, b = ctl["v2"]
            if typ == SCALE:
                val = _lerp(a, b, _to_float(v), clamp=False)
                for o in ctl["objs"]:
                    for i in range(3):
                        if ctl["v3"][i] != 0:
                            self.s[o][i] = val
            elif typ == ROTATE:
                ang = _lerp(a, b, _to_float(v))
                R = _euler_mat([c * ang for c in ctl["v3"]])
                for o in ctl["objs"]:
                    self.r[o] = self.r[o] @ R
            elif typ == MOVE:
                d = np.array(ctl["v3"], np.float64) * _lerp(a, b, _to_float(v))
                for o in ctl["objs"]:
                    moves.append((o, d))
            elif typ == COLOR:
                for o in ctl["objs"]:
                    self.color[o] = _to_rgb(v)
            elif typ == TEXCHANGE:
                for o in ctl["objs"]:
                    self.tex.setdefault(o, {})[ctl["str"] or "_BaseMap"] = (name, str(v))
            elif typ == MESHCHANGE:
                for o in ctl["objs"]:
                    self.mesh[o] = str(v)
        if moves:
            lin = self._linear_world()
            for o, d in moves:              # Translate(d, Space.Self): rotation only
                par = cache.nodes[o]["parent"]
                Rw = self._rot_world(o)
                wd = Rw @ d
                self.p[o] = self.p[o] + (np.linalg.solve(lin[par], wd) if par is not None else wd)

    def _local(self, k):
        M = np.eye(4)
        M[:3, :3] = self.r[k] * self.s[k][None, :]
        M[:3, 3] = self.p[k]
        return M

    def _rot_world(self, k):
        R = np.eye(3)
        while k is not None:
            R = self.r[k] @ R
            k = self.c.nodes[k]["parent"]
        return R

    def _linear_world(self):
        out = {}
        for k in self.c.nodes:
            M = np.eye(3); j = k
            while j is not None:
                M = (self.r[j] * self.s[j][None, :]) @ M
                j = self.c.nodes[j]["parent"]
            out[k] = M
        return out

    def world(self):
        out = {}

        def rec(k, P):
            M = P @ self._local(k)
            out[k] = M
            for ch in self.c.nodes[k]["children"]:
                if ch in self.c.nodes:
                    rec(ch, M)
        for k, n in self.c.nodes.items():
            if n["parent"] is None:
                rec(k, np.eye(4))
        return out

    def active(self, k):
        while k is not None:
            if not self.c.nodes[k]["active"]:
                return False
            k = self.c.nodes[k]["parent"]
        return True


# ------------------------------------------------------------------ face texture
def _face_texture(rig, W, res):
    """Composite the avatarHeadMaster quads into an RGB texture (res x res)."""
    cam = rig.c.scene["camera"]
    cx, cy, half = cam["p"][0], cam["p"][1], cam["ortho_size"]
    canvas = Image.new("RGBA", (res, res), (200, 160, 130, 255))
    layers = []
    face_root = rig.c.scene["face_root"]
    for k, n in rig.c.nodes.items():
        if "mat" not in n or not n.get("renderer_enabled", True) or not rig.active(k):
            continue
        # only nodes of the face rig
        j = k
        while rig.c.nodes[j]["parent"] is not None:
            j = rig.c.nodes[j]["parent"]
        if j != face_root:
            continue
        t = rig.tex.get(k, {}).get("_BaseMap")
        if not t:
            continue
        im = rig.c.texture(*t)
        if im is None:
            continue
        M = W[k]
        opaque = not n["mat"]["transparent"]
        layers.append((0 if opaque else 1, -M[2, 3], k, im, M))
    layers.sort(key=lambda x: (x[0], x[1]))
    s = res / (2 * half)
    for _, _, k, im, M in layers:
        n = rig.c.nodes[k]
        col = np.array(n["mat"]["base_color"][:3], np.float32) * np.array(rig.color.get(k, (1, 1, 1)), np.float32)
        alpha = n["mat"]["base_color"][3]
        tw, th = im.size
        # quad local (x,y) in [-.5,.5] -> texture px: (x+.5)*tw, (.5-y)*th
        # local -> RT px: col = (wx-cx+half)*s, row = (half-(wy-cy))*s
        A = np.array([[M[0, 0] * s, M[0, 1] * s, (M[0, 3] - cx + half) * s],
                      [-M[1, 0] * s, -M[1, 1] * s, (half - (M[1, 3] - cy)) * s],
                      [0, 0, 1]])
        Tt = np.array([[1.0 / tw, 0, -0.5], [0, -1.0 / th, 0.5], [0, 0, 1]])   # tex px -> local
        F = A @ Tt                                                              # tex px -> RT px
        try:
            Finv = np.linalg.inv(F)
        except np.linalg.LinAlgError:
            continue
        lay = im.transform((res, res), Image.AFFINE, tuple(Finv[:2].ravel()), resample=Image.BILINEAR)
        if not np.allclose(col, 1.0) or alpha < 1:
            a = np.asarray(lay, np.float32)
            a[..., :3] *= col
            a[..., 3] *= alpha
            lay = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGBA")
        if n["mat"]["transparent"]:
            canvas.alpha_composite(lay)
        else:
            canvas.paste(lay.convert("RGB"), (0, 0), lay.getchannel("A"))
    return np.asarray(canvas.convert("RGB"), np.float32) / 255.0


# ------------------------------------------------------------------ rasterizer
def _sample(tex, u, v, wrap=True):
    """Bilinear sample tex (H,W,C float) at Unity UVs (v up)."""
    H, Wd = tex.shape[:2]
    x = u * Wd - 0.5
    y = (1.0 - v) * H - 0.5
    x0 = np.floor(x); y0 = np.floor(y)
    fx = (x - x0)[..., None]; fy = (y - y0)[..., None]
    x0 = x0.astype(np.int64); y0 = y0.astype(np.int64)
    if wrap:
        xa, xb = x0 % Wd, (x0 + 1) % Wd
        ya, yb = y0 % H, (y0 + 1) % H
    else:
        xa, xb = np.clip(x0, 0, Wd - 1), np.clip(x0 + 1, 0, Wd - 1)
        ya, yb = np.clip(y0, 0, H - 1), np.clip(y0 + 1, 0, H - 1)
    return ((tex[ya, xa] * (1 - fx) + tex[ya, xb] * fx) * (1 - fy) +
            (tex[yb, xa] * (1 - fx) + tex[yb, xb] * fx) * fy)


def _raster(S, objects):
    """objects: list of dict(P=(n,3) camera space [x right, y up, depth], T, ...).
    Returns per-pixel (obj id, tri id, b0, b1, b2) for the nearest surface."""
    best_d = np.full(S * S, np.inf, np.float32)
    best_o = np.full(S * S, -1, np.int32)
    best_t = np.zeros(S * S, np.int64)
    best_b = np.zeros((S * S, 3), np.float32)
    for oi, ob in enumerate(objects):
        P, T = ob["P"], ob["T"]
        sx, sy, dz = P[:, 0], P[:, 1], P[:, 2]
        x0, x1, x2 = sx[T[:, 0]], sx[T[:, 1]], sx[T[:, 2]]
        y0, y1, y2 = sy[T[:, 0]], sy[T[:, 1]], sy[T[:, 2]]
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        keep = (area * ob.get("winding", 1) > 1e-9) & (dz[T].min(1) > 0)
        if not keep.any():
            continue
        T = T[keep]; x0, x1, x2, y0, y1, y2, area = (a[keep] for a in (x0, x1, x2, y0, y1, y2, area))
        xmin = np.clip(np.ceil(np.minimum(np.minimum(x0, x1), x2) - 0.5), 0, S).astype(np.int64)
        xmax = np.clip(np.floor(np.maximum(np.maximum(x0, x1), x2) - 0.5), -1, S - 1).astype(np.int64)
        ymin = np.clip(np.ceil(np.minimum(np.minimum(y0, y1), y2) - 0.5), 0, S).astype(np.int64)
        ymax = np.clip(np.floor(np.maximum(np.maximum(y0, y1), y2) - 0.5), -1, S - 1).astype(np.int64)
        w = xmax - xmin + 1; h = ymax - ymin + 1
        ok = (w > 0) & (h > 0)
        if not ok.any():
            continue
        idx = np.nonzero(ok)[0]
        cnt = (w * h)[idx]
        tri = np.repeat(idx, cnt)
        off = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        px = xmin[tri] + off % w[tri]
        py = ymin[tri] + off // w[tri]
        cxp = px + 0.5; cyp = py + 0.5
        inv = 1.0 / area[tri]
        b0 = ((x1[tri] - cxp) * (y2[tri] - cyp) - (x2[tri] - cxp) * (y1[tri] - cyp)) * inv
        b1 = ((x2[tri] - cxp) * (y0[tri] - cyp) - (x0[tri] - cxp) * (y2[tri] - cyp)) * inv
        b2 = 1.0 - b0 - b1
        m = (b0 >= -1e-6) & (b1 >= -1e-6) & (b2 >= -1e-6)
        tri, px, py, b0, b1, b2 = tri[m], px[m], py[m], b0[m], b1[m], b2[m]
        Tt = T[tri]
        # perspective-correct barycentrics
        iz0, iz1, iz2 = 1.0 / dz[Tt[:, 0]], 1.0 / dz[Tt[:, 1]], 1.0 / dz[Tt[:, 2]]
        izs = b0 * iz0 + b1 * iz1 + b2 * iz2
        depth = (1.0 / izs).astype(np.float32)
        pb0, pb1 = b0 * iz0 / izs, b1 * iz1 / izs
        pix = py * S + px
        # nearest candidate per pixel: one integer sort on (pixel, quantised depth)
        dmin, dmax = float(depth.min()), float(depth.max())
        dq = ((depth - dmin) * (((1 << 22) - 1) / max(dmax - dmin, 1e-9))).astype(np.int64)
        order = np.argsort((pix.astype(np.int64) << 22) | dq)
        ps = pix[order]
        first = np.ones(len(ps), bool)
        first[1:] = ps[1:] != ps[:-1]
        sel = order[first]
        pix, depth, tri, pb0, pb1 = pix[sel], depth[sel], tri[sel], pb0[sel], pb1[sel]
        better = depth < best_d[pix]
        pix = pix[better]
        best_d[pix] = depth[better]
        best_o[pix] = oi
        orig = np.nonzero(keep)[0]
        best_t[pix] = orig[tri[better]]
        best_b[pix, 0] = pb0[better]; best_b[pix, 1] = pb1[better]
        best_b[pix, 2] = 1.0 - pb0[better] - pb1[better]
    return best_o, best_t, best_b


# ------------------------------------------------------------------ main
def _head_objects(rig, W):
    """Collect the headScale renderers as (V world, N world, UV, T, kind, tex)."""
    out = []
    head_root = rig.c.scene["head_root"]
    for k, n in rig.c.nodes.items():
        if "mat" not in n or not rig.active(k):
            continue
        j = k
        while rig.c.nodes[j]["parent"] is not None:
            j = rig.c.nodes[j]["parent"]
        if j != head_root:
            continue
        mname = rig.mesh.get(k)
        if not mname:
            continue
        mesh = rig.c.mesh(mname)
        if mesh is None:
            continue
        V, N, UV, UV2, T = mesh
        M = W[k]
        Vw = V @ M[:3, :3].T + M[:3, 3]
        Nw = N @ np.linalg.inv(M[:3, :3])
        Nw /= np.linalg.norm(Nw, axis=1, keepdims=True) + 1e-9
        mat = n["mat"]["name"].lower()
        kind = ("skin" if "skinhead" in mat else "hair" if "hair" in mat else
                "hat" if "hat" in mat else "goggles" if "goggles" in mat else "other")
        out.append(dict(k=k, V=Vw, N=Nw, UV=UV, UV2=UV2, T=T, kind=kind))
    return out


_FRAME = None


def _frame(c):
    """(centre, half height) of the portrait frame, from the default head mesh."""
    global _FRAME
    if _FRAME is None:
        rig = _Rig(c, {"headMesh": "mesh_headTestNose2"})
        W = rig.world()
        objs = [o for o in _head_objects(rig, W) if o["kind"] == "skin"]
        V = np.concatenate([o["V"] for o in objs]) if objs else np.array([[0, 0, 0], [0, .6, 0]], np.float32)
        lo, hi = V.min(0), V.max(0)
        h = float(hi[1] - lo[1])
        half = h * FRAME_SPAN / 2
        ctr = np.array([(lo[0] + hi[0]) / 2, lo[1] - FRAME_BELOW * h + half, (lo[2] + hi[2]) / 2])
        _FRAME = (ctr, half)
    return _FRAME


def render_head(values, size=256, bg=None, quality="high"):
    """Render the avatar head for `values` -> RGBA PIL image (size x size)."""
    c = _cache()
    if c is None:
        raise RuntimeError("avatar render cache missing: %s" % _CACHE_ERR)
    values = dict(values or {})
    rig = _Rig(c, values)
    W = rig.world()
    fast = quality == "fast"
    face = _face_texture(rig, W, 256 if (fast or size <= 128) else 400)
    objs = _head_objects(rig, W)
    ss = 1 if (fast and size >= 160) else 2         # supersampling (anti-aliasing)
    S = size * ss
    if not objs:
        return Image.new("RGBA", (size, size), bg or (0, 0, 0, 0))

    # camera: yaw/pitch the head, frame the head+hair bounds
    ya, pa = math.radians(YAW_DEG), math.radians(PITCH_DEG)
    Ry = np.array([[math.cos(ya), 0, math.sin(ya)], [0, 1, 0], [-math.sin(ya), 0, math.cos(ya)]])
    Rx = np.array([[1, 0, 0], [0, math.cos(pa), -math.sin(pa)], [0, math.sin(pa), math.cos(pa)]])
    R = Rx @ Ry
    # Fixed framing from the rest-pose default head, so head size / hair volume
    # changes show up as they would in the picker instead of being re-fitted.
    ctr, half_h = _frame(c)
    f = 1.0 / math.tan(math.radians(FOV_DEG) / 2)
    dist = half_h * f / FILL                          # frame height fits the centre plane
    proj = []
    for o in objs:
        Pv = (o["V"] - ctr) @ R.T                     # model front is +Z
        # camera on +Z looking -Z (Unity: screen right = -X)
        depth = dist - Pv[:, 2]
        proj.append((-Pv[:, 0] * f / depth, -Pv[:, 1] * f / depth, depth))
        o["Nv"] = o["N"] @ R.T
    k = ox = oy = S / 2
    for o, (px, py, depth) in zip(objs, proj):
        o["P"] = np.stack([px * k + ox, py * k + oy, depth], 1).astype(np.float64)
    bo, bt, bb = _raster(S, objs)
    img = np.zeros((S * S, 4), np.float32)
    hair_rgb = np.array(_to_rgb(values.get("hairColor"), (0.2, 0.15, 0.1)), np.float32)
    for oi, o in enumerate(objs):
        sel = np.nonzero(bo == oi)[0]
        if not len(sel):
            continue
        tri = o["T"][bt[sel]]
        b = bb[sel]
        uv = (o["UV"][tri] * b[..., None]).sum(1)
        nrm = (o["Nv"][tri] * b[..., None]).sum(1)
        nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-9
        # view space: x right is -X; flip x of the normal for the light
        nl = nrm * np.array([-1, 1, 1], np.float32)
        lam = np.clip(nl @ LIGHT, 0, 1)
        shade = (AMBIENT + DIFFUSE * lam)[:, None]
        kind = o["kind"]
        if kind == "skin":
            col = _sample(face, uv[:, 0], uv[:, 1], wrap=False) * SKIN_TINT
        elif kind == "hair":
            t = rig.tex.get(o["k"], {})
            ht = c.tex_array(*t["_BaseMap"]) if "_BaseMap" in t else None
            uv2 = (o["UV2"][tri] * b[..., None]).sum(1)
            # shd_lit_AO_UV2: the per-hairstyle texture is an AO map on UV2
            base = _sample(ht, uv2[:, 0], uv2[:, 1])[:, :3] if ht is not None else np.ones((len(sel), 3), np.float32)
            pt = c.tex_array(*t["_TiledTexture"]) if "_TiledTexture" in t else None
            if pt is not None:                    # strand pattern, tiled on UV0
                pat = _sample(pt, uv[:, 0], uv[:, 1])[:, :1] / 0.4
                base = base * (0.75 + 0.25 * pat)
            col = base * hair_rgb * 1.25 + 0.04
        else:
            t = rig.tex.get(o["k"], {}).get("_BaseMap")
            ta = c.tex_array(*t) if t else None
            col = _sample(ta, uv[:, 0], uv[:, 1])[:, :3] if ta is not None else np.full((len(sel), 3), 0.8, np.float32)
        img[sel, :3] = col * shade
        img[sel, 3] = 1.0
    img = (np.clip(img, 0, 1) * 255).astype(np.uint8).reshape(S, S, 4)
    out = Image.fromarray(img, "RGBA")
    if ss > 1:
        out = out.resize((size, size), Image.BOX)
    if bg is not None:
        base = Image.new("RGBA", (size, size), bg)
        base.alpha_composite(out)
        out = base
    return out


def face_texture(values, res=512):
    """The face RenderTexture alone (debug / inspection)."""
    c = _cache()
    rig = _Rig(c, dict(values or {}))
    return Image.fromarray((np.clip(_face_texture(rig, rig.world(), res), 0, 1) * 255).astype(np.uint8), "RGB")
