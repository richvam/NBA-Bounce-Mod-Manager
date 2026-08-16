# Mesh Modding Guide

Everything about the **Meshes** tab: what it can do, what it can't, and why.
For the general app setup, see [README.md](README.md).

---

## What a mesh is, here

NBA Bounce's geometry — players, the ball, the hoop, the arena, the court
surface — is stored as Unity `Mesh` objects inside the same `.assets` files the
textures live in. A mesh is just numbers: a list of vertex positions, the
attributes attached to each vertex (normal, tangent, colour, UV coordinates,
bone weights), and a list of triangles indexing into that vertex list.

Three details drive everything else in this tab:

| Detail | What it means for you |
|---|---|
| **Vertex data is usually streamed** | The vertex numbers aren't in the `.assets` file at all — they're at a byte offset inside the companion `.resS` file. The app appends new vertex data there, so **the vertex count of a replacement is essentially unlimited** |
| **The index buffer is not streamed** | The triangle list *is* stored inside the object, at a fixed size. That's the one hard limit: **a replacement can have at most as many triangles as the mesh it replaces** |
| **The vertex layout is fixed per mesh** | Which attributes exist, in what order, at what precision, is baked into the mesh's channel table. A replacement is re-encoded into that exact layout — you don't get to add a UV set that the original didn't have |

The tab shows all of this as **Replacement budget** under the viewport.

---

## Viewing

Select any mesh in the list to see it in the viewport.

| Control | Action |
|---|---|
| Drag | Orbit |
| Right-drag (or Shift+drag) | Pan |
| Mouse wheel | Zoom |
| Double-click | Re-frame the model |

The **mode** dropdown changes how it's drawn:

- **Shaded** — flat-lit solid, the default
- **Textured** — click **Texture…** first and pick a PNG (export one from the
  Textures tab). Each triangle is filled with the texture colour at its UV
  centre, which is enough to see which part of an atlas a mesh actually uses
- **Wireframe** / **Shaded + Wire** — topology and density
- **Points** — the raw vertex cloud
- **Normals** — surface direction as RGB; flat patches of one colour mean flat
  shading, and a mesh with inverted faces shows up as an obvious colour flip
- **Submeshes** — one colour per material slot, so you can see how the mesh is
  split before you replace it

**Grid** toggles the ground plane, **Spin** turntables the model, **📷** saves a
1600×1200 render, and **Reset View** puts the camera back.

Dragging a very dense mesh drops to a point cloud until you let go — that's the
"fast preview while dragging" note in the corner, not a glitch.

---

## Exporting

**⬇ Export Mesh** writes whichever model is currently in the viewport (Original
or Replacement) to:

| Format | Use it for |
|---|---|
| **OBJ** | The round-trip format. Keeps UVs, normals and one group per submesh. Import this back and everything lines up |
| **GLB** (glTF 2.0) | Opening in Blender, Windows 3D Viewer or any web viewer with a double-click. Export only |
| **PLY** | Round-trips positions, normals and UVs; handy for scanning/sculpting tools |
| **STL** | 3D printing and simple solids. No UVs, no submeshes — don't use it for anything you plan to import back |

**⬇⬇ Export List** dumps every mesh currently shown in the list as OBJ into a
folder you pick, named `<mesh name>_<path_id>.obj`. Search or filter first, then
export, then open the folder in Blender — that's by far the fastest way to work
out which of several hundred meshes is the one you actually want.

Exported coordinates are converted from Unity's left-handed axes to the
right-handed convention every DCC tool expects (X is negated and triangle
winding is reversed). The importers undo it exactly, so an export → import
round-trip is lossless.

---

## Importing a replacement

1. Select the mesh you want to replace.
2. **⬆ Import Replacement**, pick an `.obj`, `.ply` or `.stl`.
3. The import dialog compares your model against the original and against the
   slot's budget. Set a scale if the sizes don't match — **Match original**
   fills in the factor that makes the bounding boxes the same size — and
   optionally re-centre it on the original's position.
4. **Queue Replacement**. Nothing has been written to the game yet.
5. **▶ Apply Mods** (top bar) writes every queued texture *and* mesh mod into
   the game in one pass.

### Modelling rules of thumb

- **Stay under the triangle budget.** In Blender: *Modifier ▸ Decimate*,
  Collapse, drop the ratio until the count fits.
- **Keep the UVs.** Whatever texture the game already puts on that mesh is
  still applied afterwards, so a model with no UVs comes out with the whole
  texture smeared across it. Unwrap, or start from the exported original.
- **Match the original's scale and origin.** Game meshes are in Unity units and
  positioned relative to their own origin; a model exported in metres from a
  scene where it sat at the world origin can land enormous or off to the side.
  The scale/re-centre options in the import dialog handle the common cases.
- **Triangulate.** Quads are fan-triangulated on import, which is exact for
  convex quads and can be sloppy for concave n-gons.
- **Keep groups if you want submeshes.** OBJ groups map onto the original's
  material slots in order. Extra groups are merged into the last slot; unused
  slots are emptied.

### Skinned meshes (players, mascots)

A mesh with a 🦴 in the list is skinned: every vertex carries bone indices and
weights, and without them it collapses to a heap at the skeleton's root.

Your replacement almost certainly doesn't have those weights, so with **Transfer
bone weights** on (the default) each new vertex borrows the weights of the
closest original vertex. For a model that's roughly the same shape in roughly
the same place — a re-proportioned player, a different head — that works well.
For something wildly different, expect to check it in-game.

Bone *hierarchy* is untouched: you're replacing geometry, not the skeleton.

### What can't be replaced

| Situation | Why |
|---|---|
| Meshes marked 🗜 (compressed) | Their vertex data is bit-packed; this app can read and export them but not rewrite them |
| Meshes with blend shapes | The shapes still reference the old vertex order. The mesh is written, but facial/morph animation on it will be wrong — the app warns you |
| Baked collision | Physics keeps using the original shape; only the visual mesh changes |
| More than 65,535 vertices on a 16-bit mesh | The index buffer physically can't address them |

---

## The triangle budget, and the rebuild fallback

By default a replacement is patched in **in place**: the rewritten mesh object
must serialize to exactly the same number of bytes as the one it replaces, so it
can be spliced over the original and every other object's offset in that file
stays valid. This is the same technique the Textures tab uses, and it cannot
corrupt the rest of the file.

That's why the triangle cap exists — and why a replacement with *fewer*
triangles is always fine (the leftover index buffer is zero-filled past the end
of the last submesh, where nothing reads it).

**Allow file rebuild** lifts the cap. When a replacement needs a bigger index
buffer, the whole `.assets` file is re-serialized with new object offsets.
Before the rebuilt file replaces the original, every other object in it is
checked to still be present and the same size; if anything moved, the rebuild is
abandoned and your game file is left untouched. Even so it is a much bigger
operation than an in-place patch — it holds the entire file in memory — so leave
it off unless you need it.

---

## Applying, restoring, removing

- **▶ Apply Mods** writes textures and meshes together. They have to go
  together: both append their bulk data to the same `.resS` file, which is
  rewound to its backup once per run, so applying one kind alone would strand
  the other kind's data.
- **♻ Restore Game Files** rolls every `.assets`/`.resS` back to the backup
  taken before the first write. Your model files and queue are kept, so you can
  re-apply at any time — including after a game update, which overwrites the
  game's files.
- **✕ Remove Mod** drops one replacement from the queue and puts that single
  mesh back to stock from the backup, leaving your other mods alone.

Backups are made once, the first time a file is written, and never overwritten
after that — so they always hold true stock data.

---

## Where things are stored

```
<mods_folder>/
├── mesh_mods.json      # which model file replaces which mesh, and its import options
└── meshes/             # copies of the model files you imported
```

`mesh_mods.json` keys are `<assets file>:<path_id>`. Deleting it un-queues every
mesh mod (it doesn't undo anything already written — use Restore for that).

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "over budget — needs the rebuild fallback" | Your model has more triangles than the slot holds. Decimate it, or enable Allow file rebuild |
| Replacement is invisible in-game | Usually scale or origin: re-import with **Match original** and **Move to the original's centre** |
| Replacement is inside-out | Your model's face winding is flipped. In Blender: *Mesh ▸ Normals ▸ Recalculate Outside* before exporting |
| Texture is smeared across the model | The model has no UVs, or UVs that don't match the game's texture layout |
| A player is stretched toward one point | Bone weight transfer didn't map cleanly — the replacement is too different in shape from the original |
| Mesh looks right in the viewer, wrong in-game | The viewer draws the mesh alone; in-game it's also scaled and positioned by its GameObject, and lit by the game's shaders |
| "The process cannot access the file because it is being used by another process" (WinError 32) | Something else has the `.assets` file open. Quit NBA Bounce, quit Steam entirely (not just the library window — Steam holds game files open while the game runs or verifies), close any other asset tool, then Apply again |
| Everything is fine but nothing changed in-game | The `.assets` files were replaced by a game update, or Apply wasn't pressed after queueing |

---

## Testing without the game

```
python tools/mesh_selftest.py
```

Runs 75 checks: every exporter and importer round-trip, the vertex buffer this
app writes decoded back by UnityPy's own mesh reader (a full Unity 6 channel
layout, including packed colours and bone weights), the index padding and submesh
windows, the .resS offset bookkeeping against real files on disk, the
bone-weight transfer, and the file-handle/locked-file handling the rebuild
fallback depends on. It needs no copy of the game.
