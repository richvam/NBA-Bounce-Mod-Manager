# NBA Bounce Mod Manager

> A Windows desktop app for modding **NBA Bounce** (Unity 6): textures, audio, 3D meshes, court floors, court colors, and gameplay sliders, all from one tool, without ever touching a hex editor.

![Version](https://img.shields.io/badge/version-3.0.0-orange)
![Platform](https://img.shields.io/badge/platform-Windows-blue?logo=windows)
![Python](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)
![Unity](https://img.shields.io/badge/Unity-6000.0.37f1-black?logo=unity)
![Game](https://img.shields.io/badge/game-NBA%20Bounce%20(Steam)-red)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

| Feature | Description |
|---|---|
| Home screen | Big, one-click cards for every tool below; everything is also always reachable from the tabs at the top |
| Textures | Browse, preview, export, and replace any of the ~2,000+ `Texture2D` assets in the game |
| Audio | Browse, play, tag the language of, and replace any in-game audio clip |
| Meshes | View any of the game's 3D models in an orbit/zoom viewport, export them to OBJ/GLB/PLY/STL, and import replacements |
| Floor Patterns | Give any team's court a different wood-grain pattern, including retro/throwback variants |
| Gameplay Sliders | Tune CPU skill, shot windows, movement, and more, with 2K-style difficulty presets |
| Court Colors | Recolor each team's floor and court lines (sidelines, key, three-point arc, and more) |
| Saves | Unlock mascots, jerseys, logos, balls and trails by editing your save file |
| Dark / Light mode | NBA-branded "Arena Night" (dark) and "Broadcast Day" (light) themes, switchable in Settings |
| Automatic backups | Every tool backs up the original game files before its first write, and can restore them any time |

---

## Getting Started

### Requirements

- **Windows 10 / 11**
- **Python 3.9 or newer**, download from [python.org](https://www.python.org/downloads/)
  - During install, check **"Add Python to PATH"**
- **NBA Bounce** installed via Steam

### First-Time Setup

1. Download or clone this repository
2. Double-click **`SETUP_AND_RUN.bat`**

That's it. The script installs all Python dependencies automatically and launches the app.

### Subsequent Launches

Double-click **`RUN.bat`**. It skips the install step if everything is already in place.

### Configure Your Paths

Click **Settings** in the top bar and set:

| Setting | What to enter |
|---|---|
| **Game Data Folder** | The `NBA Bounce_Data` folder inside your Steam install, e.g. `C:\SteamLibrary\steamapps\common\NBA BOUNCE\NBA Bounce_Data\` |
| **Mods Folder** | Any folder where you want your replacement PNGs and audio files stored, e.g. `C:\Users\You\Documents\NBABounceMods` |

Click **Save & Reload**. Every tab reads from these same two paths.

---

## Home Screen

The app opens on **Home**, with a card for each tool: Textures, Audio, Meshes, Floor Patterns, Gameplay Sliders, Court Colors, and Saves. Clicking a card jumps to that tool. The tabs across the top are always visible too, so you never have to come back to Home just to switch tools.

---

## Textures

The **Textures** tab lists every `Texture2D` asset found across all of the game's `.assets` files.

- **Search box**: type any part of a texture name to filter the list live
- **Filter by file**: narrow down to a specific `.assets` source file
- **Column sort**: click any column header to sort; click again to reverse (Texture Name, Size, Mod status, or File)
- Textures with an active mod show a checkmark in the Mod column

Select a texture to see it previewed on the right, side by side with your replacement if you've imported one:

1. **Export Original**: save the true, unmodified texture as a PNG so you can edit it in Photoshop, GIMP, Aseprite, or any image editor
   - Your replacement doesn't need to match the original's resolution exactly, but matching it avoids any in-game stretching
2. **Import Replacement**: pick your edited PNG. It's copied into your Mods Folder and tracked in `mods.json`; the game file isn't touched yet
3. **Remove Mod**: clears the replacement for the selected texture, and puts that texture back to stock in the game file without disturbing your other mods

### Sprite crops

Logos, icons and unlock art are drawn through a Unity **Sprite** that crops the texture down to a box around the *original* artwork — `GSWarriors_Global` only uses 514×619 of its 1024×1024 canvas. Selecting a texture outlines that box on the **Original** preview with a dashed rectangle.

You don't have to design around it: Apply widens the crop to the whole canvas, so a replacement can use every pixel of the image no matter how much bigger than the old logo it is. The outline is there so you can see what the original was actually using.

The one exception is a texture shared by several sprites (a real atlas — only two exist in the game). Those crops can't be widened without moving the other sprites' artwork, so importing one warns you and lists the regions your replacement has to stay inside.

When you're ready to write your changes into the game, use the buttons in the top bar (available from any tab):

- **Apply Mods**: writes every queued texture *and* mesh replacement into the game in one pass. The affected `.assets` and `.resS` files are backed up automatically the first time (never overwritten after that), so this is always safe to re-run, including after a game update, when it'll instantly bring all your mods back.
- **Restore Game Files**: rolls every modded `.assets`/`.resS` file back to its original state, undoing both texture and mesh replacements. Your replacement PNGs and models are kept, so you can re-apply them at any time.

> Textures and meshes are applied together on purpose: both append their data to the same companion `.resS` file, which is rewound to its backup once per run, so writing one kind on its own would strand the other kind's data.

---

## Audio

The **Audio** tab lists every `AudioClip` asset found across the game's asset files, and works the same way as Textures: live search, filter by source file, filter by language, and sortable columns.

Select a clip to:

- **Play**: plays your queued replacement if you have one, otherwise the original
- **Original**: always plays the true original, for A/B comparison against your replacement
- **Pause** / **Stop**
- **Language**: tag the clip's spoken language from a dropdown. Your manual tag is remembered; otherwise the app makes a best-effort guess from the clip's name
- **Export WAV**: save the original clip as a WAV file
- **Queue Replacement**: pick a WAV to replace the clip with (experimental, see note below)
- **Remove**: clears a queued replacement

As with Textures, the top bar has:

- **Apply Audio Mods**: writes queued replacements into the game, backing up the affected files first
- **Restore Audio**: restores the originals; your queued replacements are kept

> **Note on audio replacement:** NBA Bounce's audio is FMOD-Vorbis, and there's no pure-Python FMOD encoder available, so replacements are injected as raw PCM with the clip's format flipped to PCM. This is confirmed to work for clips the game loads into memory up front. For clips that stream continuously, the game may ignore the replacement; it won't crash, the sound just won't change.

---

## Meshes

The **Meshes** tab lists every `Mesh` asset in the game — players, the ball, the hoop, the arena, the court — and shows the selected one in a 3D viewport you can orbit (drag), pan (right-drag), zoom (wheel) and re-frame (double-click). There's no GPU requirement: the viewport is a small software renderer drawing onto the canvas.

Display modes: **Shaded**, **Textured** (pick any PNG with **Texture…** — for example one you exported from the Textures tab — and each triangle is filled with the texture colour at its UV centre), **Wireframe**, **Shaded + Wire**, **Points**, **Normals**, and **Submeshes** (one colour per material slot). **Spin** turntables the model and **📷** saves a render.

- **⬇ Export Mesh**: save the model as **OBJ** (the round-trip format: UVs, normals, one group per submesh), **GLB** (glTF 2.0, opens straight in Blender or Windows 3D Viewer), **PLY**, or **STL**
- **⬇⬇ Export List**: dump everything currently in the list as OBJ into a folder — the fastest way to find which of several hundred meshes is the one you want
- **⬆ Import Replacement**: pick an OBJ, PLY or STL. The import dialog compares it against the original and against what the slot can take, and offers a one-click scale factor to match the original's size
- **✕ Remove Mod**: drops the replacement and puts that mesh back to stock

**Replacement budget.** Vertex data is streamed to the `.resS` file, so a replacement can have as many vertices as it likes, but the triangle list lives inside the object at a fixed size — so a replacement can have at most as many triangles as the mesh it replaces. The exact numbers for the selected mesh are shown under the viewport. Decimate in Blender to fit, or tick **Allow file rebuild** to lift the limit by re-writing the whole `.assets` file (opt-in, and verified object-by-object before it replaces anything).

Skinned meshes (marked 🦴) keep animating: bone weights are copied from the nearest original vertex, since a model you made in Blender won't have the game's rigging.

Full details, modelling rules and troubleshooting: **[docs/MESH_MODDING_GUIDE.md](docs/MESH_MODDING_GUIDE.md)**.

---

## Floor Patterns

Give any team's court a different wood-grain look, chosen from a library of patterns (basketweave, herringbone, chevron, diagonal maple, wide plank, and more).

- **Assign to court**: apply a pattern to the selected team
- **Reset court to stock floor**: put a team back to the default look
- **Export pattern for editing** / **Import custom PNG**: bring in your own pattern art
- **Apply to game**: write your assignments into the game files
- **Revert all floors**: restore every court to stock

Up to 8 custom patterns can be active across the league at once (the pattern library itself is unlimited; you just can't have more than 8 different ones assigned at the same time).

---

## Gameplay Sliders

Fine-tune how the game plays across 7 categories: Shooting, Passing, Defense, Rebounding, Movement, Awareness, and Controls (35 sliders in total).

- **Difficulty presets** (2K-style naming): Rookie, Pro, All-Star, Superstar, Hall of Fame. Pick one as a starting point, then adjust individual sliders from there
- **Which difficulty in-game?**: an in-app explainer, since NBA Bounce's own Rookie / Advanced / Pro menu doesn't line up one-to-one with these preset names
- **Apply to Game** / **Restore Stock**
- **Save Set** / **Load Set**: save your own slider combinations and reload them later

---

## Court Colors

Recolor a team's court, split into two independent pieces:

- **Floor Color**: the parquet tint itself
- **Court Lines & Areas**: seven separately-colorable elements: Sideline/Apron, Key/Paint Fill, Outer Surround, Court Lines, Key/Paint Marks, Three-Point Line, and Free-Throw Circle

Pick a team from the dropdown, then click any color swatch to open the built-in color picker (RGB sliders, hex entry, and an eyedropper that can sample any pixel on your screen, not just inside the app). Other options:

- **Copy colors from**: copy another team's current colors onto the selected team as a starting point (only updates the preview until you apply)
- **Also apply to retro / throwback courts**: keep a team's alternate courts in sync
- **Apply to Game**: write your changes (only visible in-game after a restart)
- **Reset This Team**: revert the selected team back to its original colors

---

## Saves

Unlocks cosmetics by editing your save file directly. It touches no `.assets`
file at all — it only reads the game's `level1` scene to work out what exists,
and writes to your save in
`%USERPROFILE%\AppData\LocalLow\UnfinishedPixel\NBA Bounce\`.

- **Unlock Everything**: unlock every item available in your game build
- **Unlock Selected**: pick individual rows instead
- **Back Up Save / Restore Backup**: timestamped copies, kept outside the synced
  folder so they don't eat Steam Cloud quota
- **Export Copy**: save a copy elsewhere

### ⚠ Close Steam first

**This is the step that catches everyone.** The game uses Steam Auto-Cloud, so
Steam re-syncs the save folder every time it launches the game. Edit a save
while Steam is running — or with cloud sync still on — and Steam quietly puts
the old copy back. It looks exactly like the edit did nothing.

1. Steam → NBA BOUNCE → Properties → General → uncheck **Keep game saves in the Steam Cloud**
2. Exit Steam completely (check the tray *and* Task Manager for `steam.exe` and `steamwebhelper.exe`)
3. Apply the edit
4. Start Steam, launch the game, confirm your unlocks
5. Re-enable cloud sync — Steam uploads the new save as authoritative

### Notes

- **Not everything is unlockable.** Of the 333 items in the game data, 34 ship
  with their `m_bEnabled` flag turned off. Those are excluded on purpose: the
  game's `loadUnlocks()` resolves each saved id against
  `getUnlockablesEnabled()`, so writing a disabled id makes it throw
  `Sequence contains no matching element` and **hang on the loading screen**.
  The catalog is filtered on that flag and the tab refuses to write one.
- **Nothing you own is ever removed**, including the `RANDOM_*` cosmetics the
  game generates at runtime, which appear in no static catalog.
- **The catalog is re-read after a game update.** It's cached in
  `unlockables_catalog.json`, keyed on `level1`'s size and timestamp, so a patch
  that changes which items are enabled invalidates the cache automatically.
- **Achievements are unaffected.** Steam achievements live on Valve's servers;
  unlocking an `ACH_*` cosmetic locally does not award one.
- **Saves are personal, not portable.** There's no account binding, so a save
  will load for anyone — but it also carries the owner's avatars, teams,
  seasons and display settings. Share the tool, not the save.

---

## Settings

Open **Settings** from the top bar at any time.

| Option | Description |
|---|---|
| **Game Data Folder** | Path to `NBA Bounce_Data` |
| **Mods Folder** | Where your PNG/WAV replacements and mod metadata are stored |
| **Appearance** | **Dark** ("Arena Night") or **Light** ("Broadcast Day"); takes effect the next time you launch the app |

---

## File Structure

Everything you actually run sits at the top; the rest is filed underneath, and
you never need to open any of it — every tool is reachable from the app.

```
nba-bounce-mod-manager/
├── SETUP_AND_RUN.bat            # First-time setup + launch  ← start here
├── RUN.bat                      # Quick launch (after first setup)
├── app.py                       # The app itself: every tab and tool
├── README.md / CHANGELOG.md / LICENSE
│
├── modules/                     # What app.py is built from
│   ├── app_paths.py             #   Where every file lives (the folders below)
│   ├── audio_manager.py         #   Audio tab: playback, language tags, replacements
│   ├── mesh_tab.py              #   Meshes tab: browser, viewer, import/export UI
│   ├── mesh_view.py             #   Software 3D renderer for the mesh viewport
│   ├── mesh_manager.py          #   Mesh decode/encode, OBJ/GLB/PLY/STL, writeback
│   ├── sprite_crop.py           #   Widens baked sprite crops so replacements aren't cut off
│   ├── floor_patterns.py        #   Floor Patterns tool
│   ├── retro_eras.py            #   Names each court's throwback era
│   ├── slider_tab.py / slider_manager.py   # Gameplay Sliders tool
│   └── save_tab.py / save_manager.py       # Saves tool: unlock cosmetics via the .sav
│
├── data/                        # Catalogs that ship with the app (read-only)
│   ├── sliders_catalog.json / presets.json # Slider definitions and difficulty presets
│   └── retro_court_eras.json    #   Era labels for each retro court variant
│
├── docs/                        # The guides
│   ├── MESH_MODDING_GUIDE.md
│   └── NBA_Bounce_Team_Identity_Texture_Guide.md / .pdf
│
├── tools/                       # Developer scripts — the app never runs these
│   ├── mesh_selftest.py         #   Mesh pipeline self-test (no game files needed)
│   ├── build_slider_assets.py   #   Regenerates the slider catalog from the game DLL
│   └── build_retro_eras.py      #   Regenerates the retro era labels
│
└── user/                        # Everything the app WRITES — yours, never overwritten
    ├── config.json              #   Saved paths and theme preference
    ├── floor_patterns.json      #   Which pattern each court is set to
    ├── unlockables_catalog.json #   Cached unlockable ids read from level1
    └── save_backups/            #   Fallback copies of your .sav
```

Updating the app is a matter of replacing everything except `user/` — your
paths, court choices and save backups live only in there. If you're upgrading
from v2.x, the files that used to sit next to `app.py` are moved into `user/`
automatically the first time you launch v3.

**Your Mods Folder** (location you choose in Settings):

```
<mods_folder>/
├── mods.json               # Maps each texture to its replacement PNG
├── audio_mods.json         # Maps each audio clip to its replacement WAV
├── mesh_mods.json          # Maps each mesh to its replacement model + import options
├── languages.json          # Manually-tagged audio clip languages
├── meshes/                 # Your replacement models (.obj / .ply / .stl)
├── *.png                   # Your replacement textures
└── *.wav                   # Your replacement audio clips
```

> **Don't delete these `.json` files**: they're how the app knows which replacement belongs to which in-game asset.

---

## Technical Notes

<details>
<summary>How assets are patched (click to expand)</summary>

NBA Bounce uses Unity 6 (`6000.0.37f1`). Textures and audio are stored across multiple `.assets` files, with pixel/audio data in companion `.resS`/`.resource` files.

Every tool in this app uses the same **in-place binary patch** strategy:

1. New data (pixels, audio, or a color value) is written to the appropriate file
2. Only the target object's metadata bytes are updated, never resized
3. All other objects in the file are left bit-for-bit identical

This avoids a known bug in UnityPy 1.25's `save()` method that causes data loss from unmodified objects when a file is fully re-serialized.

**Meshes** follow the same rule. A replacement's vertex data is appended to the `.resS` (so the vertex count is free to change) and the existing index buffer is reused at its original byte length, zero-padded past the last submesh window — which keeps the rewritten `Mesh` object exactly the size of the one it replaces, and caps a replacement at the original's triangle count. The Meshes tab's opt-in rebuild fallback is the one place this app *does* fully re-serialize a file; because of the data-loss risk noted above, the rebuilt file is verified object-by-object against the original before it is allowed to replace it, and abandoned if anything else moved.

**Texture format mapping:**
| Original format | Written as | Why |
|---|---|---|
| DXT5 / DXT5Crunched | BC7 | Same 1 byte/pixel footprint, better quality |
| DXT1 / DXT1Crunched | DXT1 | Same 0.5 bytes/pixel footprint |
| Others | Unchanged | Safe fallback |

</details>

<details>
<summary>Dependencies (auto-installed)</summary>

```
UnityPy==1.25.0
Pillow
pygame
attrs
lz4
brotli
etcpak
texture2ddecoder
astc-encoder-py
fsspec
dnfile
numpy      # optional: only speeds up the 3D mesh viewer
```

All packages are installed automatically by `SETUP_AND_RUN.bat` or by `app.py` itself on first run. No manual `pip install` is needed.

</details>

---

## Troubleshooting

| Problem | Fix |
|---|---|
| **"UnityPy not installed" on launch** | Run `SETUP_AND_RUN.bat` (not `RUN.bat`) to reinstall dependencies |
| **Game data folder not found** | Open Settings and browse to your `NBA Bounce_Data` folder |
| **Textures look wrong / stretched in game** | Your replacement PNG should match the original's resolution |
| **A replacement logo is cut off in game** | Fixed in v2.1.1 — click Apply Mods again and the sprite crop gets widened to the full canvas. If the texture is a multi-sprite atlas, importing it lists the regions your artwork has to stay inside |
| **Game crashes after applying** | Click Restore Game Files (or Restore Audio) to revert, then check your replacement file is valid |
| **Audio replacement doesn't seem to change anything** | Some clips stream continuously and may ignore replacement; see the note under Audio above |
| **Mod not showing after a game update** | Game updates overwrite `.assets` files; just click Apply Mods / Apply Audio Mods again |
| **A mesh replacement is over budget, invisible, or inside-out** | See the troubleshooting table in [docs/MESH_MODDING_GUIDE.md](docs/MESH_MODDING_GUIDE.md) |
| **Court Colors / Floor Patterns changes not visible** | Changes only appear after fully quitting and relaunching the game |

---

## License

MIT. Do whatever you want with it.

---

## Credits

Built with [UnityPy](https://github.com/K0lb3/UnityPy) by K0lb3, the only Python library that makes Unity asset editing possible.

---

## Development

Co-developed with [Claude](https://claude.ai) by Anthropic. The core Unity asset patching logic was built and debugged iteratively through AI-assisted development.
