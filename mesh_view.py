"""
mesh_view.py -- the 3D viewport for the Meshes tab.

Tkinter has no 3D canvas, and a modding tool shouldn't need a GPU stack just to
look at a basketball, so this is a small software renderer: vertices are
transformed in Python (numpy when it's installed, plain lists when it isn't),
triangles are depth-sorted and painted back-to-front with PIL, and the finished
image is blitted onto a tk.Canvas.

Painter's algorithm rather than a z-buffer, because filling ~10k polygons with
PIL is fast (the fill loop runs in C) while a per-pixel depth test in Python is
not. The consequences are the usual ones -- interpenetrating surfaces can sort
wrong -- and they don't matter for looking at a mesh before you replace it.

Nothing here knows about Unity. It draws anything with `.vertices`,
`.triangles`, optional `.normals`/`.uvs` and a `.submeshes` list, which is what
mesh_manager.MeshGeometry provides.

Controls
    drag                orbit
    right-drag / shift  pan
    wheel               zoom
    double-click        re-frame the model
"""

import math
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

try:
    import numpy as _np
except Exception:
    _np = None

# Display modes offered in the toolbar.
MODES = ["Shaded", "Textured", "Wireframe", "Shaded + Wire", "Points", "Normals",
         "Submeshes"]

# Distinct per-submesh colors, chosen to stay readable in both app themes.
SUBMESH_COLORS = [
    (200, 84, 96), (86, 154, 220), (240, 190, 84), (110, 196, 140),
    (176, 128, 220), (232, 142, 74), (96, 200, 200), (214, 122, 178),
]

_LIGHT = (0.35, 0.62, 0.70)          # view-space key light
_MAX_INTERACTIVE_TRIS = 9000         # above this, dragging shows a point cloud


def _normalize(v):
    length = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / length for c in v)


class MeshView(tk.Frame):
    """A pannable/orbitable 3D view of one mesh."""

    def __init__(self, parent, theme=None, width=560, height=420, on_status=None):
        theme = theme or {}
        self.bg_color   = theme.get("ebg", "#0B1220")
        self.grid_color = theme.get("sub", "#4C6785")
        self.text_color = theme.get("text", "#E8EEFF")
        self.accent     = theme.get("accent", "#1D428A")
        # The app ships a dark AND a light theme, and the viewport surface flips
        # with it. A pale clay model and a gold wireframe read well on the dark
        # background and vanish on the light one, so the model's own colors are
        # picked from the background's brightness rather than hard-coded.
        self._light_bg  = self._luminance(self._hex(self.bg_color)) > 0.5
        self.wire_color = self.accent if self._light_bg else theme.get("gold", "#FDB927")
        self.base_color = (108, 124, 148) if self._light_bg else (196, 200, 210)
        super().__init__(parent, bg=self.bg_color, highlightthickness=0)

        self.canvas = tk.Canvas(self, bg=self.bg_color, highlightthickness=0,
                                width=width, height=height, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        self._image_id = self.canvas.create_image(0, 0, anchor="nw")
        self._photo    = None
        self.on_status = on_status

        # model
        self.geo        = None
        self.texture    = None
        self._tex_cache = None
        self._face_tex_colors = None

        # camera
        self.yaw, self.pitch = math.radians(25), math.radians(-18)
        self.zoom            = 1.0
        self.pan             = [0.0, 0.0]
        self._center         = (0.0, 0.0, 0.0)
        self._radius         = 1.0

        # options
        self.mode        = "Shaded"
        self.show_grid   = True
        self.show_axes   = True
        self.show_stats  = True
        self.spin        = False

        self._drag       = None
        self._pending    = None
        self._spin_job   = None
        self._size       = (width, height)

        self.canvas.bind("<Configure>",       self._on_configure)
        self.canvas.bind("<ButtonPress-1>",   lambda e: self._start(e, "orbit"))
        self.canvas.bind("<ButtonPress-2>",   lambda e: self._start(e, "pan"))
        self.canvas.bind("<ButtonPress-3>",   lambda e: self._start(e, "pan"))
        self.canvas.bind("<Shift-ButtonPress-1>", lambda e: self._start(e, "pan"))
        self.canvas.bind("<B1-Motion>",       self._on_drag)
        self.canvas.bind("<B2-Motion>",       self._on_drag)
        self.canvas.bind("<B3-Motion>",       self._on_drag)
        for seq in ("<ButtonRelease-1>", "<ButtonRelease-2>", "<ButtonRelease-3>"):
            self.canvas.bind(seq, self._on_release)
        self.canvas.bind("<Double-Button-1>", lambda e: self.frame_model())
        self.canvas.bind("<MouseWheel>",      self._on_wheel)              # Windows
        self.canvas.bind("<Button-4>", lambda e: self._zoom_by(1.12))      # X11
        self.canvas.bind("<Button-5>", lambda e: self._zoom_by(1 / 1.12))

    # ── model ────────────────────────────────────────────────────────────────
    def set_geometry(self, geo, keep_camera=True):
        self.geo = geo
        self._face_tex_colors = None
        self._prepare()
        if not keep_camera:
            self.frame_model()
        else:
            self.render()

    def set_texture(self, image):
        """PIL image used by 'Textured' mode; None turns it off."""
        self.texture = image
        self._tex_cache = None
        self._face_tex_colors = None
        if self.mode == "Textured":
            self.render()

    def set_mode(self, mode):
        self.mode = mode if mode in MODES else "Shaded"
        self.render()

    def _prepare(self):
        """Cache the arrays the render loop needs."""
        geo = self.geo
        if geo is None or not geo.vertices or not geo.triangles:
            self._V = self._F = None
            return
        center, extent = geo.center_extent()
        self._center = center
        self._radius = max(max(extent), 1e-4)

        if _np is not None:
            self._V = _np.asarray(geo.vertices, dtype="float32")
            self._F = _np.asarray(geo.triangles, dtype="int32")
        else:
            self._V = geo.vertices
            self._F = geo.triangles

        # triangle -> submesh index, for the Submeshes palette
        face_sub = [0] * len(geo.triangles)
        for si, sm in enumerate(geo.submeshes):
            for t in range(sm["first"], min(sm["first"] + sm["count"], len(face_sub))):
                face_sub[t] = si
        self._face_sub = face_sub

    # ── camera ───────────────────────────────────────────────────────────────
    def frame_model(self):
        self.zoom = 1.0
        self.pan  = [0.0, 0.0]
        self.render()

    def reset_view(self):
        self.yaw, self.pitch = math.radians(25), math.radians(-18)
        self.frame_model()

    def set_spin(self, on):
        self.spin = bool(on)
        if self._spin_job:
            self.after_cancel(self._spin_job)
            self._spin_job = None
        if self.spin:
            self._spin_step()

    def _spin_step(self):
        if not self.spin:
            return
        self.yaw += math.radians(2.0)
        self.render(interactive=True)
        self._spin_job = self.after(60, self._spin_step)

    # ── events ───────────────────────────────────────────────────────────────
    def _on_configure(self, event):
        if (event.width, event.height) != self._size and event.width > 1:
            self._size = (event.width, event.height)
            self.render()

    def _start(self, event, kind):
        self._drag = (kind, event.x, event.y)
        return "break"

    def _on_drag(self, event):
        if not self._drag:
            return
        kind, x0, y0 = self._drag
        dx, dy = event.x - x0, event.y - y0
        if kind == "orbit":
            self.yaw   += dx * 0.01
            self.pitch = max(-math.pi / 2 + 0.01,
                             min(math.pi / 2 - 0.01, self.pitch - dy * 0.01))
        else:
            self.pan[0] += dx
            self.pan[1] += dy
        self._drag = (kind, event.x, event.y)
        self.render(interactive=True)

    def _on_release(self, event):
        if self._drag:
            self._drag = None
            self.render()

    def _on_wheel(self, event):
        self._zoom_by(1.12 if event.delta > 0 else 1 / 1.12)

    def _zoom_by(self, factor):
        self.zoom = max(0.05, min(60.0, self.zoom * factor))
        self.render(interactive=True)
        # a wheel event has no "release", so settle on a full-quality frame
        if self._pending:
            self.after_cancel(self._pending)
        self._pending = self.after(220, self.render)

    # ── rendering ────────────────────────────────────────────────────────────
    def render(self, interactive=False):
        w, h = self._size
        if w < 2 or h < 2:
            return
        img  = Image.new("RGB", (w, h), self.bg_color)
        draw = ImageDraw.Draw(img)
        self._draw_backdrop(draw, w, h)

        if self.geo is None or self._V is None:
            draw.text((w // 2 - 60, h // 2), "No mesh selected",
                      fill=self._mix(self._hex(self.bg_color),
                                     self._hex(self.text_color), 0.75))
        else:
            try:
                self._draw_mesh(draw, w, h, interactive)
            except Exception as exc:                      # never kill the UI over a frame
                draw.text((12, 12), f"Render error: {exc}", fill=(220, 90, 90))
            if self.show_axes:
                self._draw_axes(draw, w, h)
            if self.show_stats:
                self._draw_stats(draw, w, h, interactive)

        self._photo = ImageTk.PhotoImage(img)
        self.canvas.itemconfig(self._image_id, image=self._photo)

    def render_image(self, width=1280, height=960):
        """Off-screen high-resolution render, for 'Save Snapshot'."""
        old_size, old_stats = self._size, self.show_stats
        self._size, self.show_stats = (width, height), False
        img  = Image.new("RGB", (width, height), self.bg_color)
        draw = ImageDraw.Draw(img)
        self._draw_backdrop(draw, width, height)
        if self.geo is not None and self._V is not None:
            self._draw_mesh(draw, width, height, False)
            if self.show_axes:
                self._draw_axes(draw, width, height)
        self._size, self.show_stats = old_size, old_stats
        return img

    # -- geometry transform ---------------------------------------------------
    def _basis(self):
        cy, sy = math.cos(self.yaw),   math.sin(self.yaw)
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        # yaw about Y, then pitch about X; rows are the view-space axes
        return ((cy,        0.0,  -sy),
                (sp * sy,   cp,   sp * cy),
                (cp * sy,  -sp,   cp * cy))

    def _project(self, w, h):
        """Model -> screen. Returns (xs, ys, zs) and the pixels-per-unit scale."""
        R  = self._basis()
        cx, cy = self._center[0], self._center[1]
        cz = self._center[2]
        scale = (min(w, h) * 0.42 / self._radius) * self.zoom
        ox, oy = w / 2 + self.pan[0], h / 2 + self.pan[1]

        if _np is not None:
            Rm = _np.asarray(R, dtype="float32")
            P  = (self._V - _np.asarray([cx, cy, cz], dtype="float32")) @ Rm.T
            xs = ox + P[:, 0] * scale
            ys = oy - P[:, 1] * scale
            return xs, ys, P[:, 2], P, scale

        P = []
        for vx, vy, vz in self._V:
            ax, ay, az = vx - cx, vy - cy, vz - cz
            P.append((R[0][0] * ax + R[0][1] * ay + R[0][2] * az,
                      R[1][0] * ax + R[1][1] * ay + R[1][2] * az,
                      R[2][0] * ax + R[2][1] * ay + R[2][2] * az))
        xs = [ox + p[0] * scale for p in P]
        ys = [oy - p[1] * scale for p in P]
        zs = [p[2] for p in P]
        return xs, ys, zs, P, scale

    def _draw_mesh(self, draw, w, h, interactive):
        geo   = self.geo
        xs, ys, zs, P, scale = self._project(w, h)
        n_tris = len(geo.triangles)

        if self.show_grid:
            self._draw_grid(draw, w, h, scale)

        point_cloud = (self.mode == "Points"
                       or (interactive and n_tris > _MAX_INTERACTIVE_TRIS))
        if point_cloud:
            step = 1 if len(xs) < 25000 else max(1, len(xs) // 25000)
            col  = self._hex(self.wire_color)
            rng  = range(0, len(xs), step)
            for i in rng:
                x, y = float(xs[i]), float(ys[i])
                if -2 <= x <= w + 2 and -2 <= y <= h + 2:
                    draw.point((x, y), fill=col)
            return

        order, shades, normals = self._sorted_faces(P, w, h, xs, ys)
        wire   = self.mode in ("Wireframe", "Shaded + Wire")
        filled = self.mode != "Wireframe"
        wire_col = self._hex(self.wire_color) if self.mode != "Shaded + Wire" \
            else ((70, 82, 104) if self._light_bg else (40, 44, 56))

        base = self.base_color
        tex_colors = self._face_texture_colors() if self.mode == "Textured" else None

        for fi in order:
            tri = geo.triangles[fi]
            pts = [(float(xs[tri[0]]), float(ys[tri[0]])),
                   (float(xs[tri[1]]), float(ys[tri[1]])),
                   (float(xs[tri[2]]), float(ys[tri[2]]))]
            if filled:
                if self.mode == "Normals":
                    nx, ny, nz = normals[fi]
                    color = (int((nx * 0.5 + 0.5) * 255),
                             int((ny * 0.5 + 0.5) * 255),
                             int((nz * 0.5 + 0.5) * 255))
                elif self.mode == "Submeshes":
                    sub = SUBMESH_COLORS[self._face_sub[fi] % len(SUBMESH_COLORS)]
                    s = shades[fi]
                    color = (int(sub[0] * s), int(sub[1] * s), int(sub[2] * s))
                elif tex_colors is not None:
                    r, g, b = tex_colors[fi]
                    s = shades[fi]
                    color = (int(r * s), int(g * s), int(b * s))
                else:
                    s = shades[fi]
                    color = (int(base[0] * s), int(base[1] * s), int(base[2] * s))
                draw.polygon(pts, fill=color,
                             outline=wire_col if wire else None)
            else:
                draw.line(pts + [pts[0]], fill=wire_col)

    def _sorted_faces(self, P, w, h, xs, ys):
        """Back-to-front face order, per-face lighting and view-space normals.

        Off-screen triangles are dropped here rather than in the draw loop --
        when you zoom into a detail it cuts the work by an order of magnitude.
        """
        geo = self.geo
        light = _normalize(_LIGHT)
        # more fill light on the light theme: its base color is dark, so a
        # 0.28-ambient shadow side turns into an unreadable near-black blob
        ambient = 0.45 if self._light_bg else 0.28
        direct  = 1.0 - ambient

        if _np is not None:
            F  = self._F
            v0, v1, v2 = P[F[:, 0]], P[F[:, 1]], P[F[:, 2]]
            nrm = _np.cross(v1 - v0, v2 - v0)
            length = _np.linalg.norm(nrm, axis=1, keepdims=True)
            nrm = nrm / _np.maximum(length, 1e-12)
            shades = ambient + direct * _np.abs(nrm @ _np.asarray(light, dtype="float32"))

            sx, sy = _np.asarray(xs), _np.asarray(ys)
            fx, fy = sx[F], sy[F]
            visible = ((fx.max(1) >= -1) & (fx.min(1) <= w + 1)
                       & (fy.max(1) >= -1) & (fy.min(1) <= h + 1))
            depth = (v0[:, 2] + v1[:, 2] + v2[:, 2]) / 3.0
            idx   = _np.nonzero(visible)[0]
            order = idx[_np.argsort(depth[idx], kind="stable")]
            return order.tolist(), shades.tolist(), nrm.tolist()

        shades, normals, keyed = [], [], []
        for fi, (a, b, c) in enumerate(geo.triangles):
            p0, p1, p2 = P[a], P[b], P[c]
            ux, uy, uz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
            wx, wy, wz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
            nx, ny, nz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
            length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            nx, ny, nz = nx / length, ny / length, nz / length
            normals.append((nx, ny, nz))
            shades.append(ambient + direct * abs(nx * light[0] + ny * light[1]
                                                 + nz * light[2]))
            tx = (xs[a], xs[b], xs[c])
            ty = (ys[a], ys[b], ys[c])
            if max(tx) >= -1 and min(tx) <= w + 1 and max(ty) >= -1 and min(ty) <= h + 1:
                keyed.append(((p0[2] + p1[2] + p2[2]) / 3.0, fi))
        keyed.sort()
        return [fi for _, fi in keyed], shades, normals

    def _face_texture_colors(self):
        """One flat color per triangle, sampled from the texture at the UV
        centroid. Per-pixel UV mapping in Python would be far too slow; this
        costs one lookup per triangle and still shows you which part of the
        atlas a mesh actually uses."""
        if self._face_tex_colors is not None:
            return self._face_tex_colors
        geo = self.geo
        if self.texture is None or not geo.uvs:
            return None
        if self._tex_cache is None:
            tex = self.texture.convert("RGB")
            if max(tex.size) > 512:
                tex.thumbnail((512, 512))
            self._tex_cache = tex
        tex = self._tex_cache
        tw, th = tex.size
        px = tex.load()
        uvs = geo.uvs
        out = []
        for a, b, c in geo.triangles:
            u = (uvs[a][0] + uvs[b][0] + uvs[c][0]) / 3.0
            v = (uvs[a][1] + uvs[b][1] + uvs[c][1]) / 3.0
            # Unity's UV origin is bottom-left, PIL's is top-left
            x = int(min(tw - 1, max(0, (u % 1.0) * tw)))
            y = int(min(th - 1, max(0, (1.0 - (v % 1.0)) * th)))
            out.append(px[x, y])
        self._face_tex_colors = out
        return out

    # -- decoration -----------------------------------------------------------
    def _draw_backdrop(self, draw, w, h):
        """Subtle vertical gradient so the model reads against the background.

        It brightens downward on the dark theme and darkens downward on the
        light one, so the model always sits against the flatter half."""
        top = self._hex(self.bg_color)
        factor = 0.86 if self._light_bg else 1.35
        bottom = tuple(max(0, min(255, int(c * factor + (0 if self._light_bg else 8))))
                       for c in top)
        steps = 32
        for i in range(steps):
            t = i / (steps - 1)
            color = tuple(int(top[k] + (bottom[k] - top[k]) * t) for k in range(3))
            draw.rectangle([0, int(h * i / steps), w, int(h * (i + 1) / steps)],
                           fill=color)

    def _draw_grid(self, draw, w, h, scale):
        """A 10x10 ground grid under the model, at the bottom of its bounds."""
        geo = self.geo
        lo, hi = geo.bounds()
        span = max(hi[0] - lo[0], hi[2] - lo[2], 1e-4) * 0.75
        y    = lo[1]
        cx, cz = self._center[0], self._center[2]
        R = self._basis()
        ox, oy = w / 2 + self.pan[0], h / 2 + self.pan[1]
        cy_center = self._center[1]

        def to_screen(x, z):
            ax, ay, az = x - cx, y - cy_center, z - cz
            vx = R[0][0] * ax + R[0][1] * ay + R[0][2] * az
            vy = R[1][0] * ax + R[1][1] * ay + R[1][2] * az
            return ox + vx * scale, oy - vy * scale

        color = self._mix(self._hex(self.bg_color), self._hex(self.grid_color), 0.35)
        steps = 10
        for i in range(steps + 1):
            t  = -span + 2 * span * i / steps
            draw.line([to_screen(t, -span), to_screen(t, span)], fill=color)
            draw.line([to_screen(-span, t), to_screen(span, t)], fill=color)

    def _draw_axes(self, draw, w, h):
        """Orientation gizmo: X red, Y green, Z blue, in Unity's axes."""
        R = self._basis()
        ox, oy, length = 42, h - 42, 26
        for vec, color, label in (((1, 0, 0), (226, 90, 96), "X"),
                                  ((0, 1, 0), (128, 210, 128), "Y"),
                                  ((0, 0, 1), (110, 160, 240), "Z")):
            vx = sum(R[0][k] * vec[k] for k in range(3))
            vy = sum(R[1][k] * vec[k] for k in range(3))
            ex, ey = ox + vx * length, oy - vy * length
            draw.line([(ox, oy), (ex, ey)], fill=color, width=2)
            draw.text((ex + 2, ey - 6), label, fill=color)

    def _draw_stats(self, draw, w, h, interactive):
        geo = self.geo
        lo, hi = geo.bounds()
        size = tuple(hi[i] - lo[i] for i in range(3))
        lines = [
            geo.name,
            f"{geo.vertex_count:,} verts   {geo.triangle_count:,} tris   "
            f"{len(geo.submeshes)} submesh{'es' if len(geo.submeshes) != 1 else ''}",
            f"size  {size[0]:.2f} x {size[1]:.2f} x {size[2]:.2f}",
        ]
        if interactive and geo.triangle_count > _MAX_INTERACTIVE_TRIS:
            lines.append("(fast preview while dragging)")
        color = self._hex(self.text_color)
        dim   = self._mix(self._hex(self.bg_color), color, 0.55)
        for i, line in enumerate(lines):
            draw.text((12, 10 + i * 15), line, fill=color if i == 0 else dim)

    # -- color helpers --------------------------------------------------------
    @staticmethod
    def _luminance(rgb):
        return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255.0

    @staticmethod
    def _hex(value):
        if isinstance(value, tuple):
            return value
        value = value.lstrip("#")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

    @staticmethod
    def _mix(a, b, t):
        return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
