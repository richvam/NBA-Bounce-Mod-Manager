# NBA Bounce Mod Manager

> A Windows desktop app for modding **NBA Bounce** (Unity 6): textures, audio, court floors, court colors, and gameplay sliders, all from one tool, without ever touching a hex editor.

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

The app opens on **Home**, with a card for each tool: Textures, Audio, Floor Patterns, Gameplay Sliders, and Court Colors. Clicking a card jumps to that tool. The tabs across the top (Home, Textures, Audio, Floor Patterns, Gameplay Sliders, Court Colors) are always visible too, so you never have to come back to Home just to switch tools.

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

- **Apply Texture Mods**: writes every queued texture replacement into the game in one pass. The affected `.assets` and `.resS` files are backed up automatically the first time (never overwritten after that), so this is always safe to re-run, including after a game update, when it'll instantly bring all your mods back.
- **Restore Textures**: rolls every modded texture file back to its original state. Your replacement PNGs are kept, so you can re-apply them at any time.

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

```
nba-bounce-mod-manager/
├── app.py                              # Main app: Home / Textures / Audio tabs, Settings
├── audio_manager.py                    # Audio tab: playback, language tags, replacements
├── sprite_crop.py                      # Widens baked sprite crops so replacements aren't cut off
├── floor_patterns.py                   # Floor Patterns tool
├── slider_tab.py / slider_manager.py   # Gameplay Sliders tool
├── sliders_catalog.json / presets.json # Slider definitions and difficulty presets
├── save_tab.py / save_manager.py       # Saves tool: unlock cosmetics via the .sav
├── unlockables_catalog.json            # Auto-generated: cached unlockable ids from level1
├── SETUP_AND_RUN.bat                   # First-time setup + launch
├── RUN.bat                             # Quick launch (after first setup)
├── config.json                         # Auto-generated: saved paths and theme preference
└── README.md
```

**Your Mods Folder** (location you choose in Settings):

```
<mods_folder>/
├── mods.json               # Maps each texture to its replacement PNG
├── audio_mods.json         # Maps each audio clip to its replacement WAV
├── languages.json          # Manually-tagged audio clip languages
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
| **A replacement logo is cut off in game** | Fixed in v2.1.1 — click Apply Texture Mods again and the sprite crop gets widened to the full canvas. If the texture is a multi-sprite atlas, importing it lists the regions your artwork has to stay inside |
| **Game crashes after applying** | Click Restore Textures (or Restore Audio) to revert, then check your replacement file is valid |
| **Audio replacement doesn't seem to change anything** | Some clips stream continuously and may ignore replacement; see the note under Audio above |
| **Mod not showing after a game update** | Game updates overwrite `.assets` files; just click Apply Texture Mods / Apply Audio Mods again |
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
