"""
camera_manager.py -- byte work for the Camera tab. No tkinter in here.

Self-contained like slider_manager.py: imports nothing from app.py and never
calls apply_single_mod().

How NBA Bounce's match camera works (bounce.GameplayCamera, Assembly-CSharp)
---------------------------------------------------------------------------
One GameplayCamera component in level2 ('GameplayCamera_arcade') drives the
in-match camera. It holds six authored CameraPoint objects in a 3x2 grid:

        TL  TC  TR      "far" row   -- used while the ball is on the far side
        BL  BC  BR      "near" row  -- used while the ball is on the near side

Each point is a Transform (position + rotation) plus an m_fFOV. Every frame
the game smooths the ball position (with possession and loose-ball lead),
remaps it through a curve against the court zones, and bilinearly blends
position, rotation and FOV between the four surrounding points, then adds
m_fAddedFov. If the ball rises high in the frame, the camera is lifted by
the overshoot x fHeightMultiplier.

So a camera angle is just new values in 18 point slots plus ten tuning floats
and the render camera's far clip. Every one of them is a fixed-size float, so
all writes are in place: the file size and every other object stay exactly as
they were, and other mods in level2 (sliders, team-select grid, textures) are
not touched. Offsets are resolved from the current file each time, never
hard-coded, and the GameplayCamera layout is checked to the byte before
anything is written.

The built-in "Default" preset holds the game's original values at full
float32 precision, so reverting to it is bit-identical to a clean install and
never needs a backup file.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import struct
from datetime import datetime, timezone

PRESET_FORMAT = "nba-bounce-camera"
PRESET_VERSION = 1
PRESET_EXT = ".bouncecam"
MAX_PRESET_BYTES = 64 * 1024

SCENE_FILE = "level2"
BACKUP_SUFFIX = ".camera_backup"
RENDER_CAMERA = "camera3dBounce"

POINT_KEYS = ["TL", "TC", "TR", "BL", "BC", "BR"]
DEFAULT_FAR_CLIP = 100.0
FAR_CLIP_RANGE = (10.0, 2000.0)
POS_LIMIT = 200.0
FOV_RANGE = (1.0, 120.0)

# Tuning floats inside GameplayCamera, in serialized order:
#   key, serialized field, min, max
TUNING_FIELDS = [
    ("follow_smooth",       "SmoothedTargetXZ.smoothTime",          0.0,  3.0),
    ("possession_lead",     "m_fPredictedModifierX",              -10.0, 10.0),
    ("possession_smooth",   "SmoothedPredictedModifier.smoothTime", 0.0,  5.0),
    ("velocity_lead",       "m_fVelocityPredictionMultiplier",      0.0,  3.0),
    ("velocity_smooth",     "SmoothedBallVelocity.smoothTime",      0.0,  5.0),
    ("added_fov",           "m_fAddedFov",                        -10.0, 40.0),
    ("max_viewport_height", "m_fMaxViewportHeight",                 0.0,  3.0),
    ("height_smooth",       "SmoothedTargetHeight.smoothTime",      0.0,  3.0),
    ("deadzone_y",          "fScreenDeadZoneY",                     0.0,  1.0),
    ("height_follow",       "fHeightMultiplier",                    0.0, 40.0),
]
TUNING_RANGES = {k: (lo, hi) for k, _, lo, hi in TUNING_FIELDS}

# Court geometry in level2 (CourtCorner objects): a real NBA-size floor.
# X runs baseline to baseline, Z sideline to sideline (negative Z = the side
# the default camera sits on).
COURT_HALF_X = 14.32
COURT_HALF_Z = 7.62
HOOP_X = 12.45                  # rim centre ("hoop" objects in level2)
RIM_HEIGHT = 3.05

# Rig = the slider-friendly description the six points are generated from.
# Two styles:
#   "sideline" (default) - the game's own layout: cameras along the near
#                sideline, sliding toward whichever basket the ball is near.
#   "end"      - "behind the play" (NBA 2K style): when the ball is near a
#                basket the camera sits back toward half court and looks down
#                the floor at that basket; around mid-court it swings out to a
#                sideline view. The game blends between the two on its own.
RIG_STYLES = ("sideline", "end", "iso")
SIDELINE_KEYS = ("near_distance", "far_distance", "near_height", "far_height",
                 "near_pitch", "far_pitch", "pan_range", "fov")
ISO_KEYS = ("iso_yaw", "iso_pitch", "iso_distance", "iso_fov", "iso_pan", "iso_depth")
END_KEYS = ("end_back", "end_height", "end_pitch", "end_side", "end_follow",
            "end_fov", "mid_distance", "mid_height", "mid_pitch", "mid_fov")
RIG_RANGES = {
    # fixed-angle isometric style (classic NBA Live)
    "iso_yaw":       (-80.0, 80.0),   # + looks toward the right basket, - the left
    "iso_pitch":     (5.0, 85.0),
    "iso_distance":  (5.0, 150.0),    # camera distance from the point it looks at
    "iso_fov":       (3.0, 80.0),
    "iso_pan":       (0.0, 16.0),     # how far the view slides toward each basket
    "iso_depth":     (0.0, 7.0),      # how far it slides toward the near/far side
    # behind-the-play style
    "end_back":      (3.0, 40.0),     # camera distance back from the rim, along the court
    "end_height":    (1.0, 40.0),
    "end_pitch":     (0.0, 80.0),
    "end_side":      (-7.0, 7.0),     # sideways offset toward the near sideline
    "end_follow":    (0.0, 7.0),      # extra sideways shift toward the ball's side
    "end_fov":       (5.0, 80.0),
    "mid_distance":  (4.0, 60.0),     # sideline camera used around mid-court
    "mid_height":    (1.0, 40.0),
    "mid_pitch":     (0.0, 80.0),
    "mid_fov":       (5.0, 80.0),
    # sideline style
    "near_distance": (4.0, 90.0),
    "far_distance":  (4.0, 90.0),
    "near_height":   (0.5, 70.0),
    "far_height":    (0.5, 70.0),
    "near_pitch":    (-10.0, 89.0),
    "far_pitch":     (-10.0, 89.0),
    "pan_range":     (0.0, 20.0),
    "toe_in":        (0.0, 30.0),
    "fov":           (2.0, 60.0),
    "far_fov":       (2.0, 60.0),
}

DEFAULT_TUNING = {
    "follow_smooth": 0.25, "possession_lead": 1.0, "possession_smooth": 1.0,
    "velocity_lead": 0.2, "velocity_smooth": 2.0, "added_fov": 3.0,
    "max_viewport_height": 1.2, "height_smooth": 0.4, "deadzone_y": 0.7,
    "height_follow": 14.0,
}


class CameraError(RuntimeError):
    pass


class PresetError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Serialized layout
# ---------------------------------------------------------------------------
class _Reader:
    def __init__(self, data: bytes):
        self.b, self.p = data, 0

    def i32(self):
        v = struct.unpack_from("<i", self.b, self.p)[0]; self.p += 4; return v

    def i64(self):
        v = struct.unpack_from("<q", self.b, self.p)[0]; self.p += 8; return v

    def f32(self):
        v = struct.unpack_from("<f", self.b, self.p)[0]; self.p += 4; return v

    def skip(self, n):
        if self.p + n > len(self.b):
            raise ValueError("past end")
        self.p += n

    def align(self):
        self.p = (self.p + 3) & ~3

    def pptr(self):
        return self.i32(), self.i64()

    def string(self):
        n = self.i32()
        if n < 0 or n > 4096:
            raise ValueError("bad string")
        self.skip(n)
        self.align()


def parse_gameplay_camera(raw: bytes):
    """Parse the GameplayCamera MonoBehaviour -> (refs, field offsets).

    Raises ValueError unless the layout consumes exactly every byte, which is
    how a game update that changes the class is caught before any write.
    """
    r = _Reader(raw)
    r.pptr(); r.skip(1); r.align(); r.pptr(); r.string()   # MonoBehaviour header
    r.string()                                             # m_sLocalizationTerm
    n = r.i32()                                            # m_aoNonGameplayRenderObjects
    if n < 0 or n > 256:
        raise ValueError("bad array")
    r.skip(12 * n)
    refs = {}
    for k in POINT_KEYS:
        refs[k + "point"] = r.pptr()
    for k in POINT_KEYS:
        refs[k + "zone"] = r.pptr()
    offs = {}

    def f(name):
        offs[name] = r.p
        r.f32()

    r.skip(8)                                   # SmoothedTargetXZ.current (runtime)
    f("SmoothedTargetXZ.smoothTime")
    f("m_fPredictedModifierX")
    r.skip(4)                                   # SmoothedPredictedModifier.current
    f("SmoothedPredictedModifier.smoothTime")
    f("m_fVelocityPredictionMultiplier")
    r.skip(4)                                   # SmoothedBallVelocity.current
    f("SmoothedBallVelocity.smoothTime")
    n = r.i32()                                 # m_oRealToMarginSmoothed keyframes
    if n < 0 or n > 64:
        raise ValueError("bad curve")
    r.skip(28 * n + 12)
    r.skip(12)                                  # m_vPlayerArrowOffset
    f("m_fAddedFov")
    f("m_fMaxViewportHeight")
    r.skip(4)                                   # SmoothedTargetHeight.current
    f("SmoothedTargetHeight.smoothTime")
    f("fScreenDeadZoneY")
    f("fHeightMultiplier")
    if r.p != len(raw):
        raise ValueError(f"layout mismatch ({r.p} of {len(raw)} bytes)")
    for k, (fid, pid) in refs.items():
        if fid != 0 or pid == 0:
            raise ValueError(f"{k} is not a local reference")
    return {k: pid for k, (_, pid) in refs.items()}, offs


# Transform: m_GameObject PPtr (12) | m_LocalRotation (16) | m_LocalPosition (12)
_TR_ROT, _TR_POS = 12, 28


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------
def euler_to_quat(pitch_deg: float, yaw_deg: float = 0.0):
    """Unity Quaternion.Euler(pitch, yaw, 0) -> [x, y, z, w]."""
    p, y = math.radians(pitch_deg) / 2, math.radians(yaw_deg) / 2
    cx, sx, cy, sy = math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return [cy * sx, sy * cx, -sy * sx, cy * cx]


def quat_to_euler(q):
    """[x, y, z, w] -> (pitch, yaw) in degrees (roll ignored)."""
    x, y, z, w = q
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * x - y * z)))))
    yaw = math.degrees(math.atan2(2 * (w * y + x * z), 1 - 2 * (x * x + y * y)))
    return pitch, yaw


def _point(x, y, z, pitch, yaw, fov):
    return {"pos": [round(x, 4), round(y, 4), round(z, 4)],
            "rot": [round(c, 6) for c in euler_to_quat(pitch, yaw)],
            "fov": round(fov, 4)}


def _build_end_points(rig: dict) -> dict:
    """Behind-the-play layout.

    L/R columns are used while the ball is near the left/right basket: the
    camera sits end_back metres from that rim toward half court and looks
    straight down the floor at it (yaw -90 / +90). The near/far rows shift it
    sideways toward the ball's side by end_follow. The C column is a sideline
    camera for mid-court, so the game's own blending swings the camera round
    as the ball crosses half court.
    """
    back, h, p = float(rig["end_back"]), float(rig["end_height"]), float(rig["end_pitch"])
    side, follow = float(rig.get("end_side", 0.0)), float(rig.get("end_follow", 0.0))
    fov, mfov = float(rig["end_fov"]), float(rig["mid_fov"])
    md, mh, mp = float(rig["mid_distance"]), float(rig["mid_height"]), float(rig["mid_pitch"])
    pts = {}
    for row, z in (("B", -(side + follow)), ("T", -(side - follow))):
        pts[row + "L"] = _point(-HOOP_X + back, h, z, p, -90.0, fov)
        pts[row + "C"] = _point(0.0, mh, -md, mp, 0.0, mfov)
        pts[row + "R"] = _point(HOOP_X - back, h, z, p, 90.0, fov)
    return {k: pts[k] for k in POINT_KEYS}


def rig_style(rig) -> str:
    st = rig.get("style") if isinstance(rig, dict) else None
    return st if st in ("end", "iso") else "sideline"


def _build_iso_points(rig: dict) -> dict:
    """Fixed-angle isometric layout (classic NBA Live).

    All six points share ONE rotation, so the camera never turns -- not on a
    change of possession, not anywhere on the floor. Each point is placed so
    it looks at a spot on the floor: +/-iso_pan toward the baskets and
    +/-iso_depth toward the far/near side. The game's blending therefore just
    slides the view around with the ball, like a 16-bit isometric camera.
    """
    yaw, pitch = float(rig["iso_yaw"]), float(rig["iso_pitch"])
    dist, fov = float(rig["iso_distance"]), float(rig["iso_fov"])
    pan, depth = float(rig["iso_pan"]), float(rig.get("iso_depth", 0.0))
    fx = math.sin(math.radians(yaw)) * math.cos(math.radians(pitch))
    fy = -math.sin(math.radians(pitch))
    fz = math.cos(math.radians(yaw)) * math.cos(math.radians(pitch))
    pts = {}
    for row, lz in (("B", -depth), ("T", depth)):
        for col, sign in (("L", -1), ("C", 0), ("R", 1)):
            lx = sign * pan
            pts[row + col] = _point(lx - dist * fx, -dist * fy, lz - dist * fz,
                                    pitch, yaw, fov)
    return {k: pts[k] for k in POINT_KEYS}


def build_points_from_rig(rig: dict) -> dict:
    """Six CameraPoints from the rig values (see RIG_RANGES)."""
    if rig_style(rig) == "end":
        return _build_end_points(rig)
    if rig_style(rig) == "iso":
        return _build_iso_points(rig)
    pts = {}
    for row, pre in (("B", "near"), ("T", "far")):
        z = -float(rig[f"{pre}_distance"])
        y = float(rig[f"{pre}_height"])
        pitch = float(rig[f"{pre}_pitch"])
        fov = float(rig.get("far_fov", rig["fov"])) if row == "T" else float(rig["fov"])
        for col, sign in (("L", -1), ("C", 0), ("R", 1)):
            pts[row + col] = {
                "pos": [round(sign * float(rig["pan_range"]), 4), round(y, 4), round(z, 4)],
                "rot": [round(c, 6) for c in
                        euler_to_quat(pitch, -sign * float(rig.get("toe_in", 0.0)))],
                "fov": round(fov, 4),
            }
    return {k: pts[k] for k in POINT_KEYS}


def rig_from_points(points: dict) -> dict:
    """Best-fit rig for any six points (used for presets saved without one)."""
    def r1(v):
        return round(v, 2)
    br, tr, bc = points["BR"], points["TR"], points["BC"]
    rots = [points[k]["rot"] for k in POINT_KEYS]
    same_rot = all(max(abs(a - b) for a, b in zip(r, rots[0])) < 1e-4 for r in rots)
    pitch0, yaw0 = quat_to_euler(rots[0])
    if same_rot and abs(yaw0) >= 3.0 and pitch0 > 0.5:     # one fixed angle: isometric
        f = (math.sin(math.radians(yaw0)) * math.cos(math.radians(pitch0)),
             -math.sin(math.radians(pitch0)),
             math.cos(math.radians(yaw0)) * math.cos(math.radians(pitch0)))
        dist = bc["pos"][1] / -f[1]
        look = {k: (points[k]["pos"][0] + dist * f[0], points[k]["pos"][2] + dist * f[2])
                for k in POINT_KEYS}
        rig = {"style": "iso", "iso_yaw": r1(yaw0), "iso_pitch": r1(pitch0),
               "iso_distance": r1(dist), "iso_fov": r1(bc["fov"]),
               "iso_pan": r1((look["BR"][0] - look["BL"][0]) / 2),
               "iso_depth": r1((look["TC"][1] - look["BC"][1]) / 2)}
        return {k: (v if k == "style" else min(max(v, RIG_RANGES[k][0]), RIG_RANGES[k][1]))
                for k, v in rig.items()}
    if abs(quat_to_euler(br["rot"])[1]) > 45:           # cameras face the baskets
        rig = {"style": "end",
               "end_back": r1(HOOP_X - br["pos"][0]), "end_height": r1(br["pos"][1]),
               "end_pitch": r1(quat_to_euler(br["rot"])[0]),
               "end_side": r1(-(br["pos"][2] + tr["pos"][2]) / 2),
               "end_follow": r1(abs(tr["pos"][2] - br["pos"][2]) / 2),
               "end_fov": r1(br["fov"]),
               "mid_distance": r1(-bc["pos"][2]), "mid_height": r1(bc["pos"][1]),
               "mid_pitch": r1(quat_to_euler(bc["rot"])[0]), "mid_fov": r1(bc["fov"])}
        return {k: (v if k == "style" else min(max(v, RIG_RANGES[k][0]), RIG_RANGES[k][1]))
                for k, v in rig.items()}
    tp, tc = quat_to_euler(points["TC"]["rot"])[0], points["TC"]
    bp, bc = quat_to_euler(points["BC"]["rot"])[0], points["BC"]
    pan = (abs(points["TR"]["pos"][0]) + abs(points["TL"]["pos"][0]) +
           abs(points["BR"]["pos"][0]) + abs(points["BL"]["pos"][0])) / 4
    toe = (abs(quat_to_euler(points["BR"]["rot"])[1]) +
           abs(quat_to_euler(points["BL"]["rot"])[1])) / 2
    rig = {
        "near_distance": r1(-bc["pos"][2]), "far_distance": r1(-tc["pos"][2]),
        "near_height": r1(bc["pos"][1]), "far_height": r1(tc["pos"][1]),
        "near_pitch": r1(bp), "far_pitch": r1(tp),
        "pan_range": r1(pan), "toe_in": r1(toe), "fov": r1(bc["fov"]),
    }
    if abs(tc["fov"] - bc["fov"]) > 1e-3:
        rig["far_fov"] = r1(tc["fov"])
    return {k: min(max(v, RIG_RANGES[k][0]), RIG_RANGES[k][1]) for k, v in rig.items()}


# ---------------------------------------------------------------------------
# Wireframe "what the camera sees" (for the preview, no game needed)
# ---------------------------------------------------------------------------
def court_polylines():
    """NBA court lines, both hoops and backboards, in level2 world space."""
    hx, hz = COURT_HALF_X, COURT_HALF_Z
    arc = lambda cx, cz, r, a0, a1, y=0.0, n=32: [
        (cx + r * math.cos(a0 + (a1 - a0) * i / n), y,
         cz + r * math.sin(a0 + (a1 - a0) * i / n)) for i in range(n + 1)]
    lines = [[(-hx, 0, -hz), (hx, 0, -hz), (hx, 0, hz), (-hx, 0, hz), (-hx, 0, -hz)],
             [(0, 0, -hz), (0, 0, hz)],
             arc(0, 0, 1.83, 0, 2 * math.pi)]
    for s in (-1, 1):
        base, ft, hoop = s * hx, s * (hx - 5.79), s * HOOP_X
        lines.append([(base, 0, -2.44), (ft, 0, -2.44), (ft, 0, 2.44), (base, 0, 2.44)])
        # free-throw circle: the half facing mid-court
        a = math.pi / 2
        lines.append(arc(ft, 0, 1.83, a, a + math.pi) if s > 0 else arc(ft, 0, 1.83, -a, a))
        # three-point line: corner straights + 7.24 m arc round the rim
        zc = 6.71
        t = math.asin(zc / 7.24)
        a0, a1 = (math.pi - t, math.pi + t) if s > 0 else (-t, t)
        three = [(base, 0, -zc)] + arc(hoop, 0, 7.24, a0, a1)[::1] + [(base, 0, zc)]
        if s > 0:
            three = [(base, 0, zc)] + arc(hoop, 0, 7.24, a0, a1) + [(base, 0, -zc)]
        lines.append(three)
        bb = s * (hx - 1.22)
        lines.append([(bb, 2.9, -0.915), (bb, 2.9, 0.915), (bb, 3.97, 0.915),
                      (bb, 3.97, -0.915), (bb, 2.9, -0.915)])
        lines.append(arc(hoop, 0, 0.23, 0, 2 * math.pi, y=RIM_HEIGHT, n=16))
        lines.append([(bb, 3.05, 0), (s * (HOOP_X - 0.23), 3.05, 0)])
    return lines


def _to_camera(p, pos, rot):
    """World point -> camera space (x right, y up, z forward), Unity convention."""
    x, y, z, w = -rot[0], -rot[1], -rot[2], rot[3]          # conjugate
    vx, vy, vz = p[0] - pos[0], p[1] - pos[1], p[2] - pos[2]
    ix = w * vx + y * vz - z * vy
    iy = w * vy + z * vx - x * vz
    iz = w * vz + x * vy - y * vx
    iw = -x * vx - y * vy - z * vz
    return (ix * w - iw * x - iy * z + iz * y,
            iy * w - iw * y - iz * x + ix * z,
            iz * w - iw * z - ix * y + iy * x)


def court_view_segments(point: dict, added_fov: float, width: int, height: int,
                        near: float = 0.3):
    """2-D line segments of the court as seen from one camera point.

    Returns [(x0, y0, x1, y1), ...] in pixels for a width x height view.
    Uses the same vertical FOV the game does (point FOV + added FOV).
    """
    vf = math.radians(max(1.0, min(170.0, point["fov"] + added_fov)))
    f = (height / 2) / math.tan(vf / 2)
    out = []
    for poly in court_polylines():
        cs = [_to_camera(p, point["pos"], point["rot"]) for p in poly]
        for a, b in zip(cs, cs[1:]):
            if a[2] < near and b[2] < near:
                continue
            if a[2] < near or b[2] < near:
                t = (near - a[2]) / (b[2] - a[2])
                m = tuple(a[i] + (b[i] - a[i]) * t for i in range(3))
                a, b = (m, b) if a[2] < near else (a, m)
            out.append((width / 2 + f * a[0] / a[2], height / 2 - f * a[1] / a[2],
                        width / 2 + f * b[0] / b[2], height / 2 - f * b[1] / b[2]))
    return out


# ---------------------------------------------------------------------------
# Built-in presets
# ---------------------------------------------------------------------------
def _preset(name, desc, rig=None, points=None, tuning=None, far_clip=DEFAULT_FAR_CLIP):
    t = dict(DEFAULT_TUNING)
    t.update(tuning or {})
    p = {"format": PRESET_FORMAT, "version": PRESET_VERSION, "name": name,
         "author": "NBA Bounce Mod Manager", "description": desc,
         "tuning": t, "far_clip": far_clip}
    if rig:
        p["rig"] = dict(rig)
        p["points"] = build_points_from_rig(rig)
    if points:
        p["points"] = copy.deepcopy(points)
    return p


# The game's own values (build 0.1.4334) at full float32 precision, so
# applying Default is byte-for-byte identical to an unmodded level2.
_Q14 = [0.1218692809343338, 0.0, 0.0, 0.9925462007522583]    # 14 deg down
_DEFAULT_POINTS = {
    "TL": {"pos": [-8.6, 10.2, -36.0], "rot": _Q14, "fov": 11.3},
    "TC": {"pos": [0.0, 10.2, -36.0], "rot": _Q14, "fov": 11.3},
    "TR": {"pos": [8.6, 10.2, -36.0], "rot": _Q14, "fov": 11.3},
    "BL": {"pos": [-8.6, 9.5, -36.0], "rot": _Q14, "fov": 11.3},
    "BC": {"pos": [0.27, 9.5, -36.0], "rot": _Q14, "fov": 11.3},
    "BR": {"pos": [8.6, 9.5, -36.0], "rot": _Q14, "fov": 11.3},
}

DEFAULT_KEY = "default"
BUILTIN_PRESETS = {
    "default": _preset(
        "Default", "The original NBA Bounce camera: a long-lens shot from far "
                   "up the sideline.",
        points=_DEFAULT_POINTS),
    "broadcast": _preset(
        "Broadcast", "High sideline TV angle. Farther back and higher for a wider "
                     "view, with slow, steady panning.",
        rig={"near_distance": 44, "far_distance": 44, "near_height": 17.5,
             "far_height": 18.5, "near_pitch": 21, "far_pitch": 21,
             "pan_range": 5.5, "toe_in": 2.0, "fov": 12.5},
        tuning={"follow_smooth": 0.45, "possession_lead": 1.5,
                "velocity_lead": 0.1, "height_smooth": 0.6, "height_follow": 8.0},
        far_clip=150.0),
    "2k_drive": _preset(
        "2K Drive", "Low, tight camera that moves in and out with the play and "
                    "tracks the ball handler closely.",
        rig={"near_distance": 25, "far_distance": 22, "near_height": 5.2,
             "far_height": 5.8, "near_pitch": 13.5, "far_pitch": 12.5,
             "pan_range": 9.0, "toe_in": 0.0, "fov": 13.5},
        tuning={"follow_smooth": 0.15, "possession_lead": 1.5,
                "velocity_lead": 0.35, "velocity_smooth": 1.0,
                "height_smooth": 0.3, "deadzone_y": 0.65, "height_follow": 10.0}),
    "2k_halfcourt": _preset(
        "2K Half-Court", "NBA 2K-style camera from behind the play. Near a basket "
                         "it sits back near half court and looks down the "
                         "floor at the rim. Around mid-court it swings out to a "
                         "sideline view.",
        # end_* values come from fitting nine court landmarks in an NBA 2K
        # screenshot (backboard corners, rim, lane corners at the baseline and
        # free-throw line, free-throw circle and 3-pt apex; 4 px RMS): camera
        # 14.6 m back from the rim (just short of half court), 6.9 m up,
        # 25 deg down, 42.5 deg vertical FOV (39.5 + the game's 3 deg added
        # FOV), 0.4 m toward the far sideline.
        rig={"style": "end", "end_back": 14.6, "end_height": 6.9,
             "end_pitch": 25.0, "end_side": -0.4, "end_follow": 0.75,
             "end_fov": 39.5, "mid_distance": 18.0, "mid_height": 8.0,
             "mid_pitch": 24.0, "mid_fov": 34.0},
        tuning={"follow_smooth": 0.3, "possession_lead": 2.5,
                "possession_smooth": 0.8, "velocity_lead": 0.25,
                "height_follow": 6.0, "deadzone_y": 0.8, "height_smooth": 0.5}),
    "live_iso": _preset(
        "Classic Live Iso", "Classic NBA Live isometric look: a fixed diagonal "
                            "view from the corner. It slides with the ball but "
                            "never turns, not even on a change of possession.",
        # Tuned in game by Michael (diagonal 44.9, tilt 28.8, distance 49.7,
        # FOV 8.7 + 3): one rotation for all six points and a long lens, so
        # the court reads as near-isometric with the ball-side basket in the
        # top-right corner.
        rig={"style": "iso", "iso_yaw": 44.9, "iso_pitch": 28.8,
             "iso_distance": 49.7, "iso_fov": 8.7, "iso_pan": 10.5,
             "iso_depth": 2.5},
        tuning={"follow_smooth": 0.35, "possession_lead": 1.5,
                "possession_smooth": 1.0, "velocity_lead": 0.2,
                "height_follow": 4.0, "deadzone_y": 0.85, "height_smooth": 0.5},
        far_clip=150.0),
    "live_iso_pc": _preset(
        "Classic Live Iso (PC)", "The mirrored diagonal from the PC version: "
                                 "lower and closer, looking the other way across "
                                 "the floor. It never turns either.",
        rig={"style": "iso", "iso_yaw": -35.0, "iso_pitch": 38.0,
             "iso_distance": 28.0, "iso_fov": 17.0, "iso_pan": 8.5,
             "iso_depth": 2.5},
        tuning={"follow_smooth": 0.35, "possession_lead": 1.5,
                "possession_smooth": 1.0, "velocity_lead": 0.2,
                "height_follow": 4.0, "deadzone_y": 0.85, "height_smooth": 0.5},
        far_clip=150.0),
    "zoom": _preset(
        "Zoom", "The default angle with a tighter lens that pans the full court.",
        rig={"near_distance": 36, "far_distance": 36, "near_height": 9.5,
             "far_height": 10.2, "near_pitch": 14.0, "far_pitch": 14.0,
             "pan_range": 9.0, "fov": 8.5},
        tuning={"follow_smooth": 0.2}),
    "close_up": _preset(
        "Close-Up", "Very close, low and tight. The most dramatic view, but you "
                    "see the least of the court.",
        rig={"near_distance": 19, "far_distance": 17, "near_height": 4.0,
             "far_height": 4.6, "near_pitch": 12.0, "far_pitch": 11.0,
             "pan_range": 9.5, "fov": 13.0},
        tuning={"follow_smooth": 0.12, "velocity_lead": 0.4,
                "height_smooth": 0.25, "deadzone_y": 0.6, "height_follow": 8.0}),
    "overhead": _preset(
        "Overhead", "High and steep, looking down on the whole court.",
        rig={"near_distance": 14, "far_distance": 12, "near_height": 34,
             "far_height": 34, "near_pitch": 66, "far_pitch": 70,
             "pan_range": 6.0, "fov": 16.0},
        tuning={"follow_smooth": 0.35, "velocity_lead": 0.1,
                "deadzone_y": 0.85, "height_follow": 3.0},
        far_clip=150.0),
}


def builtin(key: str) -> dict:
    return validate_preset(copy.deepcopy(BUILTIN_PRESETS[key]))


def default_preset() -> dict:
    return builtin(DEFAULT_KEY)


# ---------------------------------------------------------------------------
# Validation + preset files
# ---------------------------------------------------------------------------
def _num(v, lo, hi, what):
    if isinstance(v, bool):
        raise PresetError(f"{what}: not a number")
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise PresetError(f"{what}: not a number")
    if math.isnan(v) or math.isinf(v):
        raise PresetError(f"{what}: invalid number")
    if not lo <= v <= hi:
        raise PresetError(f"{what}: {v:g} is outside {lo:g} to {hi:g}")
    return v


def validate_preset(p) -> dict:
    """Check and normalise a preset (from a built-in, the UI or a friend's file).

    A validated preset is always complete: six points, all ten tuning values
    and a far clip. Anything missing falls back to the game default, so
    switching presets never leaves a previous preset's value behind.
    """
    if not isinstance(p, dict) or p.get("format") != PRESET_FORMAT:
        raise PresetError("This is not an NBA Bounce camera preset.")
    try:
        ver = int(p.get("version", 0))
    except (TypeError, ValueError):
        raise PresetError("The preset's version number is unreadable.")
    if ver > PRESET_VERSION:
        raise PresetError("This preset was made with a newer version of the mod "
                          "manager. Update the mod manager to use it.")
    out = {"format": PRESET_FORMAT, "version": PRESET_VERSION,
           "name": str(p.get("name") or "Untitled").strip()[:60] or "Untitled",
           "author": str(p.get("author") or "").strip()[:60],
           "description": str(p.get("description") or "").strip()[:400]}

    rig = p.get("rig")
    if rig is not None:
        if not isinstance(rig, dict):
            raise PresetError("rig must be an object")
        style = rig.get("style", "sideline")
        if style not in RIG_STYLES:
            raise PresetError(f"rig.style must be one of {', '.join(RIG_STYLES)}")
        keys = {"end": END_KEYS, "iso": ISO_KEYS}.get(style, SIDELINE_KEYS + ("far_fov", "toe_in"))
        clean = {"style": style} if style != "sideline" else {}
        for k in keys:
            if k in rig:
                clean[k] = _num(rig[k], *RIG_RANGES[k], f"rig.{k}")
        required = [k for k in {"end": END_KEYS, "iso": ISO_KEYS}.get(style, SIDELINE_KEYS)
                    if k not in ("end_side", "end_follow", "iso_depth")]
        missing = [k for k in required if k not in clean]
        if missing:
            raise PresetError("rig is missing " + ", ".join(missing))
        rig = clean
        out["rig"] = rig

    pts = p.get("points")
    if pts is None and rig is not None:
        pts = build_points_from_rig(rig)
    if not isinstance(pts, dict) or set(pts) != set(POINT_KEYS):
        raise PresetError("A preset must define all six camera points.")
    out["points"] = {}
    for k in POINT_KEYS:
        pt = pts[k]
        if not isinstance(pt, dict):
            raise PresetError(f"{k}: bad point")
        pos, rot = pt.get("pos"), pt.get("rot")
        if not isinstance(pos, (list, tuple)) or len(pos) != 3:
            raise PresetError(f"{k}.pos needs 3 numbers")
        if not isinstance(rot, (list, tuple)) or len(rot) != 4:
            raise PresetError(f"{k}.rot needs 4 numbers")
        pos = [_num(c, -POS_LIMIT, POS_LIMIT, f"{k}.pos") for c in pos]
        rot = [_num(c, -1.0001, 1.0001, f"{k}.rot") for c in rot]
        norm = math.sqrt(sum(c * c for c in rot))
        if norm < 0.5:
            raise PresetError(f"{k}.rot is not a valid rotation")
        if abs(norm - 1.0) > 1e-2:             # the game's own values stay bit-exact
            rot = [c / norm for c in rot]
        out["points"][k] = {"pos": pos, "rot": rot,
                            "fov": _num(pt.get("fov"), *FOV_RANGE, f"{k}.fov")}

    tuning = dict(DEFAULT_TUNING)
    src = p.get("tuning") or {}
    if not isinstance(src, dict):
        raise PresetError("tuning must be an object")
    for k, v in src.items():
        if k in TUNING_RANGES:
            tuning[k] = _num(v, *TUNING_RANGES[k], k)
    out["tuning"] = tuning
    out["far_clip"] = _num(p.get("far_clip", DEFAULT_FAR_CLIP), *FAR_CLIP_RANGE, "far_clip")
    return out


def load_preset_file(path: str) -> dict:
    if os.path.getsize(path) > MAX_PRESET_BYTES:
        raise PresetError("That file is too large to be a camera preset.")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PresetError(f"The file is not valid preset JSON ({e}).")
    return validate_preset(data)


def save_preset_file(preset: dict, path: str) -> str:
    preset = validate_preset(preset)
    preset["exported"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not path.lower().endswith(PRESET_EXT):
        path += PRESET_EXT
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(preset, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def safe_filename(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().strip(".")
    return (s or "camera")[:60]


def same_camera(a: dict, b: dict, tol: float = 1e-3) -> bool:
    """True when two presets would write the same values to the game."""
    try:
        for k in POINT_KEYS:
            pa, pb = a["points"][k], b["points"][k]
            if any(abs(x - y) > tol for x, y in zip(pa["pos"], pb["pos"])):
                return False
            if any(abs(x - y) > tol for x, y in zip(pa["rot"], pb["rot"])):
                return False
            if abs(pa["fov"] - pb["fov"]) > tol:
                return False
        for k in DEFAULT_TUNING:
            if abs(a["tuning"][k] - b["tuning"][k]) > tol:
                return False
        return abs(a.get("far_clip", DEFAULT_FAR_CLIP) -
                   b.get("far_clip", DEFAULT_FAR_CLIP)) <= tol
    except (KeyError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Game file access
# ---------------------------------------------------------------------------
class CameraManager:
    """Reads and writes the match camera in <game data>/level2, in place."""

    def __init__(self, game_data_path: str):
        self.root = game_data_path or ""
        self.scene = os.path.join(self.root, SCENE_FILE)
        self._map = None
        self._stamp = None

    # -- locating ----------------------------------------------------------
    def _file_stamp(self):
        st = os.stat(self.scene)
        return (st.st_size, st.st_mtime_ns)

    def load(self):
        """Resolve every offset the camera needs. Slow (reads level2): call it
        off the UI thread. Raises CameraError with a readable message."""
        if not os.path.isfile(self.scene):
            raise CameraError("level2 was not found in the game data folder.\n"
                              "Check the game path in Settings.")
        try:
            import UnityPy
        except ImportError as e:
            raise CameraError(f"UnityPy is not installed ({e}).")
        stamp = self._file_stamp()
        env = UnityPy.load(self.scene)
        objs = {o.path_id: o for o in env.objects}

        gc, parsed, hits = None, None, []
        for o in env.objects:
            if o.type.name != "MonoBehaviour" or not 250 <= o.byte_size <= 1024:
                continue
            try:
                res = parse_gameplay_camera(o.get_raw_data())
            except Exception:
                continue
            if all(pid in objs for pid in res[0].values()):
                hits.append((o, res))
        for o, res in hits:                 # confirm by script name when resolvable
            try:
                if o.read(check_read=False).m_Script.read().m_ClassName == "GameplayCamera":
                    gc, parsed = o, res
                    break
            except Exception:
                pass
        if gc is None and len(hits) == 1:
            gc, parsed = hits[0]
        if gc is None:
            raise CameraError(
                "Could not find the match camera in level2.\n\nNBA Bounce may have "
                "been updated and changed how the camera is stored. Camera edits "
                "are switched off so nothing gets damaged.")
        refs, offs = parsed
        field_by_key = {k: f for k, f, _, _ in TUNING_FIELDS}
        m = {"tuning": {k: gc.byte_start + offs[f] for k, f in field_by_key.items()},
             "points": {}}
        for k in POINT_KEYS:
            cp = objs[refs[k + "point"]]
            if cp.byte_size < 36:
                raise CameraError(f"Camera point {k} has an unexpected layout.")
            go = cp.read(check_read=False).m_GameObject.read()
            trs = [c.component.path_id for c in go.m_Component
                   if c.component.path_id in objs
                   and objs[c.component.path_id].type.name == "Transform"]
            if len(trs) != 1:
                raise CameraError(f"Camera point {k} has no Transform.")
            tr = objs[trs[0]]
            m["points"][k] = {"fov": cp.byte_start + cp.byte_size - 4,
                              "rot": tr.byte_start + _TR_ROT,
                              "pos": tr.byte_start + _TR_POS}
        m["far_clip"] = None
        for o in env.objects:
            if o.type.name != "Camera":
                continue
            try:
                c = o.read()
                if c.m_GameObject.read().m_Name != RENDER_CAMERA:
                    continue
                raw = o.get_raw_data()
                pair = struct.pack("<ff", c.near_clip_plane, c.far_clip_plane)
                i = raw.find(pair)
                if i >= 0 and raw.find(pair, i + 1) < 0:
                    m["far_clip"] = o.byte_start + i + 4
            except Exception:
                pass
            break
        self._map, self._stamp = m, stamp
        # Sanity: every value we just located must read back as a sane float.
        self._check(self.read_current())
        return self

    def _ensure_loaded(self):
        if self._map is None or self._stamp != self._file_stamp():
            self.load()

    @property
    def loaded(self):
        return self._map is not None

    @staticmethod
    def _check(p):
        try:
            validate_preset(p)
        except PresetError as e:
            raise CameraError(f"The camera values in level2 look wrong ({e}). "
                              "Camera edits are switched off so nothing gets damaged.")

    # -- read ----------------------------------------------------------------
    def read_current(self, path=None) -> dict:
        """Exact float32 values currently in the game (no rounding, so a
        read -> write round trip is lossless)."""
        if path is None:
            if self._map is None:
                raise CameraError("Camera data is not loaded yet.")
            m, path = self._map, self.scene
        else:
            m = CameraManager._map_for(path)
        with open(path, "rb") as fh:
            def rf(off, n=1):
                fh.seek(off)
                return list(struct.unpack(f"<{n}f", fh.read(4 * n)))
            pts = {k: {"pos": rf(o["pos"], 3), "rot": rf(o["rot"], 4),
                       "fov": rf(o["fov"])[0]} for k, o in m["points"].items()}
            tun = {k: rf(off)[0] for k, off in m["tuning"].items()}
            far = rf(m["far_clip"])[0] if m["far_clip"] else DEFAULT_FAR_CLIP
        return {"format": PRESET_FORMAT, "version": PRESET_VERSION,
                "name": "Current camera", "points": pts, "tuning": tun,
                "far_clip": far}

    @staticmethod
    def _map_for(path):
        mgr = CameraManager(os.path.dirname(path))
        mgr.scene = path
        mgr.load()
        return mgr._map

    # -- write ---------------------------------------------------------------
    def ensure_backup(self):
        """One-time safety copy of level2 before the first camera write."""
        bak = self.scene + BACKUP_SUFFIX
        if not os.path.exists(bak):
            shutil.copy2(self.scene, bak)
            return True
        return False

    def apply(self, preset: dict) -> dict:
        """Write a preset into level2 and read it back.

        Returns {"written": n, "verified": n, "backed_up": bool}. Raises
        CameraError on a locked file or a failed verification.
        """
        preset = validate_preset(preset)
        self._ensure_loaded()
        m = self._map
        writes = []
        for k in POINT_KEYS:
            pt, o = preset["points"][k], m["points"][k]
            writes += [(o["pos"], struct.pack("<3f", *pt["pos"])),
                       (o["rot"], struct.pack("<4f", *pt["rot"])),
                       (o["fov"], struct.pack("<f", pt["fov"]))]
        for k, off in m["tuning"].items():
            writes.append((off, struct.pack("<f", preset["tuning"][k])))
        if m["far_clip"]:
            writes.append((m["far_clip"], struct.pack("<f", preset["far_clip"])))
        size = os.path.getsize(self.scene)
        if any(off < 0 or off + len(d) > size for off, d in writes):
            raise CameraError("A camera offset falls outside level2. Nothing was written.")
        backed_up = self.ensure_backup()
        try:
            with open(self.scene, "r+b") as fh:
                for off, data in writes:
                    fh.seek(off)
                    fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except PermissionError:
            raise CameraError("level2 is locked. Close NBA Bounce completely and try again.")
        self._stamp = self._file_stamp()
        # Verify byte-for-byte
        verified = 0
        with open(self.scene, "rb") as fh:
            for off, data in writes:
                fh.seek(off)
                if fh.read(len(data)) == data:
                    verified += 1
        if verified != len(writes):
            raise CameraError(f"Only {verified} of {len(writes)} camera values "
                              "verified after writing.")
        return {"written": len(writes), "verified": verified, "backed_up": backed_up}

    def revert_default(self) -> dict:
        """Put the game's original camera back. Needs no backup file: the
        Default preset carries the original values at full precision."""
        return self.apply(default_preset())

    def has_backup(self):
        return os.path.exists(self.scene + BACKUP_SUFFIX)


# ---------------------------------------------------------------------------
# User preset library (user/camera_presets/*.bouncecam)
# ---------------------------------------------------------------------------
class PresetLibrary:
    def __init__(self, folder: str):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)

    def list(self):
        """[(path, preset)] for every valid user preset, sorted by name.
        Broken files are skipped and reported in .errors."""
        self.errors = []
        out = []
        for fn in sorted(os.listdir(self.folder)):
            if not fn.lower().endswith(PRESET_EXT):
                continue
            path = os.path.join(self.folder, fn)
            try:
                out.append((path, load_preset_file(path)))
            except Exception as e:
                self.errors.append(f"{fn}: {e}")
        out.sort(key=lambda t: t[1]["name"].lower())
        return out

    def path_for(self, name: str) -> str:
        return os.path.join(self.folder, safe_filename(name) + PRESET_EXT)

    def save(self, preset: dict) -> str:
        return save_preset_file(preset, self.path_for(preset["name"]))

    def delete(self, path: str):
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(self.folder):
            raise PresetError("Only presets in your camera preset folder can be deleted.")
        os.remove(path)


# ---------------------------------------------------------------------------
# Developer CLI:  python camera_manager.py <NBA Bounce_Data> [dump|apply X|default]
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        sys.exit("usage: camera_manager.py <NBA Bounce_Data> dump|default|apply <key or file>")
    cm = CameraManager(sys.argv[1]).load()
    cmd = sys.argv[2]
    if cmd == "dump":
        print(json.dumps(cm.read_current(), indent=1))
    elif cmd == "default":
        print(cm.revert_default())
    elif cmd == "apply":
        arg = sys.argv[3]
        pre = builtin(arg) if arg in BUILTIN_PRESETS else load_preset_file(arg)
        print(cm.apply(pre))
