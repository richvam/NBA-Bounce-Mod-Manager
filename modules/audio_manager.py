"""
NBA Bounce — Audio Mod Manager
==============================================================================
A self-contained companion module for NBA Bounce Mod Manager.

Import it and drop one button into your top bar:

    from audio_manager import open_audio_window
    ...
    ttk.Button(top, text="🔊 Audio", style="Soft.TButton",
               command=lambda: open_audio_window(self)).pack(side="right", ...)

It reads user/config.json for game_data_path / mods_folder / theme,
so it works whether it's launched from the main app or on its own.

--------------------------------------------------------------------------
WHAT IT DOES
--------------------------------------------------------------------------
• Scans all .assets/level files for Unity AudioClip objects
• Plays / stops / pauses(resume) any clip (pygame.mixer under the hood)
• Lets you tag each clip's language (manual override or best-effort guess)
• Lets you queue a replacement WAV and (experimentally) write it into the game

--------------------------------------------------------------------------
HONEST LIMITATIONS (read before trusting the "Apply" button)
--------------------------------------------------------------------------
• Unity audio is almost always FMOD-Vorbis. There is NO pure-Python FMOD/FSB5
  *encoder* in the UnityPy stack (fmod_toolkit only decodes). So replacement
  works by injecting raw PCM and flipping the clip's format to PCM.
• That is proven to work for clips Unity loads as DecompressOnLoad /
  CompressedInMemory. For fully *streamed* FMOD clips the runtime may ignore
  the PCM bytes — the game won't crash, the sound just won't change.
• Every apply backs up the .assets AND its .resource/.resS first, and REFUSES
  (never corrupts) if the in-place byte size wouldn't match. Restore is one click.
• This module NEVER imports or modifies apply_single_mod() — textures untouched.
==============================================================================
"""

import sys
import os
import subprocess

# ── Optional playback dependency (installed on demand, mirrors app.py) ─────────
def _ensure_pygame():
    try:
        import pygame  # noqa
        return True
    except ImportError:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "pygame",
                            "--upgrade"], capture_output=True, text=True)
            import pygame  # noqa
            return True
        except Exception:
            return False


import json
import shutil
import threading
import tempfile
import ntpath
import wave
import re
import struct
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import UnityPy

# ── Paths / config ─────────────────────────────────────────────────────────────
import app_paths                        # every path in the app comes from here
CONFIG_FILE     = app_paths.user("config.json")
BACKUP_SUFFIX   = ".original_backup"
AUDIO_META_FILE = "audio_mods.json"     # queued replacements
LANGUAGES_FILE  = "languages.json"      # per-clip language tags (manual / detected)

# ── Language support ───────────────────────────────────────────────────────────
# Language is NOT stored in Unity AudioClips, so we derive it from (in priority):
#   1. a manual tag the user sets           (saved in languages.json)
#   2. a best-effort guess from the clip name (locale codes / language words)
LANG_CODE_TO_NAME = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ru": "Russian", "ja": "Japanese",
    "ko": "Korean", "zh": "Chinese", "nl": "Dutch", "pl": "Polish",
    "tr": "Turkish", "ar": "Arabic", "sv": "Swedish", "no": "Norwegian",
    "da": "Danish", "fi": "Finnish", "cs": "Czech", "el": "Greek",
    "he": "Hebrew", "hi": "Hindi", "th": "Thai", "vi": "Vietnamese",
    "uk": "Ukrainian", "ro": "Romanian", "hu": "Hungarian", "id": "Indonesian",
}
NAME_TO_LANG_CODE = {v.lower(): k for k, v in LANG_CODE_TO_NAME.items()}
# common 3-letter ISO 639-2 aliases -> 2-letter code
_THREE_LETTER = {
    "eng": "en", "spa": "es", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de",
    "ita": "it", "por": "pt", "rus": "ru", "jpn": "ja", "kor": "ko", "chi": "zh",
    "zho": "zh", "dut": "nl", "nld": "nl", "pol": "pl", "tur": "tr", "ara": "ar",
    "swe": "sv", "nor": "no", "dan": "da", "fin": "fi", "cze": "cs", "ces": "cs",
    "gre": "el", "ell": "el", "heb": "he", "hin": "hi", "tha": "th", "vie": "vi",
    "ukr": "uk", "ron": "ro", "rum": "ro", "hun": "hu", "ind": "id",
}
# bare 2-letter codes are only trusted if they're unlikely to be plain English
# words — "it"/"no"/"id"/"da"/"he"/"hi" collide with common words, so for those
# we require an explicit locale form (e.g. it-IT) instead.
_SAFE_TWO_LETTER = {
    "en", "es", "fr", "de", "pt", "ru", "ja", "ko", "zh", "nl", "pl",
    "tr", "ar", "sv", "fi", "cs", "el", "uk", "ro", "hu", "th", "vi",
}
UNKNOWN_LANG = "Unknown"


def lang_name(code):
    if not code:
        return UNKNOWN_LANG
    return LANG_CODE_TO_NAME.get(code, code.upper())


def detect_language_from_name(name):
    """Best-effort language code from a clip name, or None if nothing matches."""
    raw = name or ""
    # explicit locale tags first, e.g. en-US / it-IT / pt-BR / es-419.
    # Require an uppercase 2-letter region or 3-digit region so we don't match
    # lowercase English phrases like "no-go".
    m = re.search(r"(?:^|[^A-Za-z])([a-z]{2})[-_](?:[A-Z]{2}|[0-9]{3})(?![A-Za-z])", raw)
    if m and m.group(1) in LANG_CODE_TO_NAME:
        return m.group(1)
    low = raw.lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", low) if t]
    for tok in tokens:
        if tok in NAME_TO_LANG_CODE:       # full word, e.g. "spanish"
            return NAME_TO_LANG_CODE[tok]
        if tok in _THREE_LETTER:           # 3-letter ISO, e.g. "eng"
            return _THREE_LETTER[tok]
        if tok in _SAFE_TWO_LETTER:        # safe bare 2-letter, e.g. "fr"
            return tok
    return None


# ── NBA-branded themes (kept in sync with the main app palette) ────────────────
THEMES = {
    "dark": {
        "bg": "#08101E", "panel": "#0E1828", "ebg": "#060C16", "row_odd": "#0C1525",
        "accent": "#1D428A", "hi": "#C8102E", "hi_active": "#A00D24", "gold": "#FDB927",
        "soft_active": "#2550A8", "text": "#ECF0FF", "sub": "#5B7AA0",
        "dis_bg": "#172030", "dis_fg": "#364E68",
        "border": "#1D428A", "mod_border": "#C8102E", "sash": "#1D428A", "sep": "#C8102E",
    },
    "light": {
        "bg": "#EBF0FC", "panel": "#FFFFFF", "ebg": "#D8E4F5", "row_odd": "#EFF4FD",
        "accent": "#1D428A", "hi": "#C8102E", "hi_active": "#A00D24", "gold": "#8A5A00",
        "soft_active": "#2550A8", "text": "#07142A", "sub": "#3D5C88",
        "dis_bg": "#BDD0E8", "dis_fg": "#7A98B8",
        "border": "#1D428A", "mod_border": "#C8102E", "sash": "#1D428A", "sep": "#C8102E",
    },
}


def _load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"game_data_path": "", "mods_folder": "", "theme": "dark"}


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


# ── Unity helpers (independent of app.py) ──────────────────────────────────────
def find_asset_containers(game_data_path):
    """.assets files + level files — both can hold AudioClip objects."""
    out = []
    for f in sorted(os.listdir(game_data_path)):
        full = os.path.join(game_data_path, f)
        if f.endswith(".assets"):
            out.append(full)
        elif f.startswith("level") and "." not in f:
            out.append(full)
    return out


def _fmt_name(compression_id):
    try:
        from UnityPy.enums import AudioCompressionFormat as AF
        return AF(compression_id).name
    except Exception:
        return str(compression_id)


def extract_audioclips_from_file(assets_path):
    """Fast metadata scan — does NOT decode samples."""
    clips = []
    env = UnityPy.load(assets_path)
    for obj in env.objects:
        if obj.type.name != "AudioClip":
            continue
        try:
            data = obj.read()
            clips.append({
                "name":        getattr(data, "m_Name", "") or f"audio_{obj.path_id}",
                "path_id":     obj.path_id,
                "assets_file": assets_path,
                "channels":    getattr(data, "m_Channels", 0) or 0,
                "frequency":   getattr(data, "m_Frequency", 0) or 0,
                "length":      float(getattr(data, "m_Length", 0.0) or 0.0),
                "bits":        getattr(data, "m_BitsPerSample", 0) or 0,
                "compression": _fmt_name(getattr(data, "m_CompressionFormat", -1)),
            })
        except Exception:
            clips.append({
                "name": f"audio_{obj.path_id}", "path_id": obj.path_id,
                "assets_file": assets_path, "channels": 0, "frequency": 0,
                "length": 0.0, "bits": 0, "compression": "unknown",
            })
    return clips


def decode_clip_samples(assets_path, path_id):
    """Return (filename, audio_bytes) decoded to a playable container (wav/ogg/m4a).

    Reads from the original backup when present so playback always previews the
    true original, not a queued/applied replacement.
    """
    source = (assets_path + BACKUP_SUFFIX
              if os.path.exists(assets_path + BACKUP_SUFFIX) else assets_path)
    env = UnityPy.load(source)
    for obj in env.objects:
        if obj.path_id == path_id and obj.type.name == "AudioClip":
            data = obj.read()
            samples = data.samples            # {filename: bytes}
            if not samples:
                raise ValueError("Clip has no decodable audio data")
            fname, raw = next(iter(samples.items()))
            return fname, raw
    raise ValueError(f"AudioClip path_id {path_id} not found")


def _find_resource_file(assets_path, source_path):
    """Resolve an m_Resource.m_Source string to a real companion file on disk."""
    assets_dir = os.path.dirname(assets_path)
    base = ntpath.basename(source_path)
    name = ntpath.splitext(base)[0]
    for cand in (base, f"{name}.resource", f"{name}.assets.resS", f"{name}.resS"):
        p = os.path.join(assets_dir, cand)
        if os.path.exists(p):
            return p
    return None


def get_companion_resources(assets_path):
    """Companion streamed-audio files for backup (.resource / .resS)."""
    out = []
    for suffix in (".resource", ".resS"):
        c = assets_path + suffix
        if os.path.exists(c):
            out.append(c)
    # some builds name the resource without the .assets stem — catch siblings too
    assets_dir = os.path.dirname(assets_path)
    stem = os.path.basename(assets_path)
    for f in os.listdir(assets_dir):
        if f.endswith(".resource") and f.startswith(os.path.splitext(stem)[0]):
            full = os.path.join(assets_dir, f)
            if full not in out:
                out.append(full)
    return out


def ensure_audio_backup(assets_path):
    """Back up the .assets file AND its companion resource files (once)."""
    made = False
    bak = assets_path + BACKUP_SUFFIX
    if not os.path.exists(bak):
        shutil.copy2(assets_path, bak)
        made = True
    for comp in get_companion_resources(assets_path):
        cbak = comp + BACKUP_SUFFIX
        if not os.path.exists(cbak):
            shutil.copy2(comp, cbak)
    return made


def restore_audio_backup(assets_path):
    bak = assets_path + BACKUP_SUFFIX
    if os.path.exists(bak):
        shutil.copy2(bak, assets_path)
        for comp in get_companion_resources(assets_path):
            cbak = comp + BACKUP_SUFFIX
            if os.path.exists(cbak):
                shutil.copy2(cbak, comp)
        return True
    return False


def _wav_to_pcm(wav_path):
    """Read a WAV file with the stdlib -> (pcm_bytes, channels, framerate, bits)."""
    with wave.open(wav_path, "rb") as w:
        ch  = w.getnchannels()
        fr  = w.getframerate()
        sw  = w.getsampwidth()          # bytes per sample
        nf  = w.getnframes()
        pcm = w.readframes(nf)
    return pcm, ch, fr, sw * 8, nf


def _to_pcm16(pcm_bytes, bits):
    """Normalize 8/16/24/32-bit integer PCM to signed 16-bit PCM (what our FSB5
    encoder writes). Returns pcm16_bytes. Raises on formats we can't safely
    convert with the stdlib alone (e.g. 32-bit float — export 16-bit PCM instead)."""
    if bits == 16:
        return pcm_bytes
    if bits == 8:
        # WAV 8-bit is unsigned (0..255); shift to signed 16-bit.
        out = bytearray(len(pcm_bytes) * 2)
        for i, b in enumerate(pcm_bytes):
            struct.pack_into("<h", out, i * 2, (b - 128) * 256)
        return bytes(out)
    if bits == 24:
        n = len(pcm_bytes) // 3
        out = bytearray(n * 2)
        for i in range(n):
            b0, b1, b2 = pcm_bytes[i * 3: i * 3 + 3]
            val = b0 | (b1 << 8) | (b2 << 16)
            if val & 0x800000:
                val -= 0x1000000
            struct.pack_into("<h", out, i * 2, max(-32768, min(32767, val >> 8)))
        return bytes(out)
    if bits == 32:
        raise ValueError(
            "32-bit WAV is ambiguous (could be int or float) and isn't supported. "
            "Export a 16-bit PCM WAV instead.")
    raise ValueError(f"Unsupported bit depth: {bits}-bit. Export a 16-bit PCM WAV.")


def _build_fsb5_pcm16(pcm_bytes, channels, frequency):
    """
    Wrap raw 16-bit PCM into a minimal single-sample FSB5 (FMOD Sample Bank).

    Unity/FMOD do NOT store raw headerless PCM in .resource files — non-WAV/
    Ogg/M4A AudioClips are FSB5 sample banks (confirmed: Unity3D packs one
    FSB5 per clip into its .resource files — see HearthSim/python-fsb5 docs).
    Writing headerless PCM (as an earlier version of this function did)
    produces a file FMOD can't parse, which is silently ignored -> no sound.

    This layout was verified two ways before use:
      1. Structurally, against the independent HearthSim FSB5 reader.
      2. By round-tripping through the REAL FMOD engine (via fmod_toolkit),
         confirming exact channel/frequency/frame-count/byte-for-byte match.
    """
    if channels not in (1, 2):
        raise ValueError(
            f"FSB5 single-sample header only supports mono/stereo (got {channels} channels)")
    bytes_per_frame = channels * 2
    if len(pcm_bytes) % bytes_per_frame != 0:
        raise ValueError("PCM byte length is not a whole number of frames")
    num_frames = len(pcm_bytes) // bytes_per_frame   # per-channel frame count

    # per-sample bit-packed 64-bit header (LSB-first):
    #   bit0: extraParams | bits1-4: freq index (unused, overridden below)
    #   bit5: twoChannels  | bits6-33: dataOffset/16  | bits34-63: frame count
    extra_params, freq_index, data_offset = 1, 0, 0
    two_channels = 1 if channels == 2 else 0
    sample_header = (
        (extra_params & 0x1)
        | ((freq_index & 0xF) << 1)
        | ((two_channels & 0x1) << 5)
        | ((data_offset & 0xFFFFFFF) << 6)
        | ((num_frames & 0x3FFFFFFF) << 34)
    )
    sample_header_bytes = struct.pack("<Q", sample_header)

    # one FREQUENCY metadata chunk (type=2) so the exact rate always applies,
    # even if it doesn't match FSB5's coarse built-in frequency lookup table
    CHUNK_TYPE_FREQUENCY = 2
    chunk_size = 4
    chunk_header = (0 & 0x1) | ((chunk_size & 0xFFFFFF) << 1) | ((CHUNK_TYPE_FREQUENCY & 0x7F) << 25)
    chunk_bytes = struct.pack("<I", chunk_header) + struct.pack("<I", frequency)

    sample_headers_blob = sample_header_bytes + chunk_bytes
    MODE_PCM16 = 2
    main_header = struct.pack(
        "<4sIIIIII8s16s8s",
        b"FSB5", 1, 1, len(sample_headers_blob), 0, len(pcm_bytes), MODE_PCM16,
        b"\x00" * 8, b"\x00" * 16, b"\x00" * 8,
    )
    return main_header + sample_headers_blob + pcm_bytes


def apply_audio_replacement(assets_path, path_id, replacement_wav):
    """
    EXPERIMENTAL resource-file replacement — mirrors the texture patch philosophy.

    Unity/FMOD .resource files store audio as FSB5 sample banks, not headerless
    PCM, so the replacement is wrapped in a minimal single-sample PCM16 FSB5
    container (see _build_fsb5_pcm16 for how that was verified against a real
    FMOD engine, not just guessed from the spec).

    Strategy (resource-backed clips only):
      1. Restore .resource from backup (clean base), append the FSB5 blob, record offset/size
      2. Flip clip metadata to PCM + new channels/frequency/bits/length
      3. Re-serialize ONLY this object; assert byte size unchanged; splice in place

    Refuses (raises) rather than corrupting if anything wouldn't line up.
    Returns a short status string on success.
    """
    from UnityPy.streams.EndianBinaryWriter import EndianBinaryWriter
    from UnityPy.helpers.TypeTreeHelper import write_typetree
    try:
        from UnityPy.enums import AudioCompressionFormat as AF
        PCM = AF.PCM.value
    except Exception:
        PCM = 0

    pcm, ch, fr, bits, nframes = _wav_to_pcm(replacement_wav)
    pcm = _to_pcm16(pcm, bits)
    bits = 16
    length = (nframes / fr) if fr else 0.0
    fsb5_blob = _build_fsb5_pcm16(pcm, ch, fr)

    env = UnityPy.load(assets_path)
    target = None
    for obj in env.objects:
        if obj.path_id == path_id and obj.type.name == "AudioClip":
            target = obj
            break
    if target is None:
        raise ValueError(f"AudioClip path_id {path_id} not found in file")

    data = target.read()
    res  = getattr(data, "m_Resource", None)
    src  = getattr(res, "m_Source", "") if res else ""
    res_path = _find_resource_file(assets_path, src) if src else None

    if not (res and res_path and os.path.exists(res_path)):
        raise ValueError(
            "This clip stores its audio inline (not in a .resource file). "
            "Replacement only supports resource-backed clips. "
            "Skipped to avoid changing the object's byte size.")

    # 1. clean base from backup, then append the FSB5 blob (not raw PCM)
    res_bak = res_path + BACKUP_SUFFIX
    if os.path.exists(res_bak):
        shutil.copy2(res_bak, res_path)
    offset_before = os.path.getsize(res_path)
    with open(res_path, "ab") as rf:
        rf.write(fsb5_blob)

    # 2. update metadata (all fixed-width fields -> object byte size unchanged)
    res.m_Offset = offset_before
    res.m_Size   = len(fsb5_blob)
    if getattr(data, "m_CompressionFormat", None) is not None:
        data.m_CompressionFormat = PCM
    if getattr(data, "m_Channels", None)      is not None: data.m_Channels      = ch
    if getattr(data, "m_Frequency", None)     is not None: data.m_Frequency     = fr
    if getattr(data, "m_BitsPerSample", None) is not None: data.m_BitsPerSample = bits
    if getattr(data, "m_Length", None)        is not None: data.m_Length        = length
    # Unity's runtime 'm_Format' (sample format): 1 = PCM16 in most versions
    if getattr(data, "m_Format", None) is not None:
        data.m_Format = 1

    # 3. serialize just this object and patch in place
    writer = EndianBinaryWriter(endian=target.reader.endian)
    node   = target._get_typetree_node()
    write_typetree(data, node, writer, env.file)
    new_bytes = writer.bytes
    if len(new_bytes) != target.byte_size:
        raise ValueError(
            f"Serialized size changed ({target.byte_size} -> {len(new_bytes)} b). "
            "Refusing in-place patch to avoid corrupting the file. "
            "(This clip likely uses variable-width fields that shifted.)")

    with open(assets_path, "rb") as f:
        file_bytes = bytearray(f.read())
    file_bytes[target.byte_start: target.byte_start + target.byte_size] = new_bytes
    with open(assets_path, "wb") as f:
        f.write(file_bytes)

    return (f"PCM16 {ch}ch/{fr}Hz wrapped in FSB5, {len(fsb5_blob):,} bytes injected "

            f"(format flipped to PCM)")


# ── Playback engine (pygame.mixer) ─────────────────────────────────────────────
class _Player:
    """One-clip-at-a-time play / pause(resume) / stop wrapper around pygame."""

    def __init__(self):
        self._ok = _ensure_pygame()
        self._inited = False
        self._paused = False
        self._tmpdir = tempfile.mkdtemp(prefix="nbaudio_")
        self._cur = None
        self._counter = 0

    @property
    def available(self):
        return self._ok

    def _init_mixer(self):
        if self._inited:
            return True
        try:
            import pygame
            pygame.mixer.init()
            self._inited = True
        except Exception:
            self._inited = False
        return self._inited

    def play_bytes(self, filename, data):
        if not self._ok or not self._init_mixer():
            raise RuntimeError("Audio device not available (pygame mixer failed to init).")
        import pygame
        ext = os.path.splitext(filename)[1] or ".wav"
        # Release the previous clip: stop AND unload, or Windows keeps the temp
        # file locked and the next write fails with 'Permission denied'.
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass
        try:
            pygame.mixer.music.unload()          # pygame >= 2.0 — frees the file handle
        except Exception:
            pass
        # Write each clip to a fresh unique path so we never touch a locked file.
        self._counter += 1
        path = os.path.join(self._tmpdir, f"clip_{self._counter}{ext}")
        with open(path, "wb") as f:
            f.write(data)
        # best-effort cleanup of the previous temp file (now unlocked)
        if self._cur and self._cur != path and os.path.exists(self._cur):
            try:
                os.remove(self._cur)
            except Exception:
                pass
        self._cur = path
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        self._paused = False

    def pause(self):
        if not self._inited:
            return
        import pygame
        if pygame.mixer.music.get_busy() and not self._paused:
            pygame.mixer.music.pause()
            self._paused = True

    def resume(self):
        if not self._inited:
            return
        import pygame
        if self._paused:
            pygame.mixer.music.unpause()
            self._paused = False

    def stop(self):
        if not self._inited:
            return
        import pygame
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass
        self._paused = False

    def is_playing(self):
        if not self._inited:
            return False
        import pygame
        return bool(pygame.mixer.music.get_busy()) and not self._paused

    def is_paused(self):
        return self._paused

    def cleanup(self):
        try:
            self.stop()
        except Exception:
            pass
        try:
            import pygame
            pygame.mixer.quit()
        except Exception:
            pass
        try:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        except Exception:
            pass



# ── Embeddable audio panel ─────────────────────────────────────────────────────
class AudioManagerFrame(ttk.Frame):
    """
    The whole audio feature as a ttk.Frame, so it can live in a notebook tab
    inside the main app OR inside a standalone Toplevel.

    host: optional main-app instance. If given, we read host.cfg (game path /
          mods folder / theme) and host.C (live palette). When the host switches
          theme it should call self.retheme(host.C).
    """

    def __init__(self, parent, host=None, cfg=None):
        super().__init__(parent)
        self.host = host
        self.cfg  = (getattr(host, "cfg", None) or cfg or _load_config())
        self.C    = (getattr(host, "C", None)
                     or THEMES.get(self.cfg.get("theme", "dark"), THEMES["dark"]))

        self.mods_folder    = self.cfg.get("mods_folder", "")
        self.game_path      = self.cfg.get("game_data_path", "")
        self.all_clips      = []
        self.filtered_clips = []
        self.selected       = None
        self._loading       = False
        self._loaded_once   = False
        self._sort_col      = None
        self._sort_asc      = True

        self.audio_mods  = (_load_json(os.path.join(self.mods_folder, AUDIO_META_FILE), {})
                            if self.mods_folder else {})
        self.languages   = (_load_json(os.path.join(self.mods_folder, LANGUAGES_FILE), {})
                            if self.mods_folder else {})

        self.player   = _Player()
        self._tk_themed = []       # (widget, {option: palette_role})

        self._apply_styles()
        self._build_ui()
        self.after(400, self._poll_playback_state)

    # ── theming ──────────────────────────────────────────────────────────────
    def _tw(self, widget, **roles):
        self._tk_themed.append((widget, roles))
        return widget

    def _apply_styles(self):
        C = self.C
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("Panel.TFrame", background=C["panel"])
        s.configure("Sub.TLabel", background=C["panel"], foreground=C["sub"], font=("Segoe UI", 9))
        s.configure("Brand.TLabel", background=C["panel"], foreground=C["hi"], font=("Segoe UI", 13, "bold"))
        s.configure("BrandSub.TLabel", background=C["panel"], foreground=C["sub"], font=("Segoe UI", 10))
        s.configure("Accent.TButton", background=C["hi"], foreground="#FFFFFF",
                    font=("Segoe UI", 10, "bold"), relief="flat", padding=(8, 6))
        s.map("Accent.TButton", background=[("active", C["hi_active"]), ("disabled", C["dis_bg"])],
              foreground=[("disabled", C["dis_fg"])])
        s.configure("Soft.TButton", background=C["accent"], foreground="#FFFFFF",
                    font=("Segoe UI", 10), relief="flat", padding=(8, 6))
        s.map("Soft.TButton", background=[("active", C["soft_active"]), ("disabled", C["dis_bg"])],
              foreground=[("disabled", C["dis_fg"])])
        s.configure("Treeview", background=C["ebg"], foreground=C["text"],
                    fieldbackground=C["ebg"], rowheight=28, font=("Segoe UI", 10))
        s.configure("Treeview.Heading", background=C["accent"], foreground="#FFFFFF",
                    font=("Segoe UI", 10, "bold"), relief="flat", padding=(4, 6))
        s.map("Treeview.Heading", background=[("active", C["soft_active"])])
        s.map("Treeview", background=[("selected", C["hi"])], foreground=[("selected", "#FFFFFF")])

    def retheme(self, palette):
        """Called by the host when the app theme changes."""
        self.C = palette
        self._apply_styles()
        C = self.C
        try:
            self.configure(style="TFrame")
        except Exception:
            pass
        for widget, roles in self._tk_themed:
            try:
                widget.configure(**{k: C[v] for k, v in roles.items()})
            except Exception:
                pass
        try:
            self.paned.configure(bg=C["sash"])
        except Exception:
            pass
        try:
            self.tree.tag_configure("modded", foreground=C["gold"])
        except Exception:
            pass

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        C = self.C

        # action toolbar
        bar = ttk.Frame(self, style="Panel.TFrame"); bar.pack(fill="x")
        self._tw(bar)
        head = tk.Label(bar, text="🔊  Audio Clips", bg=C["panel"], fg=C["hi"],
                        font=("Segoe UI", 12, "bold"))
        head.pack(side="left", padx=14, pady=8)
        self._tw(head, bg="panel", fg="hi")
        ttk.Button(bar, text="♻ Restore Audio", style="Soft.TButton",
                   command=self._restore_all).pack(side="right", padx=(4, 12), pady=6)
        ttk.Button(bar, text="▶ Apply Audio Mods", style="Accent.TButton",
                   command=self._apply_all).pack(side="right", padx=4, pady=6)

        # status line at the bottom of the tab
        self.status_var = tk.StringVar(value="Open this tab to scan for audio clips.")
        self.status_lbl = tk.Label(self, textvariable=self.status_var, bg=C["panel"],
                                   fg=C["text"], font=("Segoe UI", 9), anchor="w", padx=10, pady=3)
        self.status_lbl.pack(fill="x", side="bottom")
        self._tw(self.status_lbl, bg="panel", fg="text")

        # split pane
        self.paned = tk.PanedWindow(self, orient=tk.HORIZONTAL, sashwidth=6,
                                    sashrelief="flat", bg=C["sash"], borderwidth=0)
        self.paned.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_left(self.paned)
        self._build_right(self.paned)

    def _build_left(self, parent):
        C = self.C
        frame = ttk.Frame(parent, style="Panel.TFrame")
        parent.add(frame, minsize=320, width=460, stretch="always")
        frame.rowconfigure(1, weight=1); frame.columnconfigure(0, weight=1)

        sf = ttk.Frame(frame, style="Panel.TFrame"); sf.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        sf.columnconfigure(1, weight=1)
        ic = tk.Label(sf, text="🔍", bg=C["panel"], fg=C["sub"], font=("Segoe UI", 11))
        ic.grid(row=0, column=0, padx=(0, 4)); self._tw(ic, bg="panel", fg="sub")
        self.search_var = tk.StringVar(); self.search_var.trace_add("write", self._on_search)
        ttk.Entry(sf, textvariable=self.search_var).grid(row=0, column=1, sticky="ew")

        tf = ttk.Frame(frame, style="Panel.TFrame"); tf.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        tf.rowconfigure(0, weight=1); tf.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tf, columns=("name", "len", "lang", "mod", "file"),
                                 show="headings", selectmode="browse")
        for col, txt, w, anc in (("name", "Clip Name", 170, "w"), ("len", "Length", 60, "center"),
                                 ("lang", "Language", 90, "center"),
                                 ("mod", "Mod", 38, "center"), ("file", "File", 130, "w")):
            self.tree.heading(col, text=txt, command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=w, anchor=anc)
        self.tree.tag_configure("modded", foreground=C["gold"])
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-Button-1>", self._on_row_double_click)
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns"); self.tree.configure(yscrollcommand=vsb.set)

        ff = ttk.Frame(frame, style="Panel.TFrame"); ff.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
        fl = tk.Label(ff, text="File:", bg=C["panel"], fg=C["sub"], font=("Segoe UI", 9))
        fl.pack(side="left"); self._tw(fl, bg="panel", fg="sub")
        self.file_filter_var = tk.StringVar(value="All")
        self.file_filter_cb = ttk.Combobox(ff, textvariable=self.file_filter_var, state="readonly", width=16)
        self.file_filter_cb.pack(side="left", padx=(4, 10))
        self.file_filter_cb.bind("<<ComboboxSelected>>", self._on_search)

        ll = tk.Label(ff, text="Language:", bg=C["panel"], fg=C["sub"], font=("Segoe UI", 9))
        ll.pack(side="left"); self._tw(ll, bg="panel", fg="sub")
        self.lang_filter_var = tk.StringVar(value="All")
        self.lang_filter_cb = ttk.Combobox(ff, textvariable=self.lang_filter_var, state="readonly", width=14)
        self.lang_filter_cb.pack(side="left", padx=4)
        self.lang_filter_cb.bind("<<ComboboxSelected>>", self._on_search)

    def _build_right(self, parent):
        C = self.C
        frame = ttk.Frame(parent, style="Panel.TFrame")
        parent.add(frame, minsize=380, stretch="always")
        frame.columnconfigure(0, weight=1)

        self.info_var = tk.StringVar(value="Select an audio clip from the list.")
        il = tk.Label(frame, textvariable=self.info_var, bg=C["panel"], fg=C["text"],
                      font=("Segoe UI", 11, "bold"), anchor="w", padx=14, pady=2)
        il.grid(row=0, column=0, sticky="ew", pady=(12, 0)); self._tw(il, bg="panel", fg="text")
        self.meta_var = tk.StringVar(value="")
        ml = tk.Label(frame, textvariable=self.meta_var, bg=C["panel"], fg=C["sub"],
                      font=("Segoe UI", 9), anchor="w", padx=14)
        ml.grid(row=1, column=0, sticky="ew", pady=(0, 6)); self._tw(ml, bg="panel", fg="sub")

        tp = ttk.Frame(frame, style="Panel.TFrame"); tp.grid(row=2, column=0, sticky="ew", padx=10, pady=4)
        self._tw(tp)
        ttk.Button(tp, text="▶ Play",     style="Accent.TButton", command=self._play).pack(side="left", padx=3)
        ttk.Button(tp, text="🔁 Original", style="Soft.TButton",   command=self._play_original).pack(side="left", padx=3)
        ttk.Button(tp, text="⏸ Pause",    style="Soft.TButton",   command=self._pause).pack(side="left", padx=3)
        ttk.Button(tp, text="⏹ Stop",     style="Soft.TButton",   command=self._stop).pack(side="left", padx=3)
        self.play_state = tk.StringVar(value="")
        ps = tk.Label(tp, textvariable=self.play_state, bg=C["panel"], fg=C["gold"],
                      font=("Segoe UI", 9, "bold"))
        ps.pack(side="left", padx=10); self._tw(ps, bg="panel", fg="gold")

        # per-clip language tag (right-aligned). Setting it overrides the guess
        # and persists to languages.json.
        self.lang_var = tk.StringVar(value="")
        self.lang_cb = ttk.Combobox(tp, textvariable=self.lang_var, state="readonly", width=13,
                                    values=[UNKNOWN_LANG] + [LANG_CODE_TO_NAME[c] for c in
                                            sorted(LANG_CODE_TO_NAME, key=lambda k: LANG_CODE_TO_NAME[k])])
        self.lang_cb.pack(side="right", padx=(4, 6))
        self.lang_cb.bind("<<ComboboxSelected>>", self._set_language)
        lgl = tk.Label(tp, text="Language:", bg=C["panel"], fg=C["sub"], font=("Segoe UI", 9))
        lgl.pack(side="right"); self._tw(lgl, bg="panel", fg="sub")

        rf = ttk.Frame(frame, style="Panel.TFrame"); rf.grid(row=3, column=0, sticky="ew", padx=10, pady=(8, 12))
        self._tw(rf)
        rl = tk.Label(rf, text="Replacement (experimental — WAV, backs up first):",
                      bg=C["panel"], fg=C["sub"], font=("Segoe UI", 9))
        rl.pack(anchor="w", pady=(0, 4)); self._tw(rl, bg="panel", fg="sub")
        self.repl_var = tk.StringVar(value="No replacement queued.")
        rv = tk.Label(rf, textvariable=self.repl_var, bg=C["panel"], fg=C["text"],
                      font=("Segoe UI", 9), anchor="w")
        rv.pack(anchor="w"); self._tw(rv, bg="panel", fg="text")
        rb = ttk.Frame(rf, style="Panel.TFrame"); rb.pack(anchor="w", pady=6); self._tw(rb)
        ttk.Button(rb, text="⬇ Export WAV",        style="Soft.TButton",   command=self._export_wav).pack(side="left", padx=3)
        ttk.Button(rb, text="⬆ Queue Replacement", style="Accent.TButton", command=self._queue_replacement).pack(side="left", padx=3)
        ttk.Button(rb, text="✕ Remove",            style="Soft.TButton",   command=self._remove_replacement).pack(side="left", padx=3)

    # ── loading (lazy) ─────────────────────────────────────────────────────────
    def load(self):
        """Scan for clips the first time the tab is shown. Safe to call repeatedly."""
        # pick up any path/mods changes made in the host's Settings
        self.cfg = getattr(self.host, "cfg", None) or self.cfg
        self.game_path   = self.cfg.get("game_data_path", "")
        self.mods_folder = self.cfg.get("mods_folder", "")
        if self.mods_folder:
            self.audio_mods  = _load_json(os.path.join(self.mods_folder, AUDIO_META_FILE), self.audio_mods)
            self.languages   = _load_json(os.path.join(self.mods_folder, LANGUAGES_FILE), self.languages)
        if self._loaded_once:
            return
        if not self.game_path or not os.path.isdir(self.game_path):
            self.status_var.set("❌ No game data path set. Configure it in ⚙ Settings, then reopen this tab.")
            return
        self._loaded_once = True
        self._load_async()

    def _load_async(self):
        if self._loading:
            return
        self._loading = True
        self.status_var.set("Scanning for audio clips…")
        threading.Thread(target=self._load_worker, daemon=True).start()

    def _load_worker(self):
        try:
            files = find_asset_containers(self.game_path)
            clips = []
            for i, af in enumerate(files):
                self.after(0, lambda n=os.path.basename(af), a=i + 1, b=len(files):
                           self.status_var.set(f"Scanning {n}  ({a}/{b})…"))
                try:
                    clips.extend(extract_audioclips_from_file(af))
                except Exception as e:
                    print(f"Warning: could not read {af}: {e}")
            self.all_clips = clips
            self.after(0, self._on_loaded)
        except Exception as e:
            self.after(0, lambda: self.status_var.set(f"❌ Error: {e}"))
        finally:
            self._loading = False

    def _on_loaded(self):
        names = sorted(set(os.path.basename(c["assets_file"]) for c in self.all_clips))
        self.file_filter_cb["values"] = ["All"] + names
        self.file_filter_var.set("All")
        self._refresh_language_filter()
        self._populate(self.all_clips)
        nmod = len(self.audio_mods)
        extra = f"  ·  {nmod} replacement(s) queued" if nmod else ""
        if not self.player.available:
            extra += "  ·  ⚠ pygame unavailable (playback disabled)"
        self.status_var.set(f"✅ Found {len(self.all_clips)} audio clips.{extra}")

    # ── list / sort / filter ───────────────────────────────────────────────────
    def _key(self, c):
        return f"{os.path.basename(c['assets_file'])}::{c['path_id']}"

    def _clip_lang_code(self, c):
        """Language code for a clip: manual tag > name guess. '' if unknown."""
        code = self.languages.get(self._key(c))
        if code:
            return code
        return detect_language_from_name(c["name"]) or ""

    def _clip_lang_name(self, c):
        return lang_name(self._clip_lang_code(c))

    def _refresh_language_filter(self):
        present = sorted(set(self._clip_lang_name(c) for c in self.all_clips))
        # keep 'Unknown' at the end for tidiness
        present = [p for p in present if p != UNKNOWN_LANG] + \
                  ([UNKNOWN_LANG] if any(p == UNKNOWN_LANG for p in present) else [])
        cur = self.lang_filter_var.get()
        self.lang_filter_cb["values"] = ["All"] + present
        if cur not in (["All"] + present):
            self.lang_filter_var.set("All")

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col, self._sort_asc = col, True
        labels = {"name": "Clip Name", "len": "Length", "lang": "Language",
                  "mod": "Mod", "file": "File"}
        for c, base in labels.items():
            arrow = (" ↑" if self._sort_asc else " ↓") if c == self._sort_col else ""
            self.tree.heading(c, text=base + arrow)
        self._populate(self.filtered_clips)

    def _sorted(self, clips):
        if not self._sort_col:
            return list(clips)
        def kf(c):
            if self._sort_col == "name": return c["name"].lower()
            if self._sort_col == "len":  return c["length"]
            if self._sort_col == "lang": return self._clip_lang_name(c).lower()
            if self._sort_col == "mod":  return 0 if self._key(c) in self.audio_mods else 1
            if self._sort_col == "file": return os.path.basename(c["assets_file"]).lower()
            return ""
        return sorted(clips, key=kf, reverse=not self._sort_asc)

    def _populate(self, clips):
        self.tree.delete(*self.tree.get_children())
        self.filtered_clips = self._sorted(clips)
        for c in self.filtered_clips:
            is_mod = self._key(c) in self.audio_mods
            length = f"{c['length']:.1f}s" if c["length"] else "?"
            self.tree.insert("", "end",
                             values=(c["name"], length, self._clip_lang_name(c),
                                     "✔" if is_mod else "",
                                     os.path.basename(c["assets_file"])),
                             tags=("modded",) if is_mod else ())

    def _on_search(self, *_):
        q  = self.search_var.get().lower()
        ff = self.file_filter_var.get()
        lf = self.lang_filter_var.get()
        self._populate([c for c in self.all_clips
                        if (not q or q in c["name"].lower())
                        and (ff == "All" or os.path.basename(c["assets_file"]) == ff)
                        and (lf == "All" or self._clip_lang_name(c) == lf)])

    # ── selection ───────────────────────────────────────────────────────────────
    def _on_select(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if idx >= len(self.filtered_clips):
            return
        self.selected = c = self.filtered_clips[idx]
        self.info_var.set(c["name"])
        self.meta_var.set(
            f"{c['channels']} ch  ·  {c['frequency']} Hz  ·  {c['bits']}-bit  ·  "
            f"{c['compression']}  ·  {c['length']:.2f}s  ·  {os.path.basename(c['assets_file'])}")
        self.player.stop()
        self.play_state.set("")
        key = self._key(c)
        if key in self.audio_mods:
            self.repl_var.set(f"Queued: {os.path.basename(self.audio_mods[key]['audio_path'])}")
        else:
            self.repl_var.set("No replacement queued.")
        # reflect current language in the selector (without firing a save)
        self.lang_var.set(self._clip_lang_name(c))

    def _on_row_double_click(self, _=None):
        """Double-click a clip in the list -> play it immediately."""
        self._on_select()          # make sure self.selected matches the clicked row
        if self.selected:
            self._play()

    def _set_language(self, _=None):
        """Persist the chosen language for the selected clip."""
        if not self.selected:
            return
        if not self.mods_folder:
            messagebox.showwarning("No mods folder", "Set a mods folder in ⚙ Settings first.", parent=self._tl())
            return
        key  = self._key(self.selected)
        name = self.lang_var.get()
        code = NAME_TO_LANG_CODE.get(name.lower(), "")
        if name == UNKNOWN_LANG or not code:
            self.languages.pop(key, None)     # clear -> falls back to name guess
        else:
            self.languages[key] = code
        _save_json(os.path.join(self.mods_folder, LANGUAGES_FILE), self.languages)
        self._refresh_row()
        self._refresh_language_filter()
        self.status_var.set(f"✅ Language set to {self.lang_var.get()} for '{self.selected['name']}'.")

    # ── playback ─────────────────────────────────────────────────────────────────
    def _tl(self):
        return self.winfo_toplevel()

    def _play(self):
        """Play the clip's replacement if one is queued/applied, else the true
        original. This mirrors the Textures tab's Original/Your Mod split —
        without it, Play always showed the original even after you'd applied
        a new replacement, which looked like the new file wasn't loading."""
        if not self.selected:
            messagebox.showinfo("No selection", "Select a clip first.", parent=self._tl()); return
        if not self.player.available:
            messagebox.showwarning("Playback unavailable",
                "pygame isn't installed / couldn't load.\n\nInstall it with:\n"
                f"  {sys.executable} -m pip install pygame", parent=self._tl()); return
        if self.player.is_paused():
            self.player.resume(); return
        c = self.selected
        key = self._key(c)
        mod = self.audio_mods.get(key)
        if mod and os.path.exists(mod.get("audio_path", "")):
            self._play_from_file(mod["audio_path"], label="▶ playing (your replacement)")
        else:
            self._play_decoded(c, label="▶ playing")

    def _play_original(self):
        """Always plays the true original, even if a replacement is queued/applied —
        for A/B comparison."""
        if not self.selected:
            messagebox.showinfo("No selection", "Select a clip first.", parent=self._tl()); return
        if not self.player.available:
            messagebox.showwarning("Playback unavailable",
                "pygame isn't installed / couldn't load.\n\nInstall it with:\n"
                f"  {sys.executable} -m pip install pygame", parent=self._tl()); return
        if self.player.is_paused():
            self.player.resume(); return
        self._play_decoded(self.selected, label="▶ playing (original)")

    def _play_decoded(self, clip, label):
        self.play_state.set("decoding…")
        def worker():
            try:
                fname, data = decode_clip_samples(clip["assets_file"], clip["path_id"])
                self.after(0, lambda: self._start_play(fname, data, label))
            except Exception as e:
                self.after(0, lambda: (self.play_state.set(""),
                                       messagebox.showerror("Playback failed", str(e), parent=self._tl())))
        threading.Thread(target=worker, daemon=True).start()

    def _play_from_file(self, path, label):
        self.play_state.set("loading…")
        def worker():
            try:
                with open(path, "rb") as f:
                    data = f.read()
                self.after(0, lambda: self._start_play(os.path.basename(path), data, label))
            except Exception as e:
                self.after(0, lambda: (self.play_state.set(""),
                                       messagebox.showerror("Playback failed", str(e), parent=self._tl())))
        threading.Thread(target=worker, daemon=True).start()

    def _start_play(self, fname, data, label="▶ playing"):
        try:
            self.player.play_bytes(fname, data)
            self.play_state.set(label)
        except Exception as e:
            self.play_state.set("")
            messagebox.showerror("Playback failed", str(e), parent=self._tl())

    def _pause(self):
        if self.player.is_paused():
            self.player.resume()
        else:
            self.player.pause()

    def _stop(self):
        self.player.stop()
        self.play_state.set("")

    def _poll_playback_state(self):
        try:
            if self.player.is_paused():
                self.play_state.set("⏸ paused (▶ to resume)")
            elif self.player.is_playing():
                self.play_state.set("▶ playing")
            elif self.play_state.get() in ("▶ playing", "⏸ paused (▶ to resume)"):
                self.play_state.set("■ finished")
        except Exception:
            pass
        self.after(400, self._poll_playback_state)

    # ── replacement ──────────────────────────────────────────────────────────────
    def _export_wav(self):
        if not self.selected:
            messagebox.showinfo("No selection", "Select a clip first.", parent=self._tl()); return
        c = self.selected
        path = filedialog.asksaveasfilename(defaultextension=".wav",
               filetypes=[("WAV", "*.wav"), ("Original container", "*.*")],
               initialfile=c["name"] + ".wav", parent=self._tl())
        if not path:
            return
        try:
            fname, data = decode_clip_samples(c["assets_file"], c["path_id"])
            with open(path, "wb") as f:
                f.write(data)
            self.status_var.set(f"✅ Exported '{c['name']}' → {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e), parent=self._tl())

    def _queue_replacement(self):
        if not self.selected:
            messagebox.showinfo("No selection", "Select a clip first.", parent=self._tl()); return
        if not self.mods_folder:
            messagebox.showwarning("No mods folder", "Set a mods folder in ⚙ Settings first.", parent=self._tl()); return
        c = self.selected
        path = filedialog.askopenfilename(filetypes=[("WAV audio", "*.wav")],
               title="Select replacement WAV", parent=self._tl())
        if not path:
            return
        try:
            with wave.open(path, "rb") as w:
                _ = w.getnframes()
        except Exception as e:
            messagebox.showerror("Not a valid WAV",
                f"Couldn't read that file as WAV:\n{e}\n\nExport a 16-bit PCM WAV from your editor.",
                parent=self._tl()); return
        os.makedirs(self.mods_folder, exist_ok=True)
        dest = os.path.join(self.mods_folder,
                            f"{os.path.basename(c['assets_file'])}__{c['path_id']}__{c['name']}.wav")
        shutil.copy2(path, dest)
        key = self._key(c)
        self.audio_mods[key] = {"name": c["name"], "assets_file": c["assets_file"],
                                "path_id": c["path_id"], "audio_path": dest}
        _save_json(os.path.join(self.mods_folder, AUDIO_META_FILE), self.audio_mods)
        self.repl_var.set(f"Queued: {os.path.basename(dest)}")
        self._refresh_row()
        self.status_var.set(f"✅ Queued replacement for '{c['name']}'. Click '▶ Apply Audio Mods' to write it in.")

    def _remove_replacement(self):
        if not self.selected:
            return
        key = self._key(self.selected)
        if key not in self.audio_mods:
            messagebox.showinfo("No replacement", "Nothing queued for this clip.", parent=self._tl()); return
        mod = self.audio_mods.pop(key)
        try:
            if os.path.exists(mod["audio_path"]):
                os.remove(mod["audio_path"])
        except Exception:
            pass
        _save_json(os.path.join(self.mods_folder, AUDIO_META_FILE), self.audio_mods)
        self.repl_var.set("No replacement queued.")
        self._refresh_row()
        self.status_var.set(f"✅ Removed queued replacement for '{self.selected['name']}'.")

    def _refresh_row(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if idx >= len(self.filtered_clips):
            return
        c = self.filtered_clips[idx]
        is_mod = self._key(c) in self.audio_mods
        length = f"{c['length']:.1f}s" if c["length"] else "?"
        self.tree.item(sel[0],
                       values=(c["name"], length, self._clip_lang_name(c),
                               "✔" if is_mod else "",
                               os.path.basename(c["assets_file"])),
                       tags=("modded",) if is_mod else ())

    # ── apply / restore ──────────────────────────────────────────────────────────
    def _apply_all(self):
        if not self.audio_mods:
            messagebox.showinfo("No replacements", "You haven't queued any audio replacements.", parent=self._tl()); return
        if not messagebox.askyesno("Apply Audio Mods (experimental)",
            f"Write {len(self.audio_mods)} audio replacement(s) into the game?\n\n"
            "⚠ EXPERIMENTAL: audio replacement wraps your WAV as 16-bit PCM in a\n"
            "minimal FSB5 sample bank (the format Unity/FMOD actually store audio\n"
            "in), verified against a real FMOD engine round-trip. It works for\n"
            "clips Unity decompresses on load; for fully streamed clips the game\n"
            "may keep the original sound (no crash).\n\n"
            "Original .assets AND .resource files are backed up first.\n"
            "Use '♻ Restore Audio' to revert everything.\n\nContinue?", parent=self._tl()):
            return
        self.player.stop()
        applied, errors, backed = 0, [], []
        for key, mod in self.audio_mods.items():
            af, wav, pid = mod["assets_file"], mod["audio_path"], mod["path_id"]
            if not os.path.exists(af):
                errors.append(f"{mod['name']}: assets file missing"); continue
            if not os.path.exists(wav):
                errors.append(f"{mod['name']}: replacement WAV missing"); continue
            if ensure_audio_backup(af):
                backed.append(os.path.basename(af))
            try:
                apply_audio_replacement(af, pid, wav)
                applied += 1
            except Exception as e:
                errors.append(f"{mod['name']}: {e}")
        msg = f"✅ Applied {applied}/{len(self.audio_mods)} audio mod(s)."
        if backed:
            msg += f"  Backed up: {', '.join(sorted(set(backed)))}."
        if errors:
            msg += f"  ⚠ {len(errors)} issue(s)."
            messagebox.showwarning("Some clips were skipped",
                "These clips couldn't be patched in place (originals untouched):\n\n"
                + "\n".join(errors[:12]), parent=self._tl())
        self.status_var.set(msg)

    def _restore_all(self):
        if not self.game_path:
            messagebox.showwarning("No game path", "No game data path in config.", parent=self._tl()); return
        restorable = [f for f in find_asset_containers(self.game_path)
                      if os.path.exists(f + BACKUP_SUFFIX)]
        if not restorable:
            messagebox.showinfo("No backups", "No audio backups found — nothing to restore.", parent=self._tl()); return
        if not messagebox.askyesno("Restore Audio",
            f"Restore {len(restorable)} original file(s) (and their .resource files)?\n"
            "Your queued replacement WAVs are NOT deleted.\n\nContinue?", parent=self._tl()):
            return
        self.player.stop()
        n = sum(1 for f in restorable if restore_audio_backup(f))
        self.status_var.set(f"✅ Restored {n} file(s). Queued replacements still saved — re-apply anytime.")

    # ── lifecycle ────────────────────────────────────────────────────────────────
    def shutdown(self):
        try:
            self.player.cleanup()
        except Exception:
            pass


# ── Standalone window wrapper (kept for launching audio on its own) ────────────
class AudioManagerWindow(tk.Toplevel):
    def __init__(self, master=None, cfg=None):
        super().__init__(master)
        cfg = cfg or _load_config()
        C = THEMES.get(cfg.get("theme", "dark"), THEMES["dark"])
        self.title("NBA Bounce — Audio Mod Manager")
        self.geometry("1180x720")
        self.minsize(980, 600)
        self.configure(bg=C["bg"])
        self.frame = AudioManagerFrame(self, host=None, cfg=cfg)
        self.frame.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(150, self.frame.load)

    def _close(self):
        try:
            self.frame.shutdown()
        except Exception:
            pass
        self.destroy()


def open_audio_window(master=None, cfg=None):
    if cfg is None and master is not None and hasattr(master, "cfg"):
        cfg = master.cfg
    return AudioManagerWindow(master, cfg)


if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()
    win = open_audio_window(root)
    win.protocol("WM_DELETE_WINDOW", lambda: (win._close(), root.destroy()))
    root.mainloop()
