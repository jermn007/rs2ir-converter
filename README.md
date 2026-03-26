# RS2IR Converter

Convert **Rocksmith 2014 CDLC** (`.psarc`) files into **Immerrock** custom song packages for Meta Quest.

Supports Lead, Rhythm, and Bass guitar tracks with full tuning, tempo-map, and section data. Lyrics are exported when present. Album art is converted automatically.

---

## Requirements

| Dependency | Install |
|---|---|
| Python 3.10+ | [python.org](https://python.org) |
| mido | `pip install mido` |
| Pillow | `pip install pillow` |
| pycryptodome | `pip install pycryptodome` |
| vgmstream-cli | See below |

### vgmstream-cli setup

vgmstream converts the WEM audio inside PSARCs to OGG. Without it, audio is skipped but everything else still converts.

1. Download the latest release from [github.com/vgmstream/vgmstream/releases](https://github.com/vgmstream/vgmstream/releases)
2. Extract **the entire zip** (not just the `.exe`) into the same folder as `rs2_to_immerrock.py`

All `.dll` files must be present alongside `vgmstream-cli.exe`.

### CDLC sources

Custom songs (CDLCs) can be downloaded from [ignition4.customsforge.com](https://ignition4.customsforge.com).

---

## Installation

```bash
git clone https://github.com/jermn007/rs2ir-converter.git
cd rs2ir-converter
pip install -r requirements.txt
```

Place `vgmstream-cli.exe` and its DLLs in the same folder.

---

## Usage

### GUI (recommended)

Double-click `rs2_to_immerrock.py`, or run:

```bash
python rs2_to_immerrock.py
```

- **Select Files** — pick individual `.psarc` files
- **Select Folder** — pick a folder; all `.psarc` files inside are queued
- **Output Folder** — where Immerrock song folders are written (default: `~/ImmerrockCustomSongs`)
- Click **Convert** — progress bar tracks each song; the log shows per-song detail

### Command line

```bash
# Single file
python rs2_to_immerrock.py song.psarc

# Single file, custom output
python rs2_to_immerrock.py song.psarc ./output/

# Entire folder of PSARCs
python rs2_to_immerrock.py /path/to/cdlcs/ /path/to/output/
```

Already-converted songs (output folder contains `.mid` files) are skipped automatically, so re-running on a folder only processes new additions.

### Output structure

Each PSARC produces one folder inside the output directory:

```
Artist - Song Title/
    GGLead.mid        ← lead guitar (if present)
    GGRhythm.mid      ← rhythm guitar (if present)
    GGBass.mid        ← bass guitar (if present)
    Song.ogg          ← audio (requires vgmstream)
    Cover.jpg         ← album art (requires Pillow)
    Info.txt          ← metadata
    Sections.txt      ← section markers for Phrase Refiner
    Lyrics.txt        ← lyrics (empty placeholder if none)
```

Only tracks present in the PSARC are generated.

---

## How It Works

### 1. PSARC Extraction

A `.psarc` is a container archive. The table of contents (TOC) is encrypted with **AES-256-CFB** using the publicly-known RS2 PSARC key. After decryption the TOC lists every internal file; content blocks are zlib-compressed and decompressed on demand. No temporary files are written to disk.

### 2. Arrangement Parsing

Modern CDLCs store note data as binary `.sng` files inside the PSARC (`songs/bin/generic/*.sng`). Older CDLCs fall back to XML arrangement files.

**SNG decryption** uses a separate **AES-256 custom counter mode**: each 16-byte block is XOR'd with `AES_ECB(key, IV+i)` where the IV (embedded at bytes 8–23 of the file) increments by one per block in big-endian carry fashion. After decryption the payload is a 4-byte uncompressed length followed by zlib-compressed binary data.

**SNG binary parsing** reads the decompressed data sequentially through 14 typed sections:

| Section | Used for |
|---|---|
| BPM | Beat/tempo map → MIDI tempo track |
| Chords | Chord template table (fret positions per string) |
| Vocals | Lyrics text and timestamps |
| Sections | Named song sections → `Sections.txt` |
| Arrangements | Per-difficulty note arrays |
| Metadata | Song length, average tempo, tuning offsets |

All difficulty levels in the Arrangements section are **unioned** together (deduplicated by time + string + fret rounded to 5 ms) to produce the densest possible note chart, equivalent to expert difficulty throughout.

**Tuning** is read from the Metadata section as semitone offsets applied to each string's standard open-string MIDI note.

### 3. MIDI Generation

One Type-1 MIDI file is produced per arrangement (Lead, Rhythm, Bass).

**Track 0 — Tempo map**

A `set_tempo` event is written at every beat boundary using the actual inter-beat interval from the SNG beat map. A pre-roll tempo event at tick 0 covers any silence before the first beat, so MIDI tick 0 aligns exactly with OGG position 0.

**Track 1 — Notes**

RS2 uses one string per channel:

| Channel | String | Standard tuning |
|---|---|---|
| 0 | Low E (string 5) | E2 |
| 1 | A (string 4) | A2 |
| 2 | D (string 3) | D3 |
| 3 | G (string 2) | G3 |
| 4 | B (string 1) | B3 |
| 5 | High e (string 0) | E4 |

Bass uses channels 0–3 only.

MIDI note number = open-string note + fret + tuning offset. Chords are expanded from the chord template table into one note-on per played string, all at the same tick. A 32nd-note gap is enforced between consecutive same-pitch notes to prevent sustain bars from visually merging separate strums.

A **chord mode trigger** (Channel 15, note 30, zero-duration) is emitted at every note-on tick, matching the Immerrock/EoF MIDI spec so that chord diagrams render correctly.

**Channel 15 — Note effects and finger placement**

Additional zero-duration note-on events on Channel 15 carry per-note metadata. Velocity encodes the string: `channel × 5 + 1`.

| Note | Effect |
|---|---|
| 12 | Palm mute |
| 13 | Dead note (fret-hand mute) |
| 14 | Harmonic |
| 15 | Hammer-on / pull-off |
| 17 | Tapping |
| 18 | Stroke down |
| 19 | Stroke up |
| 20 | Slide |
| 30 | Chord mode trigger (every note-on tick) |
| 31–35 | Finger placement: Index, Middle, Ring, Little, Thumb |

Finger signals (31–35) are emitted for chord notes, which carry finger data from the RS2 chord template. Individual notes rarely have explicit finger assignments in RS2 CDLC data.

**Pitch bend**

- **Slides** — a 16-step linear pitch-bend sweep is emitted on the note's channel over the full sustain duration, from neutral (0) to the target semitone offset. Scale: +1280 MIDI units per semitone, matching the Immerrock reference value.
- **Vibrato** — a sinusoidal pitch-bend sweep at 5 Hz / ±1 semitone is emitted for the duration of notes flagged with vibrato in the RS2 data. Slides take priority if both flags are present.
- Pitch bend resets to neutral at the end of each affected note.

**Timing correction** — RS2 beat timestamps are absolute seconds from the start of the audio. `_time_to_ticks` interpolates linearly between beat boundaries to place each note at the correct fractional-beat tick position.

### 4. Audio Conversion

The largest WEM file in the PSARC (main audio, not preview) is extracted and piped through `vgmstream-cli` to produce `Song.ogg`.

### 5. Text Files

| File | Contents |
|---|---|
| `Info.txt` | Artist, Title, Album, Year, Genre, BPM, per-track tuning, chart delay |
| `Sections.txt` | Timestamped section names from the SNG, used by Immerrock's Phrase Refiner |
| `Lyrics.txt` | One line per vocal event in `M:SS.mmm "text"` format; placeholder if none |

### 6. Album Art

The DDS texture from the PSARC is converted to JPEG at up to 512 × 512 using Pillow.

---

## Known Limitations

- **Finger placement on single notes** — RS2 CDLC charters rarely assign explicit finger data to individual (non-chord) notes, so finger signals are only emitted for chord notes where the data is present
- **Thumb visualization** — note 35 (Thumb) is not yet visualized in Immerrock (per the developer); the signal is emitted but has no in-game effect currently
- **Vocals** in RS2 CDLCs rarely include beat timing; `Lyrics.txt` is generated but may be empty
- **Drop / open tunings** that go below MIDI note 0 or above 127 are clamped

---

## Attribution

This tool was built by reverse-engineering publicly documented formats. Key references:

- **[Editor on Fire (EoF)](https://github.com/raynebc/editor-on-fire)** by Raymond Cooke — source of the Immerrock MIDI format specification (`src/ir.c`, `src/ir.h`), including channel assignments, velocity conventions, and Channel 15 hand-mode events
- **[vgmstream](https://github.com/vgmstream/vgmstream)** — WEM → OGG audio conversion
- **[RocksmithToolkit](https://github.com/rscustom/rocksmith-custom-song-toolkit)** — reference for SNG binary layout (`Sng2014File.cs`) and SNG/PSARC decryption keys
- **[Immerrock](https://immerrock.com)** — the VR guitar game this targets; custom song format documented at immerrock.com/custom-song-quick-guide
- **[pycryptodome](https://pycryptodome.readthedocs.io)**, **[mido](https://mido.readthedocs.io)**, **[Pillow](https://python-pillow.org)** — Python libraries

---

## License

MIT — see [LICENSE](LICENSE)
