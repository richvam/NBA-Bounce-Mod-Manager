# 🏀 NBA Bounce Texture Mod Manager

> A Windows desktop app for replacing textures in **NBA Bounce** (Unity 6) — browse, preview, import, and apply custom PNG textures without ever touching a hex editor.

![Platform](https://img.shields.io/badge/platform-Windows-blue?logo=windows)
![Python](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)
![Unity](https://img.shields.io/badge/Unity-6000.0.37f1-black?logo=unity)
![Game](https://img.shields.io/badge/game-NBA%20Bounce%20(Steam)-red)
![License](https://img.shields.io/badge/license-MIT-green)

---

## ✨ Features

| Feature | Description |
|---|---|
| 🖼️ Texture browser | Browse all ~2,000+ `Texture2D` assets across every game file |
| 🔍 Search & filter | Live search by name; filter by `.assets` source file |
| 📋 Sortable list | Sort by name, resolution, mod status, or file — click any column header |
| 👁️ Side-by-side preview | See the original and your replacement at the same time |
| ⬇️ Export original | Save any texture as PNG for editing in your image editor |
| 📦 Batch export | Ctrl+click to select multiple textures and export them all as a ZIP |
| ⬆️ Import replacement | Import a PNG replacement for any texture |
| ▶️ Apply all mods | Write all replacements into the game in one click, with automatic backup |
| ↶ Revert in game | Undo a single texture without restoring all originals |
| ♻️ Restore originals | Roll back every modded file to its original state |
| 📦 Export mod pack | Bundle all your mods into a shareable `.zip` |
| 📥 Import mod pack | Install a friend's mod pack in one step |
| 🔍 Auto-detect game | Automatically finds your NBA Bounce Steam install on first launch |
| 🌙☀️ Dark / Light mode | Full NBA-branded dark and light themes, switchable in Settings |
| 📊 Version display | Shows game version and Unity engine version in the status bar |

---

## 🚀 Getting Started

### Requirements

- **Windows 10 / 11**
- **Python 3.9 or newer** — download from [python.org](https://www.python.org/downloads/)
  - ⚠️ During install, check **"Add Python to PATH"**
- **NBA Bounce** installed via Steam

### First-Time Setup

1. Download or clone this repository
2. Double-click **`SETUP_AND_RUN.bat`**

That's it. The script installs all Python dependencies automatically and launches the app.

### Subsequent Launches

Double-click **`RUN.bat`** — it skips the install step if everything is already in place.

---

## 📖 How to Use

### 1 — Configure Paths

On first launch the app will try to **auto-detect** your NBA Bounce Steam install. If it succeeds, you can skip this step entirely.

If auto-detect doesn't find the game, click **⚙ Settings** and set:

| Setting | What to enter |
|---|---|
| **Game Data Folder** | The `NBA Bounce_Data` folder inside your Steam install<br>e.g. `C:\SteamLibrary\steamapps\common\NBA BOUNCE\NBA Bounce_Data\` |
| **Mods Folder** | Any folder where you want your replacement PNGs stored<br>e.g. `C:\Users\You\Documents\NBABounceMods` |

Click **Save & Reload** and the texture list will populate.

---

### 2 — Browse & Find Textures

The left panel lists every `Texture2D` asset found across all game files.

- **Search box** — type any part of a texture name to filter the list live
- **Filter by file** — narrow down to a specific `.assets` source file
- **Column sort** — click any column header to sort; click again to reverse
  - `Texture Name` — alphabetical
  - `Size` — by pixel area (largest first)
  - `Mod` — modded textures float to the top
  - `File` — groups by source file
- **Multi-select** — hold **Ctrl** or **Shift** and click to select multiple textures for batch operations
- Textures with an active mod show a **✔** in the Mod column and are highlighted in **gold**

---

### 3 — Preview Textures

Click any texture in the list to load a preview on the right:

| Panel | What it shows |
|---|---|
| **Original** *(blue border)* | The unmodified texture, always read from the original backup |
| **Your Mod** *(red border)* | Your replacement PNG, if one has been imported |

The info bar below the previews shows the texture name, resolution, compression format, and which `.assets` file it comes from.

---

### 4 — Export an Original

1. Select a texture in the list
2. Click **⬇ Export Original**
3. Save the PNG anywhere you like
4. Open it in Photoshop, GIMP, Aseprite, or any image editor

> **Tip:** Your replacement PNG does *not* need to be the same resolution, but matching it avoids any in-game stretching.

**Batch export:** Select multiple textures with Ctrl+click, then click **📦 Batch Export** to save them all to a single ZIP archive.

---

### 5 — Import a Replacement

1. Select the texture you want to replace in the list
2. Click **⬆ Import Replacement**
3. Choose your edited PNG

The replacement is copied to your Mods Folder and registered in `mods.json`. The game file is **not changed yet**.

---

### 6 — Apply Mods to the Game

Click **▶ Apply All Mods** when you're ready.

- The app backs up all affected `.assets` and `.resS` files automatically before the first write (only once — never overwrites an existing backup)
- A progress bar shows each texture as it is applied
- All replacements are written in a single pass

> You can re-run **▶ Apply All Mods** as many times as you like — after a game update, just click it again and all your mods come back instantly.

---

### 7 — Revert a Single Texture

If you want to undo just one mod without touching the others:

1. Select the texture
2. Click **↶ Revert in Game**

The original texture bytes are patched back into the game file. Your mod PNG and its entry in `mods.json` are **kept** — use **▶ Apply All Mods** to re-apply it later.

---

### 8 — Restore All Originals

Click **♻ Restore Originals** to roll back every game file to its unmodified state.

- All backed-up `.assets` and `.resS` files are restored
- Your mod PNGs are **not deleted**
- You can re-apply all mods at any time with **▶ Apply All Mods**

---

### 9 — Mod Packs (Share with Friends)

**Exporting a pack:**

1. Set up your mods as normal
2. Click **📦 Export Pack**
3. Save the `.zip` — share it anywhere (Discord, GitHub Releases, etc.)

**Importing a pack:**

1. Click **📥 Import Pack**
2. Select the received `.zip`
3. Click **▶ Apply All Mods** to write the textures into your game

Mod packs use relative filenames so they work on any machine regardless of where the game is installed.

---

## ⚙️ Settings

Open **⚙ Settings** from the top bar at any time.

| Option | Description |
|---|---|
| **Game Data Folder** | Path to `NBA Bounce_Data` |
| **Mods Folder** | Where your PNG replacements are stored |
| **🔍 Auto-Detect Game** | Re-runs Steam library scan to find the game |
| **🌙 Dark Mode** | Arena Night — deep navy with NBA blue & red accents |
| **☀️ Light Mode** | Broadcast Day — clean white with NBA blue & red accents |

Theme changes apply live; your choice is saved automatically.

---

## 📁 File Structure

```
nba-bounce-mod-manager/
├── app.py                  # Main application
├── SETUP_AND_RUN.bat       # First-time setup + launch
├── RUN.bat                 # Quick launch (after first setup)
├── config.json             # Auto-generated: saved paths and theme preference
└── README.md
```

**Your Mods Folder** (location you choose in Settings):

```
<mods_folder>/
├── mods.json               # Maps each texture to its replacement PNG
└── *.png                   # Your replacement textures
```

> **Don't delete `mods.json`** — it's how the app knows which PNG belongs to which in-game texture.

---

## 🔧 Technical Notes

<details>
<summary>How textures are patched (click to expand)</summary>

NBA Bounce uses Unity 6 (`6000.0.37f1`). Textures are stored across multiple `.assets` files, with pixel data in companion `.resS` files.

The app uses an **in-place binary patch** strategy:

1. New pixel data is appended to the `.resS` file
2. Only the target texture's metadata bytes are updated in the `.assets` file (width, height, format, stream offset/size)
3. All other objects in the file are left bit-for-bit identical

This avoids a known bug in UnityPy 1.25's `save()` method that causes ~6 KB of data loss from unmodified objects.

**Format mapping:**
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
attrs
lz4
brotli
etcpak
texture2ddecoder
astc-encoder-py
fsspec
```

All packages are installed automatically by `SETUP_AND_RUN.bat` or by `app.py` itself on first run. No manual `pip install` is needed.

</details>

---

## 🛠️ Troubleshooting

| Problem | Fix |
|---|---|
| **"UnityPy not installed" on launch** | Run `SETUP_AND_RUN.bat` (not `RUN.bat`) to reinstall dependencies |
| **Game data folder not found** | Open Settings → click 🔍 Auto-Detect Game, or browse manually to `NBA Bounce_Data` |
| **Textures look wrong / stretched in game** | Your replacement PNG should match the original's resolution |
| **Game crashes after applying** | Click ♻ Restore Originals to revert, then check your PNG is a valid RGBA image |
| **Version shows "unknown"** | Make sure the Game Data Folder path is set correctly and `globalgamemanagers` exists in it |
| **Mod not showing after game update** | Game updates overwrite `.assets` files — just click ▶ Apply All Mods again |
| **Import Pack says "skipped"** | The mod pack was made on a different version of the game; the texture may have moved to a different `.assets` file |

---

## 📜 License

MIT — do whatever you want with it.

---

## 🙏 Credits

Built with [UnityPy](https://github.com/K0lb3/UnityPy) by K0lb3 — the only Python library that makes Unity asset editing possible.

---

## 🤖 Development

Co-developed with [Claude](https://claude.ai) by Anthropic. The core Unity texture patching logic was built and debugged iteratively through AI-assisted development.
