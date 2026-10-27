"""Build avatar_catalog/render_cache/ -- everything modules/avatar_render.py needs
to draw an avatar head without the game installed.

Usage:
    python tools/build_render_cache.py <game install or *_Data folder> [out_dir]
        [--tex 256]

READ-ONLY on the game folder. Requires UnityPy (>=1.25), Pillow, numpy.

HOW THE GAME BUILDS A HEAD (what this cache captures)
  level0 holds the avatar rig (AvatarStuff/AvatarManager/visual) and its
  SuperAvatar MonoBehaviour. SuperAvatar.m_controlList is a list of
  CustomizationControl {m_name, m_controlType, m_controlObjects[], m_string,
  m_ints[], m_vector2, m_vector3, m_counterScaleObjects[]}; each control drives
  Transforms / Renderers in that rig:
    * Face: avatarHeadMaster/baseTexture is a stack of unlit, alpha-blended
      quads (skin head texture, hairTextureHead, nose shadow, eyes + iris +
      shine, eyebrows, mouth) filmed by Camera_avatarHeadMaster (orthographic,
      size 0.5) into a RenderTexture. Face sliders move/scale/rotate those
      quads' parent Transforms (MoveRelative / Scale / RotateAxis controls).
    * Head: rig_game/DEF-spine.006/headScale holds mesh_headTestNose2 (textured
      with that RenderTexture through URP/Lit, _BaseColor 0.717), the 3D nose
      (same material), and the hair / hat / goggles meshes. headSizeX/Y scale
      headScale. hairColor sets the hair material's _BaseColor and also tints
      the hairTextureHead quad.
  Stored here: the two rigs' transforms (scene.json), the parsed control list,
  every head-relevant mesh (meshes.npz) and downscaled textures (textures.zip).
"""
import argparse, io, json, os, struct, sys, zipfile

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_avatar_catalog import Game, externals, safe  # noqa: E402

MESH_CTLS = ["headMesh", "noseMesh", "hairMesh", "hatMesh", "gogglesMesh"]
TEX_CTLS = {  # control -> max texture size
    "skinTextureHead": 256, "hairTextureHead": 256, "noseShadow": 128,
    "eyesTexture": 128, "eyesIrisTexture": 128, "eyesBrightTexture": 64,
    "eyebrowsTexture": 128, "mouthTexture": 128, "eyeBlink": 64,
    "hairTexture": 256, "hairTexturePattern": 128, "hatTexture": 256,
    "gogglesTexture": 256}
FACE_ROOT = "avatarHeadMaster"
HEAD_ROOT = "headScale"


def log(*a):
    print(*a, flush=True)


def parse_controls(raw):
    """SuperAvatar raw MonoBehaviour bytes -> list of CustomizationControl dicts."""
    p = [0]

    def i32():
        v = struct.unpack_from("<i", raw, p[0])[0]; p[0] += 4; return v

    def f32():
        v = struct.unpack_from("<f", raw, p[0])[0]; p[0] += 4; return v

    def s():
        n = i32(); v = raw[p[0]:p[0] + n].decode("utf-8", "replace"); p[0] = (p[0] + n + 3) & ~3; return v

    def ptr():
        fid, pid = struct.unpack_from("<iq", raw, p[0]); p[0] += 12; return pid

    ptr(); i32(); ptr(); s()          # m_GameObject, m_Enabled, m_Script, m_Name
    out = []
    for _ in range(i32()):
        c = {"name": s(), "type": i32()}
        c["objs"] = [ptr() for _ in range(i32())]
        c["str"] = s()
        c["ints"] = [i32() for _ in range(i32())]
        c["v2"] = [f32(), f32()]
        c["v3"] = [f32(), f32(), f32()]
        c["counter"] = [ptr() for _ in range(i32())]
        out.append(c)
    return out


def q(v):
    return [float(v.x), float(v.y), float(v.z)] + ([float(v.w)] if hasattr(v, "w") else [])


def mesh_arrays(obj):
    from UnityPy.helpers.MeshHelper import MeshHandler
    mesh = obj.read()
    h = MeshHandler(mesh, version=obj.version); h.process()
    V = np.array([v[:3] for v in h.m_Vertices], np.float32)
    UV = (np.array([v[:2] for v in h.m_UV0], np.float32) if h.m_UV0 else np.zeros((len(V), 2), np.float32))
    UV2 = (np.array([v[:2] for v in h.m_UV1], np.float32) if getattr(h, "m_UV1", None) else None)
    T = np.array([t for sm in h.get_triangles() for t in sm if len(t) == 3], np.int32)
    N = np.array([v[:3] for v in h.m_Normals], np.float32) if h.m_Normals else None
    if N is None or len(N) != len(V):
        N = np.zeros_like(V); tri = V[T]; fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        for k in range(3):
            np.add.at(N, T[:, k], fn)
        N /= np.linalg.norm(N, axis=1, keepdims=True) + 1e-9
    return V, N, UV, UV2, T


def build(game_path, out_dir, tex_max=256):
    G = Game(game_path)
    log("data folder:", G.data)
    os.makedirs(out_dir, exist_ok=True)

    # ---- rigs + SuperAvatar control list (level0)
    f_sa, o_sa = G.find_mono("SuperAvatar", ["level0"])
    if o_sa is None:
        raise SystemExit("SuperAvatar not found in level0")
    controls = parse_controls(o_sa.get_raw_data())
    env, objs = G.env("level0")
    tfs = {pid: o.read() for pid, o in objs.items() if o.type.name == "Transform"}
    go_tf = {}
    for pid, t in tfs.items():
        go_tf[t.m_GameObject.path_id] = pid

    def tname(pid):
        return tfs[pid].m_GameObject.read().m_Name

    def find(name, under=None):
        for pid in tfs:
            if tname(pid) == name:
                if under is None:
                    return pid
                f = tfs[pid].m_Father.path_id
                while f:
                    if tname(f) == under:
                        return pid
                    f = tfs[f].m_Father.path_id
        return None

    nodes = {}
    tex_defaults = {}

    def comp_of(go, kind):
        for c in go.m_Component:
            cc = c.component if hasattr(c, "component") else c
            o = objs.get(cc.path_id)
            if o is not None and o.type.name == kind:
                return o
        return None

    def walk(pid, parent):
        t = tfs[pid]; go = t.m_GameObject.read()
        n = {"name": go.m_Name, "parent": parent, "active": bool(go.m_IsActive),
             "p": q(t.m_LocalPosition), "r": q(t.m_LocalRotation), "s": q(t.m_LocalScale),
             "children": [c.path_id for c in t.m_Children]}
        mr = comp_of(go, "MeshRenderer")
        if mr is not None:
            r = mr.read()
            mats = [m for m in r.m_Materials if m.path_id]
            if mats:
                mt = mats[0].deref().read_typetree()
                sp = mt["m_SavedProperties"]
                col = dict(sp["m_Colors"]).get("_BaseColor", {"r": 1, "g": 1, "b": 1, "a": 1})
                n["mat"] = {"name": mt["m_Name"], "base_color": [col["r"], col["g"], col["b"], col["a"]],
                            "transparent": dict(sp["m_Floats"]).get("_Surface", 0) == 1,
                            "queue": mt.get("m_CustomRenderQueue", -1)}
            n["renderer"] = mr.path_id
            n["renderer_enabled"] = bool(r.m_Enabled)
        nodes[str(pid)] = n
        for c in t.m_Children:
            walk(c.path_id, pid)

    face_root = find(FACE_ROOT)
    head_root = find(HEAD_ROOT, under="char_avatar_Male")
    walk(face_root, None)
    walk(head_root, None)
    cam = find("Camera_" + FACE_ROOT)
    camn = tfs[cam]
    cam_o = comp_of(camn.m_GameObject.read(), "Camera").read()
    # map renderer path id -> transform pid, so controls can point at either
    r2t = {n["renderer"]: int(k) for k, n in nodes.items() if "renderer" in n}
    for c in controls:
        c["objs"] = [r2t.get(o, o) for o in c["objs"]]
        c["counter"] = [r2t.get(o, o) for o in c["counter"]]
    keep = [c for c in controls if any(str(o) in nodes for o in c["objs"])]
    for c in keep:
        c["objs"] = [o for o in c["objs"] if str(o) in nodes]

    # ---- option -> asset objects (AvatarResources, like build_avatar_catalog)
    f_res, o_res = G.find_mono("AvatarResources", ["level0"])
    res = o_res.read_typetree()
    ext = externals(f_res, G)

    def resolve(ptr):
        if not ptr["m_PathID"]:
            return None
        fname = ext[ptr["m_FileID"] - 1] if ptr["m_FileID"] > 0 else f_res
        return G.env(fname)[1].get(ptr["m_PathID"])

    want = set(MESH_CTLS) | set(TEX_CTLS)
    assets = {}
    for r in res["m_aAssetResources"]:
        cname = r["m_sAvatarControlName"]
        if cname not in want:
            continue
        items = list(r["m_aoResources"])
        for dep in r["m_dependantResources"]:
            items += dep["m_aoResources"]
        for it in items:
            o = resolve(it["m_oResource"])
            if o is None:
                continue
            nm = o.peek_name() if hasattr(o, "peek_name") else o.read().m_Name
            assets.setdefault(cname, {}).setdefault(nm, o)

    index = {"meshes": {}, "textures": {}}
    arrays = {}
    for cname in MESH_CTLS:
        for nm, o in sorted(assets.get(cname, {}).items()):
            try:
                V, N, UV, UV2, T = mesh_arrays(o)
            except Exception as ex:          # e.g. an empty placeholder mesh
                log("mesh skipped", cname, nm, ex); continue
            if len(T) == 0:
                continue
            arrays[nm + "|V"] = V.astype(np.float32)
            arrays[nm + "|N"] = N.astype(np.float16)
            arrays[nm + "|UV"] = UV.astype(np.float32)
            if UV2 is not None and len(UV2) == len(V) and np.ptp(UV2[:, 1]) > 0:
                arrays[nm + "|UV2"] = UV2.astype(np.float32)   # hair shader samples _BaseMap with UV2
            arrays[nm + "|T"] = T.astype(np.uint16 if len(V) < 65536 else np.uint32)
            index["meshes"].setdefault(cname, []).append(nm)
        log(cname, len(index["meshes"].get(cname, [])), "meshes")
    np.savez_compressed(os.path.join(out_dir, "meshes.npz"), **arrays)
    tmpzip = os.path.join(out_dir, "textures.zip.tmp")
    with zipfile.ZipFile(tmpzip, "w", zipfile.ZIP_STORED) as zf:
        for cname, mx in TEX_CTLS.items():
            if cname not in ("eyesBrightTexture", "eyeBlink"):
                mx = min(mx, tex_max)
            for nm, o in sorted(assets.get(cname, {}).items()):
                try:
                    im = o.read().image.convert("RGBA")
                except Exception as ex:
                    log("tex fail", cname, nm, ex); continue
                if max(im.size) > mx:
                    im.thumbnail((mx, mx), Image.LANCZOS)
                buf = io.BytesIO(); im.save(buf, "PNG", optimize=True)
                zf.writestr("%s/%s.png" % (cname, safe(nm)), buf.getvalue())
                index["textures"].setdefault(cname, {})[nm] = "%s/%s.png" % (cname, safe(nm))
            log(cname, len(index["textures"].get(cname, {})), "textures")
    os.replace(tmpzip, os.path.join(out_dir, "textures.zip"))

    scene = {"source": "level0 SuperAvatar + avatarHeadMaster / char_avatar_Male headScale",
             "face_root": face_root, "head_root": head_root, "nodes": nodes,
             "camera": {"p": q(camn.m_LocalPosition), "ortho_size": float(cam_o.orthographic_size)},
             "controls": keep, "index": index}
    with open(os.path.join(out_dir, "scene.json"), "w", encoding="utf-8") as fh:
        json.dump(scene, fh, indent=0)
    log("controls kept:", len(keep), "nodes:", len(nodes))
    return scene


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("game"); ap.add_argument("out", nargs="?")
    ap.add_argument("--tex", type=int, default=256)
    a = ap.parse_args()
    out = a.out or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "avatar_catalog", "render_cache")
    build(a.game, out, a.tex)
