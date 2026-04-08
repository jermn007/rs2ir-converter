#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║         Rocksmith 2014 PSARC  →  Immerrock Converter                ║
║                                                                      ║
║  Usage:                                                              ║
║    GUI mode:     python rs_to_immerrock.py                         ║
║    Single file:  python rs_to_immerrock.py song.psarc [output/]    ║
║    Folder:       python rs_to_immerrock.py cdlcs/ [output/]        ║
║                                                                      ║
║  Requirements:                                                       ║
║    pip install mido pillow pycryptodome                              ║
║    vgmstream-cli.exe + DLLs  (for WEM→OGG audio conversion)         ║
║    → Download: https://github.com/vgmstream/vgmstream/releases       ║
║      Extract the full zip alongside this script.                    ║
╚══════════════════════════════════════════════════════════════════════╝
"""

__version__ = '1.2.1'

import os, sys, zlib, struct, json, math, subprocess, shutil, tempfile, re
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import soundfile as _sf
    _SOUNDFILE_OK = True
except Exception:
    _SOUNDFILE_OK = False

try:
    import mido
except ImportError:
    sys.exit("ERROR: 'mido' not installed. Run: pip install mido")

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("WARNING: 'pillow' not installed. Album art conversion will be skipped.")
    print("         Run: pip install pillow")

# ═══════════════════════════════════════════════════════════════
#  PSARC EXTRACTION
# ═══════════════════════════════════════════════════════════════

# RS PSARCs (official and CDLC) are AES-256-CFB encrypted after the
# 32-byte header. This key is publicly known (extracted from the game).
_RS_AES_KEY = bytes.fromhex(
    'C53DB23870A1A2F71CAE64061FDD0E1157309DC85204D4C5BFDF25090DF2572C'
)

# SNG arrangement binaries use a separate AES-256 key (PC platform only).
_RS_SNG_KEY_PC = bytes.fromhex(
    'CB648DF3D12A16BF71701414E69619EC171CCA5D2A142E3E59DE7ADDA18A3A30'
)


def _decrypt_rs_psarc(raw: bytes, toc_size: int) -> bytes:
    """Decrypt the TOC portion of an RS PSARC.
    Only bytes 32..toc_size are AES-256-CFB encrypted; file data blocks
    at toc_size onwards are stored as plain zlib-compressed blocks."""
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise RuntimeError(
            "pycryptodome is required for RS PSARC decryption.\n"
            "  Run: pip install pycryptodome"
        )
    iv = b'\x00' * 16
    cipher = AES.new(_RS_AES_KEY, AES.MODE_CFB, iv=iv, segment_size=128)
    decrypted_toc = cipher.decrypt(raw[32:toc_size])
    return raw[:32] + decrypted_toc + raw[toc_size:]


def _decrypt_rs_sng(data: bytes) -> bytes:
    """Decrypt and decompress an RS PC SNG file into raw binary chart data.

    File layout:
        [4B magic=0x4A LE][4B platform (3=PC)][16B AES-IV][encrypted payload]
    The payload = zlib-compress([4B uncompressed-length][binary chart data]).
    Encryption: AES-256 custom block-counter — each 16-byte block is XOR'd
    with AES_ECB(SNG_KEY_PC, IV+i) where IV increments by 1 per block
    (big-endian carry from the last byte), starting from IV in the file.
    """
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise RuntimeError(
            "pycryptodome is required for SNG decryption.\n"
            "  Run: pip install pycryptodome"
        )
    if len(data) < 28:
        raise ValueError("SNG data too short")
    magic = struct.unpack_from('<I', data, 0)[0]
    if magic != 0x4A:
        raise ValueError(f"Not a valid SNG file (magic={magic:#x})")

    iv  = bytearray(data[8:24])   # AES IV embedded in file header
    enc = data[24:]                # encrypted payload (includes trailing signature)

    ecb       = AES.new(_RS_SNG_KEY_PC, AES.MODE_ECB)
    decrypted = bytearray()
    for i in range(0, len(enc), 16):
        block     = enc[i:i + 16]
        keystream = ecb.encrypt(bytes(iv))
        decrypted.extend(b ^ k for b, k in zip(block, keystream))
        # Increment IV: big-endian +1 with carry from last byte
        for j in range(15, -1, -1):
            iv[j] = (iv[j] + 1) & 0xFF
            if iv[j] != 0:
                break

    # First 4 bytes of decrypted data = original uncompressed length (unused);
    # the rest is zlib-compressed binary.  decompressobj stops at stream end.
    d = zlib.decompressobj()
    return d.decompress(bytes(decrypted[4:]))


def _parse_sng_binary(data: bytes, arr_type: str = 'lead') -> dict:
    """Parse a decrypted+decompressed RS SNG binary into an arrangement dict.

    Returns the same structure as parse_arrangement() so the MIDI pipeline
    works transparently with either source.

    Binary layout (little-endian, '010 Editor' type sizes: long=4, ulong=4,
    short=2, byte=1, float=4, double=8):
        BPM_SECTION → PHRASE_SECTION → CHORD_SECTION → CHORD_NOTES_SECTION
        → VOCAL_SECTION [+ symbol sections if vocals] → PHRASE_ITERATION_SECTION
        → PHRASE_EXTRA_INFO → NLD_SECTION → ACTION → EVENT → TONE → DNA
        → SECTIONS → ARRANGEMENT_SECTION → METADATA
    """
    pos = 0

    def r_u32():
        nonlocal pos
        v = struct.unpack_from('<I', data, pos)[0]; pos += 4; return v

    def r_i32():
        nonlocal pos
        v = struct.unpack_from('<i', data, pos)[0]; pos += 4; return v

    def r_i16():
        nonlocal pos
        v = struct.unpack_from('<h', data, pos)[0]; pos += 2; return v

    def r_f32():
        nonlocal pos
        v = struct.unpack_from('<f', data, pos)[0]; pos += 4; return v

    def r_u8():
        nonlocal pos
        v = data[pos]; pos += 1; return v

    def r_i8():
        nonlocal pos
        v = struct.unpack_from('<b', data, pos)[0]; pos += 1; return v

    def r_str(n):
        nonlocal pos
        raw = data[pos:pos + n]; pos += n
        return raw.split(b'\x00')[0].decode('utf-8', errors='replace')

    def skip(n):
        nonlocal pos
        pos += n

    # ── BPM_SECTION  (BPM<size=16>: float+short+short+long+long) ──
    beats = []
    for _ in range(r_i32()):
        t    = r_f32()
        meas = r_i16()
        skip(2 + 8)   # Beat(short) + PhraseIteration(long) + Mask(long)
        beats.append({'time': t, 'measure': int(meas)})

    # ── PHRASE_SECTION  (PHRASE<size=44>) ────────────────────────
    skip(r_i32() * 44)

    # ── CHORD_SECTION  (CHORD<size=72>: ulong+byte[6]+byte[6]+long[6]+char[32]) ─
    chord_templates = []
    for _ in range(r_i32()):
        skip(4)                                  # ulong Mask
        frets   = [r_u8() for _ in range(6)]    # byte[6] Frets (255 = not played)
        fingers = [r_u8() for _ in range(6)]    # byte[6] Fingers (0=none,1=index..5=thumb)
        skip(24)                                 # Notes (6×float, unused)
        name    = r_str(32)                      # char[32] chord name (e.g. "Emin", "D5/A")
        chord_templates.append({'frets': frets, 'fingers': fingers, 'name': name})

    # ── CHORD_NOTES_SECTION  (CHORD_NOTES<size=2376>) ────────────
    skip(r_i32() * 2376)

    # ── VOCAL_SECTION  (VOCAL<size=60>: float+long+float+char[48]) ──
    vocals = []
    n_voc = r_i32()
    for _ in range(n_voc):
        t      = r_f32()
        note   = r_i32()
        length = r_f32()
        lyric  = r_str(48)
        vocals.append({'time': t, 'note': note, 'length': length, 'text': lyric})

    if n_voc > 0:
        skip(r_i32() * 32)   # SYMBOLS_HEADER_SECTION  (8 longs each)
        skip(r_i32() * 144)  # SYMBOLS_TEXTURE_SECTION
        skip(r_i32() * 44)   # SYMBOL_DEFINITION_SECTION

    # ── PHRASE_ITERATION_SECTION  (PHRASE_ITERATION<size=24>) ────
    skip(r_i32() * 24)

    # ── PHRASE_EXTRA_INFO_BY_LEVEL_SECTION  (<size=16>) ──────────
    skip(r_i32() * 16)

    # ── N_LINKED_DIFFICULTY_SECTION  (variable) ──────────────────
    for _ in range(r_i32()):
        skip(4)             # LevelBreak (long)
        skip(r_i32() * 4)  # NLD_Phrase[PhraseCount]

    # ── ACTION_SECTION  (ACTION<size=260>: float+char[256]) ──────
    skip(r_i32() * 260)

    # ── EVENT_SECTION  (EVENT<size=260>) ─────────────────────────
    skip(r_i32() * 260)

    # ── TONE_SECTION  (TONE<size=8>: float+long) ─────────────────
    skip(r_i32() * 8)

    # ── DNA_SECTION  (DNA<size=8>: float+long) ───────────────────
    skip(r_i32() * 8)

    # ── SECTIONS  (SECTION<size=88>: char[32]+long+float+float+long+long+byte[36]) ─
    sections = []
    for _ in range(r_i32()):
        name = r_str(32)
        skip(4)            # Number
        t    = r_f32()     # StartTime
        skip(4 + 8 + 36)   # EndTime + PhraseIter IDs + StringMask
        sections.append({'name': name, 'time': t})

    # ── ARRANGEMENT_SECTION  (variable) ──────────────────────────
    NOTE_MASK_CHORD      = 0x0002
    NOTE_MASK_MUTE       = 0x0008   # FretHandMute → dead note
    NOTE_MASK_HARMONIC   = 0x0020
    NOTE_MASK_PALM_MUTE  = 0x0040
    NOTE_MASK_HAMMER     = 0x0200   # HammerOn
    NOTE_MASK_PULLOFF    = 0x0400
    NOTE_MASK_SLIDE      = 0x0800
    NOTE_MASK_TAP        = 0x4000
    all_arrs = []   # list of (difficulty, notes)
    for _ in range(r_i32()):
        difficulty = r_i32()            # Difficulty (long)
        skip(r_i32() * 28)     # ANCHOR_SECTION         (ANCHOR<size=28>)
        skip(r_i32() * 12)     # ANCHOR_EXTENSION       (ANCHOR_EXTENSION<size=12>)
        skip(r_i32() * 20)     # FINGERPRINT_SECTION 1  (FINGERPRINT<size=20>)
        skip(r_i32() * 20)     # FINGERPRINT_SECTION 2

        arr_notes = []
        for _ in range(r_i32()):   # NOTES_SECTION
            note_mask  = r_u32()   # ulong NoteMask (4 bytes)
            skip(4 + 4)            # NoteFlags + Hash
            t          = r_f32()   # Time
            string_idx = r_u8()    # StringIndex
            fret_id    = r_u8()    # FretId (255 = not played)
            skip(2)                # AnchorFretId + AnchorWidth
            chord_id   = r_i32()   # ChordId
            skip(4 + 8 + 4 + 6)   # ChordNotesId, PhraseIds, FingerPrints, IterNotes
            slide_to   = r_i8()    # SlideTo: target fret, -1 = no slide
            skip(2)                # SlideUnpitchTo, LeftHand
            skip(1)                # Tap (already captured via NOTE_MASK_TAP)
            pick_dir   = r_u8()    # PickDirection: 0=down, 1=up
            skip(2)                # Slap, Pluck
            vibrato    = r_i16()   # Vibrato (non-zero = has vibrato)
            sustain    = r_f32()   # Sustain
            skip(4)                # MaxBend (float, unused — individual steps are in BEND_DATA)
            bend_count = r_i32()
            bend_data  = []
            for _ in range(bend_count):
                b_time = r_f32()   # absolute time (same scale as note time)
                b_step = r_f32()   # bend amount in semitones
                skip(4)            # Unk3(short) + Unk4(byte) + Unk5(byte)
                bend_data.append((b_time, b_step))

            effects = set()
            if note_mask & NOTE_MASK_PALM_MUTE: effects.add('palm_mute')
            if note_mask & NOTE_MASK_MUTE:      effects.add('dead')
            if note_mask & NOTE_MASK_HARMONIC:  effects.add('harmonic')
            if note_mask & (NOTE_MASK_HAMMER | NOTE_MASK_PULLOFF): effects.add('hammer')
            if note_mask & NOTE_MASK_TAP:       effects.add('tap')
            if note_mask & NOTE_MASK_SLIDE:     effects.add('slide')
            if pick_dir == 1: effects.add('stroke_up')   # down-strum is default; omit it

            if note_mask & NOTE_MASK_CHORD:
                if 0 <= chord_id < len(chord_templates):
                    tmpl = chord_templates[chord_id]
                    # SNG uses index 0 = low E for both chord templates and individual
                    # notes. Flip to RS convention (0 = high e) for build_midi().
                    tmpl_width = 4 if arr_type == 'bass' else 6
                    chord_name = tmpl.get('name', '')
                    for s, f in enumerate(tmpl['frets']):
                        if f != 255 and s < tmpl_width:
                            rs_str = (tmpl_width - 1) - s  # flip to RS string convention
                            arr_notes.append({'time': t, 'sustain': sustain,
                                              'string': rs_str, 'fret': f,
                                              'finger': tmpl['fingers'][s],
                                              'effects': effects,
                                              'vibrato': vibrato,
                                              'slide_semitones': 0,
                                              'bend_data': bend_data,
                                              'chord_name': chord_name})
            elif fret_id != 255:
                # slide_semitones: signed semitone offset at end of sustain
                slide_st = (slide_to - fret_id) if 0 <= slide_to <= 127 else 0
                # SNG individual notes use StringIndex 0=low E for both guitar and bass.
                # Flip to RS convention (0=high e) so build_midi() sees consistent values.
                tmpl_w = 4 if arr_type == 'bass' else 6
                note_str = (tmpl_w - 1) - string_idx
                arr_notes.append({'time': t, 'sustain': sustain,
                                  'string': note_str, 'fret': fret_id,
                                  'finger': 0, 'effects': effects,
                                  'vibrato': vibrato,
                                  'slide_semitones': slide_st,
                                  'bend_data': bend_data,
                                  'chord_name': ''})

        skip(r_i32() * 4)   # AverageNotesPerIteration float[]
        skip(r_i32() * 4)   # NotesInIteration1 long[]
        skip(r_i32() * 4)   # NotesInIteration2 long[]
        all_arrs.append((difficulty, arr_notes))

    # Combine all arrangements: each arrangement is the full song at a specific
    # per-phrase difficulty mix.  Take the union — for any (time, string, fret)
    # triple, keep one copy.  Process high→low difficulty so the richest version
    # (full chord with name, correct finger/effects) wins the deduplication.
    seen_keys: set[tuple] = set()
    notes: list[dict] = []
    for _, arr_notes in sorted(all_arrs, key=lambda x: x[0], reverse=True):  # high→low
        for n in arr_notes:
            # Round time to nearest 5 ms to tolerate float imprecision
            k = (round(n['time'] * 200), n['string'], n['fret'])
            if k not in seen_keys:
                seen_keys.add(k)
                notes.append(n)
    notes.sort(key=lambda n: n['time'])

    # ── METADATA ─────────────────────────────────────────────────
    # double×4 + float×2 + byte + char[32] + short + float + long + short[N] + float×2 + long
    song_length  = 0.0
    tuning       = [0] * 6
    if pos + 50 <= len(data):
        skip(32)              # MaxScore, MaxNotesAndChords, ...Real, PointsPerNote (4 doubles)
        skip(4)               # FirstBeatLength
        skip(4)               # StartTime
        skip(1)               # CapoFretId
        skip(32)              # LastConversionDateTime char[32]
        skip(2)               # Part
        song_length  = r_f32()
        string_count = r_i32()
        if 0 < string_count <= 8 and pos + string_count * 2 <= len(data):
            tuning = [r_i16() for _ in range(string_count)]
            # Bass SNG stores tuning low→high (str0=low E); normalize to RS convention
            # (string 0=highest) so build_midi() tuning application is consistent.
            if arr_type == 'bass':
                bass_n = min(4, len(tuning))
                tuning = list(reversed(tuning[:bass_n])) + list(tuning[bass_n:])

    return {
        'arr_type':  arr_type,
        'title':     '',
        'artist':    '',
        'album':     '',
        'year':      '',
        'capo':      0,
        'tuning':    tuning,
        'avg_tempo': 120.0,
        'song_length': song_length,
        'beats':     beats,
        'sections':  sections,
        'notes':     notes,
        'vocals':    vocals,
    }


class PsarcReader:
    """Reads and extracts Rocksmith 2014 PSARC archives."""

    HEADER_SIZE      = 32
    TOC_ENTRY_SIZE   = 30
    BLOCK_SIZE_BYTES = 2   # for block_size <= 65536

    def __init__(self, path: str, verbose: bool = False):
        self.path = path
        self.verbose = verbose
        self.files: dict[str, bytes] = {}  # filename → raw bytes
        self._parse()

    def _read_uint32_be(self, data, offset):
        return struct.unpack_from('>I', data, offset)[0]

    def _read_uint40_be(self, data, offset):
        hi = struct.unpack_from('>I', data, offset)[0]
        lo = data[offset + 4]
        return (hi << 8) | lo

    def _parse(self):
        with open(self.path, 'rb') as f:
            raw = f.read()

        # ── Header ──────────────────────────────────────────────
        magic       = raw[0:4]
        if magic != b'PSAR':
            raise ValueError(f"Not a PSARC file: {self.path}")

        compression    = raw[8:12]
        toc_size       = self._read_uint32_be(raw, 12)
        toc_entry_size = self._read_uint32_be(raw, 16)
        num_files      = self._read_uint32_be(raw, 20)
        block_size     = self._read_uint32_be(raw, 24)

        if self.verbose:
            print(f"    [PSARC] magic={magic}, compression={compression}, "
                  f"num_files={num_files}, block_size={block_size}, "
                  f"toc_size={toc_size}, toc_entry_size={toc_entry_size}")
            print(f"    [PSARC] raw header bytes 32-62: {raw[32:62].hex()}")

        # ── AES decryption (RS PSARCs are encrypted after byte 32) ─
        # Detect encryption: read the first TOC entry's zindex; if it
        # exceeds the plausible max (file_size / block_size), the TOC
        # is encrypted and we must decrypt before parsing.
        toc_start = self.HEADER_SIZE
        probe_zindex = self._read_uint32_be(raw, toc_start + 16)
        plausible_max = len(raw) // max(block_size, 1) + 1
        if probe_zindex > plausible_max:
            if self.verbose:
                print(f"    [PSARC] zindex probe={probe_zindex} > {plausible_max}; "
                      f"TOC is AES-encrypted — decrypting...")
            raw = _decrypt_rs_psarc(raw, toc_size)
            if self.verbose:
                print(f"    [PSARC] decrypted; new zindex probe="
                      f"{self._read_uint32_be(raw, toc_start + 16)}")
        entries = []
        for i in range(num_files):
            base = toc_start + i * toc_entry_size
            md5    = raw[base:base+16]
            zindex = self._read_uint32_be(raw, base + 16)
            length = self._read_uint40_be(raw, base + 20)
            offset = self._read_uint40_be(raw, base + 25)
            entries.append({'md5': md5, 'zindex': zindex, 'length': length, 'offset': offset})

        # ── Block size table ────────────────────────────────────
        block_table_start = toc_start + num_files * toc_entry_size
        # Determine total blocks
        total_blocks = sum(
            math.ceil(e['length'] / block_size) if e['length'] > 0 else 0
            for e in entries
        )
        block_table = []
        for i in range(total_blocks):
            pos = block_table_start + i * 2
            if pos + 2 <= len(raw):
                block_table.append(struct.unpack_from('>H', raw, pos)[0])
            else:
                block_table.append(0)

        # ── Extract each file ────────────────────────────────────
        raw_files = {}
        for e in entries:
            if e['length'] == 0:
                raw_files[e['offset']] = b''
                continue
            num_blocks = math.ceil(e['length'] / block_size)
            out = bytearray()
            pos = e['offset']
            for b in range(num_blocks):
                bidx = e['zindex'] + b
                if bidx < len(block_table):
                    bsize = block_table[bidx]
                else:
                    bsize = 0
                if bsize == 0:
                    # Stored uncompressed (full block or last partial block)
                    chunk_size = min(block_size, e['length'] - len(out))
                    out.extend(raw[pos:pos + chunk_size])
                    pos += chunk_size
                else:
                    chunk = raw[pos:pos + bsize]
                    pos += bsize
                    try:
                        out.extend(zlib.decompress(chunk))
                    except zlib.error:
                        out.extend(chunk)  # fallback: store raw
            raw_files[id(e)] = bytes(out[:e['length']])

        # ── Resolve filenames from manifest ─────────────────────
        # Entry 0 is the manifest; entries 1..N map to paths in manifest order
        file_bytes_list = [raw_files.get(id(e), b'') for e in entries]

        # Entry 0 is the manifest (list of paths, one per line)
        if file_bytes_list:
            manifest_raw = file_bytes_list[0]
            if self.verbose:
                for idx in range(min(3, len(entries))):
                    e2 = entries[idx]
                    print(f"    [PSARC] entry[{idx}]: zindex={e2['zindex']}, "
                          f"length={e2['length']}, offset={e2['offset']}")
                print(f"    [PSARC] block_table[:8]: {block_table[:8]}")
                print(f"    [PSARC] manifest size={len(manifest_raw)} bytes")
                if manifest_raw:
                    print(f"    [PSARC] manifest preview: {manifest_raw[:200]!r}")
            paths = manifest_raw.decode('utf-8', errors='replace').splitlines()
            if self.verbose:
                print(f"    [PSARC] manifest paths ({len(paths)}):")
                for p in paths:
                    print(f"      {p}")
            # Map each subsequent file
            for i, path in enumerate(paths):
                if i + 1 < len(file_bytes_list):
                    self.files[path.strip()] = file_bytes_list[i + 1]

    def list_files(self):
        return list(self.files.keys())

    def get(self, name: str) -> bytes | None:
        return self.files.get(name)

    def find_like(self, pattern: str) -> list[str]:
        """Return paths containing 'pattern' (case-insensitive)."""
        pat = pattern.lower()
        return [k for k in self.files if pat in k.lower()]

    def extract_to(self, output_dir: str):
        """Extract all files to output_dir."""
        for path, data in self.files.items():
            dest = os.path.join(output_dir, path.replace('/', os.sep))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as f:
                f.write(data)


# ═══════════════════════════════════════════════════════════════
#  RS XML PARSING
# ═══════════════════════════════════════════════════════════════

# Standard open-string MIDI notes (low→high) for tuning offsets
# Standard open-string MIDI notes (low→high) for tuning offsets
# Standard guitar: E2 A2 D3 G3 B3 e4 (strings 5→0 in RS)
# RS string 0 = high e, string 5 = low E
GUITAR_OPEN_STANDARD = [40, 45, 50, 55, 59, 64]  # index 0 = low E
BASS_OPEN_STANDARD   = [28, 33, 38, 43]           # index 0 = low E


def parse_arrangement(xml_path: str) -> dict:
    """Parse a Rocksmith 2014 RS arrangement XML into a structured dict."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    def float_attr(el, attr, default=0.0):
        v = el.get(attr)
        return float(v) if v is not None else default

    def int_attr(el, attr, default=0):
        v = el.get(attr)
        return int(v) if v is not None else default

    # ── Metadata ─────────────────────────────────────────────
    title     = root.findtext('Title',     '')
    artist    = root.findtext('ArtistName', root.findtext('Artist', ''))
    album     = root.findtext('AlbumName',  root.findtext('Album', ''))
    year      = root.findtext('AlbumYear',  root.findtext('Year', ''))
    avg_tempo = float(root.findtext('AverageTempo', '120') or '120')
    song_length = float(root.findtext('SongLength', '0') or '0')
    offset    = float(root.findtext('Offset', '0') or '0')  # audio offset (ms)

    # ── Tuning ───────────────────────────────────────────────
    tuning_el = root.find('Tuning')
    if tuning_el is not None:
        tuning = [int_attr(tuning_el, f'string{i}') for i in range(6)]
    else:
        tuning = [0] * 6

    # Check <Arrangement> element first (standard in RS CDLCs)
    arr_el = root.find('Arrangement')
    arr_type = (arr_el.text or '').lower().strip() if arr_el is not None else ''

    # Fallback to <ArrangementType> element
    if not arr_type:
        arr_type_el = root.find('ArrangementType')
        arr_type = (arr_type_el.text or '').lower().strip() if arr_type_el is not None else ''

    # Infer 'vocals' from non-empty <Vocals> block if still unknown
    if not arr_type:
        vocals_check = root.find('Vocals')
        if vocals_check is not None and len(vocals_check) > 0:
            arr_type = 'vocals'
        else:
            arr_type = 'lead'  # default; corrected from PSARC path in convert_psarc

    # ── Ebeats (tempo map) ───────────────────────────────────
    beats = []
    ebeats_el = root.find('Ebeats')
    if ebeats_el is not None:
        for eb in ebeats_el.findall('Ebeat'):
            t    = float_attr(eb, 'time')
            meas = int_attr(eb, 'measure', -1)
            beats.append({'time': t, 'measure': meas})

    # ── Sections ─────────────────────────────────────────────
    sections = []
    sections_el = root.find('Sections')
    if sections_el is not None:
        for sec in sections_el.findall('Section'):
            name      = sec.get('name', '')
            number    = int_attr(sec, 'number')
            start     = float_attr(sec, 'startTime')
            sections.append({'name': name, 'number': number, 'time': start})

    # ── Notes ────────────────────────────────────────────────
    def _xml_effects(el):
        efx = set()
        if int_attr(el, 'palmMute',    0): efx.add('palm_mute')
        if int_attr(el, 'fretHandMute',0): efx.add('dead')
        if int_attr(el, 'harmonic',    0): efx.add('harmonic')
        if int_attr(el, 'hammerOn',    0) or int_attr(el, 'pullOff', 0):
            efx.add('hammer')
        if int_attr(el, 'tap',         0): efx.add('tap')
        if int_attr(el, 'slideTo', -1) >= 0: efx.add('slide')
        if int_attr(el, 'pickDirection', 0) == 1: efx.add('stroke_up')  # down-strum is default
        return efx

    notes = []
    notes_el = root.find('Notes')
    if notes_el is not None:
        for n in notes_el.findall('Note'):
            t        = float_attr(n, 'time')
            sustain  = float_attr(n, 'sustain', 0.0)
            string   = int_attr(n,  'string')    # RS: 0=high e, 5=low E
            fret     = int_attr(n,  'fret')
            ignore   = int_attr(n,  'ignore', 0)
            if ignore:
                continue
            slide_to = int_attr(n, 'slideTo', -1)
            slide_st = (slide_to - fret) if slide_to >= 0 else 0
            notes.append({'time': t, 'sustain': sustain,
                          'string': string, 'fret': fret,
                          'effects': _xml_effects(n), 'vibrato': 0,
                          'slide_semitones': slide_st, 'finger': 0,
                          'bend_data': [], 'chord_name': ''})

    # ── Chords ───────────────────────────────────────────────
    chord_templates = []
    ct_el = root.find('ChordTemplates')
    if ct_el is not None:
        for ct in ct_el.findall('ChordTemplate'):
            frets   = [int_attr(ct, f'fret{i}', -1) for i in range(6)]
            fingers = [int_attr(ct, f'finger{i}', -1) for i in range(6)]
            chord_templates.append({'frets': frets, 'fingers': fingers,
                                    'name': ct.get('chordName', '')})

    chords = []
    chords_el = root.find('Chords')
    if chords_el is not None:
        for ch in chords_el.findall('Chord'):
            t        = float_attr(ch, 'time')
            sustain  = float_attr(ch, 'sustain', 0.0)
            chord_id = int_attr(ch,   'chordId')
            ignore   = int_attr(ch,   'ignore', 0)
            if ignore:
                continue
            chord_efx = _xml_effects(ch)
            # Prefer <ChordNotes> child elements — they carry per-string sustain
            xml_chord_name = chord_templates[chord_id]['name'] if 0 <= chord_id < len(chord_templates) else ''
            chord_notes_el = ch.find('ChordNotes')
            if chord_notes_el is not None and len(chord_notes_el) > 0:
                for cn in chord_notes_el.findall('Note'):
                    s    = int_attr(cn, 'string')
                    f    = int_attr(cn, 'fret')
                    sust = float_attr(cn, 'sustain', sustain)
                    fng  = int_attr(cn, 'leftHand', 0)
                    if f >= 0:
                        notes.append({'time': t, 'sustain': sust,
                                      'string': s, 'fret': f,
                                      'effects': chord_efx, 'vibrato': 0,
                                      'slide_semitones': 0, 'finger': fng,
                                      'bend_data': [], 'chord_name': xml_chord_name})
            elif 0 <= chord_id < len(chord_templates):
                # Fallback: expand from ChordTemplate (no per-string sustain)
                tmpl = chord_templates[chord_id]
                for s in range(6):
                    f   = tmpl['frets'][s]
                    fng = tmpl['fingers'][s]
                    if f >= 0:
                        notes.append({'time': t, 'sustain': sustain,
                                      'string': s, 'fret': f,
                                      'effects': chord_efx, 'vibrato': 0,
                                      'slide_semitones': 0, 'finger': fng,
                                      'bend_data': [], 'chord_name': xml_chord_name})

    # ── Vocals (for Lyrics.txt) ───────────────────────────────
    vocals = []
    vocals_el = root.find('Vocals')
    if vocals_el is not None:
        for v in vocals_el.findall('Vocal'):
            t    = float_attr(v, 'time')
            text = v.get('lyric', '').strip()
            vocals.append({'time': t, 'text': text})

    return {
        'title':       title,
        'artist':      artist,
        'album':       album,
        'year':        year,
        'avg_tempo':   avg_tempo,
        'song_length': song_length,
        'offset':      offset,
        'tuning':      tuning,
        'arr_type':    arr_type,
        'beats':       sorted(beats,   key=lambda x: x['time']),
        'sections':    sorted(sections, key=lambda x: x['time']),
        'notes':       sorted(notes,    key=lambda x: x['time']),
        'vocals':      sorted(vocals,   key=lambda x: x['time']),
    }


# ═══════════════════════════════════════════════════════════════
#  MIDI GENERATION
# ═══════════════════════════════════════════════════════════════

TICKS_PER_BEAT = 480
VELOCITY       = 79   # standard note velocity in EoF


def _time_to_ticks(note_time: float, beats: list[dict]) -> int:
    """Convert absolute time (seconds) to MIDI ticks using the beat map."""
    if not beats:
        return int(note_time * TICKS_PER_BEAT * 2)  # fallback ~120 BPM

    # Find surrounding beats
    for i in range(len(beats) - 1):
        t0, t1 = beats[i]['time'], beats[i+1]['time']
        if t0 <= note_time <= t1:
            fraction = (note_time - t0) / (t1 - t0)
            tick0 = beats[i]['_tick']
            tick1 = beats[i+1]['_tick']
            return int(tick0 + fraction * (tick1 - tick0))

    # Beyond last beat: extrapolate
    if len(beats) >= 2:
        last_two_dt = beats[-1]['time'] - beats[-2]['time']
        if last_two_dt > 0:
            extra_beats = (note_time - beats[-1]['time']) / last_two_dt
            return int(beats[-1]['_tick'] + extra_beats * TICKS_PER_BEAT)

    return beats[-1]['_tick']


def _assign_beat_ticks(beats: list[dict]) -> list[dict]:
    """Assign cumulative tick positions to each beat."""
    if not beats:
        return beats
    beats = sorted(beats, key=lambda b: b['time'])
    beats[0]['_tick'] = 0
    for i in range(1, len(beats)):
        dt_sec = beats[i]['time'] - beats[i-1]['time']
        beats[i]['_tick'] = beats[i-1]['_tick'] + TICKS_PER_BEAT
        # (one tick-per-beat per beat, tempo drives actual timing)
    return beats


def build_midi(arr: dict, track_name: str, is_bass: bool = False) -> mido.MidiFile:
    """
    Convert a parsed RS arrangement dict to a mido MidiFile
    matching the Immerrock format.
    """
    beats  = _assign_beat_ticks(arr['beats'])
    notes  = arr['notes']
    tuning = arr['tuning']

    # ── Pre-roll offset ───────────────────────────────────────
    # Beat times from RS are in absolute audio seconds (t=0 = OGG start).
    # beats[0] may be at e.g. t=10.5s because the WEM has an intro.
    # MIDI tick 0 must equal OGG t=0, so shift all beat ticks forward
    # by the pre-roll duration (using the first beat interval as a proxy
    # for the pre-roll tempo).
    pre_roll_ticks = 0
    if len(beats) >= 2 and beats[0]['time'] > 0.01:
        first_interval_sec = beats[1]['time'] - beats[0]['time']
        if first_interval_sec > 0:
            pre_roll_ticks = round(beats[0]['time'] / first_interval_sec * TICKS_PER_BEAT)
            for b in beats:
                b['_tick'] += pre_roll_ticks

    # ── Open-string MIDI note for each channel ───────────────
    # Channel 0 = lowest string, channel N = Nth-from-bottom string.
    # RS string 0 = high e (index 5 from bottom), string 5 = low E (index 0).
    # Both SNG binary and XML use this convention.
    if is_bass:
        num_strings = 4
        open_base   = BASS_OPEN_STANDARD[:]   # [E1, A1, D2, G2]
        for i in range(num_strings):
            rs_string = (num_strings - 1) - i   # channel i → RS string
            open_base[i] += tuning[rs_string]
    else:
        num_strings = 6
        open_base   = GUITAR_OPEN_STANDARD[:]  # [E2, A2, D3, G3, B3, e4]
        for i in range(num_strings):
            rs_string = (num_strings - 1) - i
            open_base[i] += tuning[rs_string]

    # ── Tempo track ──────────────────────────────────────────
    mid = mido.MidiFile(type=1, ticks_per_beat=TICKS_PER_BEAT)

    tempo_track = mido.MidiTrack()
    mid.tracks.append(tempo_track)

    # Build tempo events from successive beat intervals
    prev_abs_tick = 0
    if len(beats) >= 2:
        first_dt = beats[1]['time'] - beats[0]['time']
        first_tempo = max(1, min(int(first_dt * 1_000_000), 16_777_215))

        # Time signature + pre-roll tempo always at tick 0
        tempo_track.append(mido.MetaMessage('time_signature',
            numerator=4, denominator=4,
            clocks_per_click=24, notated_32nd_notes_per_beat=8,
            time=0))
        tempo_track.append(mido.MetaMessage('set_tempo', tempo=first_tempo, time=0))

        for i in range(len(beats) - 1):
            dt_sec = beats[i+1]['time'] - beats[i]['time']
            if dt_sec <= 0:
                continue
            tempo = max(1, min(int(dt_sec * 1_000_000), 16_777_215))
            abs_tick = beats[i]['_tick']
            delta = abs_tick - prev_abs_tick
            tempo_track.append(mido.MetaMessage('set_tempo', tempo=tempo, time=delta))
            prev_abs_tick = abs_tick
    else:
        # Fallback: use average tempo
        tempo = int(60_000_000 / max(arr['avg_tempo'], 1))
        tempo_track.append(mido.MetaMessage('time_signature',
            numerator=4, denominator=4,
            clocks_per_click=24, notated_32nd_notes_per_beat=8, time=0))
        tempo_track.append(mido.MetaMessage('set_tempo', tempo=tempo, time=0))
    tempo_track.append(mido.MetaMessage('end_of_track', time=0))

    # ── Instrument track ─────────────────────────────────────
    inst_track = mido.MidiTrack()
    mid.tracks.append(inst_track)
    inst_track.name = track_name
    inst_track.append(mido.MetaMessage('track_name', name=track_name, time=0))

    # Pitch-bend scale confirmed by Immerrock dev (Motanum):
    # +1280 raw MIDI units per semitone; neutral = 8192 (mido: 0).
    PB_SEMITONE_UNITS  = 1280

    # Vibrato constants — sinusoidal sweep centred on neutral pitch.
    VIBRATO_RATE_HZ   = 5.0   # oscillations per second
    VIBRATO_AMPLITUDE = 384   # peak deviation in PB units (~0.3 semitone), per dev
    VIBRATO_STEP_SEC  = 1.0 / (VIBRATO_RATE_HZ * 8)                 # 8 steps/cycle

    # ch15 note-effect map: RS effect name → Immerrock MIDI note number
    CH15_EFFECTS = {
        'palm_mute':   12,
        'dead':        13,
        'harmonic':    14,
        'hammer':      15,
        'tap':         17,
        'stroke_down': 18,
        'stroke_up':   19,
        'slide':       20,
    }

    # Collect notes grouped by (channel, midi_note) to detect overlaps.
    # Also keep per-note metadata for ch15 signals, pitch bend, and chord names.
    note_groups: dict[tuple, list] = {}
    per_note_meta: list[tuple] = []  # (channel, on_tick, off_tick_raw, finger, effects, vibrato, note_time, note_end, slide_semitones, bend_data)
    # chord_name_at_tick: first non-empty chord name seen at each on_tick
    chord_name_at_tick: dict[int, str] = {}
    _tick_midi_notes: dict[int, list] = {}  # for music-theory fallback naming
    for note in notes:
        rs_str = note['string']
        fret    = note['fret']
        if rs_str >= num_strings:
            continue
        channel = (num_strings - 1) - rs_str

        midi_note = open_base[channel] + fret
        midi_note = max(0, min(127, midi_note))

        on_tick  = _time_to_ticks(note['time'], beats)
        sustain  = note['sustain']
        if sustain > 0:
            off_tick = _time_to_ticks(note['time'] + sustain, beats)
            note_end = note['time'] + sustain
        else:
            # No sustain: apply a minimum duration so notes render as visible
            # bars. Chords get a half-beat so the chord diagram stays readable;
            # individual notes get a 16th-note to keep fast runs feeling snappy.
            is_chord = bool(note.get('chord_name'))
            min_ticks = TICKS_PER_BEAT // 2 if is_chord else TICKS_PER_BEAT // 4
            off_tick = on_tick + max(1, min_ticks)
            note_end = note['time']

        key = (channel, midi_note)
        if key not in note_groups:
            note_groups[key] = []
        note_groups[key].append([on_tick, off_tick])

        # Collect MIDI notes per tick for chord name fallback computation
        chord_name = note.get('chord_name', '')
        if chord_name and (on_tick not in chord_name_at_tick or not chord_name_at_tick[on_tick]):
            chord_name_at_tick[on_tick] = chord_name
        # Also accumulate notes at each tick for music-theory naming below
        _tick_midi_notes.setdefault(on_tick, []).append(midi_note)

        per_note_meta.append((
            channel, on_tick, off_tick,
            note.get('finger', 0),
            note.get('effects', set()),
            note.get('vibrato', 0),
            note['time'],
            note_end,
            note.get('slide_semitones', 0),
            note.get('bend_data', []),
        ))

    # Music-theory fallback: for ticks with no template name, try to identify
    # power chord shapes from the pitch classes. Checks all pairs of pitch classes
    # present in the chord, so it works even if extra notes are present.
    _NOTE_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']
    for tick, midi_notes in _tick_midi_notes.items():
        if tick in chord_name_at_tick:
            continue  # already named by template
        pcs = sorted(set(n % 12 for n in midi_notes))
        for i in range(len(pcs)):
            for j in range(i + 1, len(pcs)):
                a, b = pcs[i], pcs[j]
                diff = (b - a) % 12
                if diff == 7:   # b is perfect 5th above a → X5 root = a
                    chord_name_at_tick[tick] = _NOTE_NAMES[a] + '5'
                    break
                elif diff == 5:  # b is perfect 4th above a → X5 root = b
                    chord_name_at_tick[tick] = _NOTE_NAMES[b] + '5'
                    break
            if tick in chord_name_at_tick:
                break

    # Cap off_tick to prevent same-pitch overlap.
    # Leave at least a 32nd-note gap so Immerrock treats them as separate events.
    NOTE_GAP = TICKS_PER_BEAT // 8  # 32nd note at 480 TPB = 60 ticks
    # events tuple: (tick, etype, channel, value, velocity)
    # etype: 'on'/'off' = note_on message; 'pb' = pitchwheel (value=pitch, velocity unused)
    events = []
    for (channel, midi_note), grp in note_groups.items():
        grp.sort(key=lambda x: x[0])
        for i in range(len(grp) - 1):
            if grp[i][1] >= grp[i + 1][0]:
                grp[i][1] = max(grp[i][0] + 1, grp[i + 1][0] - NOTE_GAP)
        for on_tick, off_tick in grp:
            events.append((on_tick,  'on',  channel, midi_note, VELOCITY))
            events.append((off_tick, 'off', channel, midi_note, 0))

    # ch15 finger placement (notes 31–35) and note effects (notes 12–20).
    # Velocity encodes which string: channel * 5 + 1
    # ch0->1, ch1->6, ch2->11, ch3->16, ch4->21, ch5->26  (matches EoF convention)
    for channel, on_tick, off_tick, finger, effects, vibrato, note_time, note_end, slide_semitones, bend_data in per_note_meta:
        str_vel = channel * 5 + 1

        if 1 <= finger <= 5:
            fn = 30 + finger  # 31=Index, 32=Middle, 33=Ring, 34=Little, 35=Thumb
            events.append((on_tick, 'mod', 15, fn, str_vel))
            events.append((on_tick, 'off', 15, fn, 0))

        for eff, eff_note in CH15_EFFECTS.items():
            if eff in effects:
                events.append((on_tick, 'mod', 15, eff_note, str_vel))
                events.append((on_tick, 'off', 15, eff_note, 0))

        # Pitch bend — slides, bends, and vibrato are mutually exclusive;
        # slides take priority, then bends, then vibrato.
        duration = note_end - note_time
        if slide_semitones and duration > 0:
            # Slide: linear sweep from neutral to target over the sustain.
            # Final event stays at full value (no reset) so Immerrock can
            # draw the trail endpoint.
            SLIDE_STEPS = 16
            pb_target = max(-8192, min(8191,
                            slide_semitones * PB_SEMITONE_UNITS))
            for i in range(SLIDE_STEPS + 1):
                frac     = i / SLIDE_STEPS
                pb_value = int(pb_target * frac)
                t        = note_time + frac * duration
                tick     = _time_to_ticks(t, beats)
                events.append((tick, 'pb', channel, pb_value, 0))
            # No reset — leave pitch at target value.
        elif bend_data:
            # Bend: follow the RS bend envelope from the SNG BEND_DATA_SECTION.
            # Times in bend_data are absolute (same scale as note_time).
            for b_time, b_step in bend_data:
                pb_value = max(-8192, min(8191,
                               int(b_step * PB_SEMITONE_UNITS)))
                tick = _time_to_ticks(b_time, beats)
                events.append((tick, 'pb', channel, pb_value, 0))
            # Reset to neutral after the note ends
            reset_tick = _time_to_ticks(note_end, beats)
            events.append((reset_tick, 'pb', channel, 0, 0))
        elif vibrato and duration > 0:
            # Vibrato: sinusoidal sweep for the duration of the note.
            t = note_time
            while t <= note_end:
                phase    = (t - note_time) * VIBRATO_RATE_HZ * 2 * math.pi
                pb_value = int(VIBRATO_AMPLITUDE * math.sin(phase))
                tick     = _time_to_ticks(t, beats)
                events.append((tick, 'pb', channel, pb_value, 0))
                t += VIBRATO_STEP_SEC
            # Reset to neutral after the note ends
            reset_tick = _time_to_ticks(note_end, beats)
            events.append((reset_tick, 'pb', channel, 0, 0))

    # Sort order within the same tick (per dev spec):
    #   1. note-offs (prevent overlap with incoming notes)
    #   2. pitch bends (set pitch before note-on fires)
    #   3. real note-ons  (ch 0–14)
    #   4. ch15 modifier note-ons  (zero-length; must follow real notes)
    def _sort_key(e):
        tick, etype, ch, val, vel = e
        if etype == 'off':   order = 0
        elif etype == 'pb':  order = 1
        elif etype == 'on':  order = 2   # real note-on
        else:                order = 3   # 'mod' = ch15 modifier note-on
        return (tick, order)
    events.sort(key=_sort_key)

    # Build sorted list of ticks where note-ons occur (for text meta ordering)
    on_tick_set = sorted({e[0] for e in events if e[1] == 'on' and e[2] < 15})

    # Convert to delta-time messages; insert text meta before note-ons at each tick
    prev_tick = 0
    text_iter = iter(on_tick_set)
    next_text_tick = next(text_iter, None)

    for abs_tick, etype, channel, value, velocity in events:
        # Emit chord/note name text meta just before the first event at this tick
        while next_text_tick is not None and next_text_tick <= abs_tick:
            name = chord_name_at_tick.get(next_text_tick, '')
            if name:
                delta = max(0, next_text_tick - prev_tick)
                inst_track.append(mido.MetaMessage('text', text=name, time=delta))
                prev_tick = next_text_tick
            next_text_tick = next(text_iter, None)

        delta = max(0, abs_tick - prev_tick)
        prev_tick = abs_tick
        if etype == 'pb':
            inst_track.append(mido.Message('pitchwheel',
                channel=channel, pitch=value, time=delta))
        else:
            inst_track.append(mido.Message('note_on',
                channel=channel, note=value,
                velocity=velocity, time=delta))

    inst_track.append(mido.MetaMessage('end_of_track', time=0))
    return mid


# ═══════════════════════════════════════════════════════════════
#  TEXT FILE GENERATORS
# ═══════════════════════════════════════════════════════════════

def build_info_txt(arrangements: list[dict], song_ogg_path: str = None) -> str:
    """Generate Info.txt from the first arrangement's metadata."""
    # Use the first arrangement for metadata
    arr = arrangements[0] if arrangements else {}

    # Determine song duration
    min_sec = '0:00'
    if song_ogg_path and os.path.exists(song_ogg_path):
        try:
            import wave
            # Not a wav — use mido or subprocess fallback
            pass
        except:
            pass
    if arr.get('song_length', 0) > 0:
        total_sec = int(arr['song_length'])
        mins = total_sec // 60
        secs = total_sec % 60
        min_sec = f'{mins}:{secs:02d}'

    # Collect tunings per arrangement type
    lead_tuning    = [0] * 6
    rhythm_tuning  = [0] * 6
    bass_tuning    = [0] * 4

    has_lead   = False
    has_rhythm = False
    has_bass   = False

    for a in arrangements:
        if not a.get('notes'):
            continue  # don't claim an arrangement exists if it has no notes
        t = a.get('arr_type', '').lower()
        if 'lead' in t or t == 'combo':
            lead_tuning = a.get('tuning', [0]*6)
            has_lead = True
        elif 'rhythm' in t or 'alt' in t:
            rhythm_tuning = a.get('tuning', [0]*6)
            has_rhythm = True
        elif 'bass' in t:
            bass_tuning = a.get('tuning', [0]*4)[:4]
            has_bass = True

    lead_tuning_str   = ', '.join(str(x) for x in lead_tuning)
    rhythm_tuning_str = ', '.join(str(x) for x in rhythm_tuning)
    bass_tuning_str   = ', '.join(str(x) for x in bass_tuning)

    bpm = int(round(arr.get('avg_tempo', 120)))
    chart_delay = int(round(arr.get('offset', 0) * -1000))  # ms, sign inverted

    lines = [
        f"Artist={arr.get('artist', '')}",
        f"Title={arr.get('title', '')}",
        f"Album={arr.get('album', '')}",
        f"Year={arr.get('year', '')}",
        f"Genre={arr.get('genre', '')}",
        f"Min:Sec={min_sec}",
        f"BPM={bpm}",
        f"Lead_Fingering={1 if has_lead else 0}",
        f"Rhythm_Fingering={1 if has_rhythm else 0}",
        f"Bass_Fingering={1 if has_bass else 0}",
        f"Lead_Difficulty={5 if has_lead else 0}",
        f"Rhythm_Difficulty={5 if has_rhythm else 0}",
        f"Bass_Difficulty={5 if has_bass else 0}",
        f"Lead_Tuning={lead_tuning_str}",
        f"Rhythm_Tuning={rhythm_tuning_str}",
        f"Bass_Tuning={bass_tuning_str}",
        f"ChartDelay={chart_delay}",
    ]
    return '\r\n'.join(lines)


def _format_timestamp(seconds: float) -> str:
    """Format seconds as M:SS.mmm for Sections.txt."""
    mins = int(seconds) // 60
    secs = seconds - mins * 60
    whole = int(secs)
    frac  = secs - whole
    frac_str = f'{frac:.3f}'[1:]  # e.g. '.791'
    return f'{mins}:{whole}{frac_str}'


def build_sections_txt(sections: list[dict], song_length: float = 0) -> str:
    """Generate Sections.txt from section data."""
    lines = []

    # Leading empty section at 0
    if not sections or sections[0]['time'] > 0:
        lines.append('0:0 ""')

    for sec in sections:
        ts   = _format_timestamp(sec['time'])
        name = sec.get('name', '').lower()
        lines.append(f'{ts} "{name}"')

    # Trailing empty section
    if song_length > 0:
        lines.append(f'{_format_timestamp(song_length)} ""')

    return '\r\n'.join(lines)


def _format_lyric_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS.ff for Lyrics.txt."""
    mins  = int(seconds) // 60
    secs  = int(seconds) % 60
    frac  = seconds - int(seconds)
    frac2 = int(round(frac * 100))
    return f'{mins:02d}:{secs:02d}.{frac2:02d}'


def build_lyrics_txt(vocals: list[dict], song_length: float = 0) -> str:
    """Generate Lyrics.txt from vocal data.

    Each charter phrase becomes its own display line. Two signals end a phrase:
      1. Capitalised word gated by inter-onset interval (IOI) — fires when the
         gap between the *start* of the previous event and the start of this one
         exceeds CAP_IOI_MIN. Using IOI guards against splitting quickly-
         delivered proper nouns: "Dancing Queen" (IOI ≈ 0.89 s) stays together
         while a real phrase break like "Diver → You" (IOI ≈ 1.2 s) splits.
      2. Silence gap ≥ TIME_GAP seconds (event end → next start) — catches
         section breaks and CDLCs with no natural capitalisation boundaries.

    The no-suffix phrase-end marker was removed: many CDLCs mix marked and
    bare words inconsistently, causing Signal 1 to fire on almost every word
    and produce one-word-per-line output regardless of threshold tuning.

    Within a phrase '-' joins the next syllable without a space
    ("Ho-" + "ly" → "Holy"); '+' and bare words keep a normal space.
    MAX_CHARS is a generous safety wrap for truly runaway phrases.
    """
    MAX_CHARS   = 50    # wrap long runs at word boundaries; natural phrases
                        # are typically ≤41 chars so this won't affect them
    TIME_GAP    = 1.5   # seconds — silence-gap signal
    CAP_IOI_MIN = 1.0   # seconds — min inter-onset interval for cap signal

    lines = ['00:00.00 ""']

    phrase_text = ''
    phrase_time = None
    join_next   = False  # True when last event ended with '-'
    last_end    = None   # time + length of last event (silence gap)
    prev_time   = None   # onset time of last event (IOI for cap signal)

    def emit():
        nonlocal phrase_text, phrase_time, join_next
        if phrase_text and phrase_time is not None:
            ts = _format_lyric_timestamp(phrase_time)
            lines.append(f'{ts} "{phrase_text}"')
        phrase_text = ''
        phrase_time = None
        join_next   = False

    for v in vocals:
        raw        = v['text'].strip()
        has_hyphen = raw.endswith('-')
        text       = raw.rstrip('+-').strip()
        if not text:
            continue

        v_end = v['time'] + v.get('length', 0)

        # Signal 2 — silence gap
        if last_end is not None and (v['time'] - last_end) > TIME_GAP:
            emit()

        # Signal 1 — capitalised word, gated by inter-onset interval
        if phrase_text and not join_next and text and text[0].isupper():
            ioi = v['time'] - prev_time if prev_time is not None else 0
            if ioi > CAP_IOI_MIN:
                emit()

        # Accumulate syllable into current phrase
        if phrase_time is None:
            phrase_time = v['time']
            phrase_text = text
        else:
            sep       = '' if join_next else ' '
            candidate = phrase_text + sep + text
            # Safety wrap: only at a word boundary, never mid-hyphen-join
            if not join_next and len(candidate) > MAX_CHARS:
                emit()
                phrase_time = v['time']
                phrase_text = text
            else:
                phrase_text = candidate

        join_next = has_hyphen
        last_end  = v_end
        prev_time = v['time']

    emit()  # flush any remaining

    return '\r\n'.join(lines)


# ═══════════════════════════════════════════════════════════════
#  AUDIO CONVERSION  (WEM → OGG)
# ═══════════════════════════════════════════════════════════════

def find_vgmstream() -> str | None:
    """Locate vgmstream-cli executable."""
    import sys
    if getattr(sys, 'frozen', False):
        # PyInstaller bundle: bundled binaries live in sys._MEIPASS (_internal/).
        # Also check next to the exe for a user-supplied vgmstream.
        search_dirs = [sys._MEIPASS, os.path.dirname(sys.executable)]
    else:
        search_dirs = [os.path.dirname(os.path.abspath(__file__))]
    candidates = [
        os.path.join(d, name)
        for d in search_dirs
        for name in ('vgmstream-cli.exe', 'vgmstream-cli')
    ] + ['vgmstream-cli.exe', 'vgmstream-cli']  # PATH fallback
    for c in candidates:
        if shutil.which(c) or os.path.isfile(c):
            return c
    return None


def convert_wem_to_ogg(wem_path: str, out_path: str, vgmstream: str) -> bool:
    """Convert a WEM file to OGG using vgmstream-cli piped through soundfile.

    vgmstream decodes to PCM WAV on stdout; soundfile re-encodes to OGG
    Vorbis at quality 0.3 (~112 kbps stereo) for compact, good-quality output.
    """
    no_window = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    wav_tmp = None
    try:
        # Step 1: vgmstream decodes WEM → WAV (no looping with -i)
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            wav_tmp = f.name
        result = subprocess.run(
            [vgmstream, '-i', '-o', wav_tmp, wem_path],
            capture_output=True, timeout=120, **no_window
        )
        if result.returncode != 0 or not os.path.exists(wav_tmp) or os.path.getsize(wav_tmp) < 1024:
            print(f"  ✗ vgmstream error (rc={result.returncode})")
            return False

        # Step 2: re-encode WAV → OGG at ~256 kbps (compression_level=0.2)
        if _SOUNDFILE_OK:
            if getattr(sys, 'frozen', False):
                # Frozen exe: DLLs are bundled and isolated — direct call is safe
                with _sf.SoundFile(wav_tmp) as inp:
                    with _sf.SoundFile(out_path, 'w', samplerate=inp.samplerate,
                                       channels=inp.channels, format='OGG',
                                       subtype='VORBIS', compression_level=0.2) as out:
                        for chunk in inp.blocks(blocksize=inp.samplerate * 10,
                                                dtype='float32'):
                            out.write(chunk)
            else:
                # Development: run in a subprocess to isolate native DLL issues
                import json
                script = (
                    f'import soundfile as sf;'
                    f'inp=sf.SoundFile({json.dumps(wav_tmp)});'
                    f'out=sf.SoundFile({json.dumps(out_path)},"w",'
                    f'samplerate=inp.samplerate,channels=inp.channels,'
                    f'format="OGG",subtype="VORBIS",compression_level=0.2);'
                    f'[out.write(c) for c in inp.blocks(blocksize=inp.samplerate*10,dtype="float32")];'
                    f'inp.close();out.close()'
                )
                enc = subprocess.run(
                    [sys.executable, '-c', script],
                    capture_output=True, timeout=120,
                    cwd=tempfile.gettempdir()
                )
                if enc.returncode != 0:
                    err = enc.stderr.decode(errors='replace').strip()
                    print(f"  ✗ OGG encode failed (rc={enc.returncode}){': ' + err if err else ''}")
                    return False
        else:
            # soundfile unavailable — rename WAV as OGG (large but functional)
            print("  ⚠ soundfile not available — audio will be uncompressed")
            shutil.move(wav_tmp, out_path)
            wav_tmp = None

        return os.path.exists(out_path)

    except Exception as e:
        print(f"  ✗ Audio conversion error: {type(e).__name__}: {e}")
        return False
    finally:
        if wav_tmp and os.path.exists(wav_tmp):
            os.unlink(wav_tmp)


# ═══════════════════════════════════════════════════════════════
#  ALBUM ART CONVERSION  (DDS / any → JPG)
# ═══════════════════════════════════════════════════════════════

def convert_art_to_jpg(art_data: bytes, out_path: str) -> bool:
    """Convert album art bytes to Cover.jpg."""
    if not PIL_AVAILABLE:
        return False
    try:
        import io
        img = Image.open(io.BytesIO(art_data))
        rgb = img.convert('RGB')
        rgb.save(out_path, 'JPEG', quality=85)
        return True
    except Exception as e:
        print(f"  ✗ Art conversion error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
#  MAIN CONVERSION PIPELINE
# ═══════════════════════════════════════════════════════════════

def convert_psarc(psarc_path: str, output_dir: str, vgmstream: str | None) -> bool:
    """
    Full pipeline: PSARC → Immerrock song folder.
    Returns True on success.
    """
    print(f"\n{'─'*60}")
    print(f"  Processing: {os.path.basename(psarc_path)}")
    print(f"{'─'*60}")

    # ── 1. Extract PSARC ────────────────────────────────────
    print("  [1/5] Extracting PSARC...")
    try:
        psarc = PsarcReader(psarc_path, verbose=False)
    except Exception as e:
        print(f"  ✗ Failed to read PSARC: {e}")
        return False

    all_paths = psarc.list_files()
    if not all_paths:
        print("  ✗ PSARC appears empty or corrupt.")
        return False

    # ── 2. Parse arrangements ────────────────────────────────
    print("  [2/5] Parsing arrangements...")

    # JSON manifest → song metadata (title, artist, album, year, tempo)
    manifest_paths = [p for p in all_paths
                      if p.endswith('.json') and 'manifest' in p.lower()]
    manifest_meta = {}
    for mp in manifest_paths:
        try:
            mdata = json.loads(psarc.get(mp))
            entries = mdata.get('Entries', {})
            for key, val in entries.items():
                attrs = val.get('Attributes', {})
                if attrs.get('SongName'):
                    manifest_meta.update(attrs)
                    break
        except:
            pass

    arrangements = []
    arr_type_map = {}  # arr_type → arrangement dict

    # ── 2a. SNG binary arrangements (modern CDLCs) ───────────
    sng_paths = [p for p in all_paths if p.endswith('.sng') and 'bin' in p.lower()]
    for sp in sng_paths:
        sp_lower = sp.lower()
        if   'bass'  in sp_lower:                      arr_t = 'bass'
        elif 'rhythm' in sp_lower or 'alt' in sp_lower: arr_t = 'rhythm'
        elif 'vocal'  in sp_lower:                      arr_t = 'vocals'
        elif 'lead'   in sp_lower or 'combo' in sp_lower: arr_t = 'lead'
        else:
            continue  # showlights, etc.
        sng_bytes = psarc.get(sp)
        if not sng_bytes:
            continue
        try:
            chart = _decrypt_rs_sng(sng_bytes)
            arr   = _parse_sng_binary(chart, arr_t)
            # Fill metadata from manifest
            if manifest_meta:
                arr['title']     = manifest_meta.get('SongName',         '')
                arr['artist']    = manifest_meta.get('ArtistName',       '')
                arr['album']     = manifest_meta.get('AlbumName',        '')
                arr['year']      = str(manifest_meta.get('SongYear',     ''))
                arr['genre']     = manifest_meta.get('SongGenre',        '')
                arr['avg_tempo'] = manifest_meta.get('SongAverageTempo', 120)
            arrangements.append(arr)
            arr_type_map[arr_t] = arr
            print(f"    ✓ {arr_t:10s} — {len(arr['notes'])} notes, "
                  f"{len(arr['beats'])} beats  (SNG)")
        except Exception as e:
            print(f"    ✗ SNG parse failed for {sp}: {e}")

    # ── 2b. XML arrangements (fallback / older CDLCs) ────────
    xml_paths = [p for p in all_paths if p.endswith('.xml') and 'arr' in p.lower()]
    for xp in xml_paths:
        xml_bytes = psarc.get(xp)
        if not xml_bytes:
            continue
        with tempfile.NamedTemporaryFile(suffix='.xml', delete=False) as tmp:
            tmp.write(xml_bytes)
            tmp_path = tmp.name
        try:
            arr = parse_arrangement(tmp_path)
            # Override with manifest metadata if available
            if manifest_meta:
                arr['title']     = arr['title']  or manifest_meta.get('SongName', '')
                arr['artist']    = arr['artist'] or manifest_meta.get('ArtistName', '')
                arr['album']     = arr['album']  or manifest_meta.get('AlbumName', '')
                arr['year']      = arr['year']   or str(manifest_meta.get('SongYear', ''))
                arr['genre']     = arr.get('genre') or manifest_meta.get('SongGenre', '')
                arr['avg_tempo'] = arr['avg_tempo'] or manifest_meta.get('SongAverageTempo', 120)

            # Refine arr_type from the PSARC internal path
            xp_lower = xp.lower()
            if 'bass' in xp_lower:
                arr['arr_type'] = 'bass'
            elif 'rhythm' in xp_lower or 'alt' in xp_lower:
                arr['arr_type'] = 'rhythm'
            elif 'vocal' in xp_lower:
                arr['arr_type'] = 'vocals'
            elif 'lead' in xp_lower:
                arr['arr_type'] = 'lead'

            # Skip arr_types already covered by SNG parsing
            if arr['arr_type'] in arr_type_map:
                continue

            arrangements.append(arr)
            arr_type_map[arr['arr_type']] = arr
            print(f"    ✓ {arr['arr_type']:10s} — {len(arr['notes'])} notes, "
                  f"{len(arr['beats'])} beats  (XML)")
        except Exception as e:
            print(f"    ✗ Could not parse {xp}: {e}")
        finally:
            os.unlink(tmp_path)

    if not arrangements:
        print("  ✗ No valid arrangements found.")
        return False

    # Determine song name for output folder
    ref = arrangements[0]
    artist = ref.get('artist') or manifest_meta.get('ArtistName', 'Unknown')
    title  = ref.get('title')  or manifest_meta.get('SongName',   'Unknown')
    folder_name = f"{artist} - {title}".replace('/', '_').replace('\\', '_')
    # Remove characters invalid in folder names
    folder_name = re.sub(r'[<>:"|?*]', '_', folder_name).strip('. ')

    song_dir = os.path.join(output_dir, folder_name)

    # Skip if already converted (has at least one MIDI file)
    if os.path.isdir(song_dir) and any(
            f.endswith('.mid') for f in os.listdir(song_dir)):
        print(f"  → Skipping (already exists): {song_dir}")
        return True

    os.makedirs(song_dir, exist_ok=True)
    print(f"  → Output: {song_dir}")

    # ── 3. Convert audio ────────────────────────────────────
    print("  [3/5] Converting audio (WEM → OGG)...")
    audio_paths = [p for p in all_paths
                   if p.endswith('.wem') and 'preview' not in p.lower()]
    # If multiple WEMs, pick the largest (main audio is always the biggest)
    if len(audio_paths) > 1:
        audio_paths.sort(key=lambda p: len(psarc.files.get(p, b'')), reverse=True)

    ogg_out = os.path.join(song_dir, 'Song.ogg')
    audio_ok = False

    if audio_paths:
        if vgmstream:
            # Extract WEM to temp, convert
            wem_data = psarc.get(audio_paths[0])
            with tempfile.NamedTemporaryFile(suffix='.wem', delete=False) as tmp:
                tmp.write(wem_data)
                wem_tmp = tmp.name
            audio_ok = convert_wem_to_ogg(wem_tmp, ogg_out, vgmstream)
            os.unlink(wem_tmp)
            if audio_ok:
                print(f"    ✓ Audio converted")
            else:
                print(f"    ✗ Audio conversion failed")
        else:
            print("    ✗ vgmstream-cli not found — skipping audio.")
            print("      Place vgmstream-cli.exe next to this script and re-run.")
    else:
        # Try OGG directly (some CDLCs include OGG)
        ogg_paths = [p for p in all_paths
                     if p.endswith('.ogg') and 'preview' not in p.lower()]
        if ogg_paths:
            with open(ogg_out, 'wb') as f:
                f.write(psarc.get(ogg_paths[0]))
            audio_ok = True
            print(f"    ✓ OGG audio extracted directly")
        else:
            print("    ✗ No audio file found in PSARC")

    # ── 4. Convert album art ─────────────────────────────────
    print("  [4/5] Converting album art...")
    art_paths = [p for p in all_paths
                 if any(p.lower().endswith(ext) for ext in ['.dds', '.png', '.jpg'])]
    # Prefer 256×256 art (album_art_256)
    art_paths.sort(key=lambda p: ('256' in p, '512' in p), reverse=True)

    art_ok = False
    if art_paths:
        art_data = psarc.get(art_paths[0])
        art_out  = os.path.join(song_dir, 'Cover.jpg')
        art_ok   = convert_art_to_jpg(art_data, art_out)
        if art_ok:
            print(f"    ✓ Album art converted")
        else:
            print(f"    ✗ Album art conversion failed")
    else:
        print("    ✗ No album art found in PSARC")

    # ── 5. Generate MIDI + text files ───────────────────────
    print("  [5/5] Generating Immerrock files...")

    # Track name mapping
    track_map = {
        'lead':   ('GGLead.mid',   'Lead',   False),
        'rhythm': ('GGRhythm.mid', 'Rhythm', False),
        'alt':    ('GGRhythm.mid', 'Rhythm', False),
        'bass':   ('GGBass.mid',   'Bass',   True),
        'combo':  ('GGLead.mid',   'Lead',   False),
    }

    for a in arrangements:
        atype = a['arr_type']
        if atype == 'vocals':
            continue  # handled via Lyrics.txt
        if not a.get('notes'):
            print(f"    ⚠ {atype}: 0 notes — skipped")
            continue
        key = next((k for k in track_map if k in atype), None)
        if not key:
            continue
        filename, track_name, is_bass = track_map[key]
        try:
            mid = build_midi(a, track_name, is_bass=is_bass)
            mid_path = os.path.join(song_dir, filename)
            mid.save(mid_path)
            print(f"    ✓ {filename}")
        except Exception as e:
            print(f"    ✗ {filename}: {e}")

    # Info.txt
    info = build_info_txt(arrangements, ogg_out if audio_ok else None)
    with open(os.path.join(song_dir, 'Info.txt'), 'w', encoding='utf-8', newline='') as f:
        f.write(info)
    print("    ✓ Info.txt")

    # Sections.txt — use lead arrangement, fall back to any
    sec_arr = arr_type_map.get('lead') or arr_type_map.get('rhythm') or arrangements[0]
    sections = build_sections_txt(sec_arr['sections'], sec_arr.get('song_length', 0))
    with open(os.path.join(song_dir, 'Sections.txt'), 'w', encoding='utf-8', newline='') as f:
        f.write(sections)
    print("    ✓ Sections.txt")

    # Lyrics.txt — use vocals arrangement if present
    vocals_arr = arr_type_map.get('vocals')
    if vocals_arr and vocals_arr['vocals']:
        lyrics = build_lyrics_txt(vocals_arr['vocals'], vocals_arr.get('song_length', 0))
    else:
        lyrics = '00:00.00 ""'
    with open(os.path.join(song_dir, 'Lyrics.txt'), 'w', encoding='utf-8', newline='') as f:
        f.write(lyrics)
    print("    ✓ Lyrics.txt")

    print(f"\n  ✓ Done! → {song_dir}")
    return True


# ═══════════════════════════════════════════════════════════════
#  GUI MODE  (drag and drop / browse)
# ═══════════════════════════════════════════════════════════════

def run_gui():
    try:
        import tkinter as tk
        from tkinter import filedialog, scrolledtext, ttk
    except ImportError:
        print("tkinter not available. Use command-line mode.")
        return
    import threading

    vgmstream = find_vgmstream()

    root = tk.Tk()
    root.title("RS → Immerrock Converter")
    root.geometry("680x540")
    root.configure(bg='#1e1e2e')

    # Window icon (title bar + taskbar)
    _icon_path = os.path.join(
        getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))),
        'icon.ico'
    )
    if os.path.exists(_icon_path):
        root.iconbitmap(_icon_path)

    # Dark title bar — Windows 10 build 18985+ / Windows 11
    try:
        import ctypes
        root.update()
        _hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            _hwnd, 20, ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int)
        )
    except Exception:
        pass  # non-Windows or older Windows — skip silently

    style = ttk.Style()
    style.theme_use('clam')
    style.configure('TButton', background='#7c3aed', foreground='white',
                    font=('Segoe UI', 10, 'bold'), padding=8)
    style.map('TButton', background=[('active', '#6d28d9')])
    style.configure('TProgressbar', troughcolor='#2d2d3f', background='#7c3aed')

    # ── Header ───────────────────────────────────────────────
    tk.Label(root, text='Rocksmith 2014 → Immerrock',
             bg='#1e1e2e', fg='#a78bfa',
             font=('Segoe UI', 16, 'bold')).pack(pady=(16, 2))
    tk.Label(root, text='Convert PSARC files to Immerrock custom songs',
             bg='#1e1e2e', fg='#6b7280',
             font=('Segoe UI', 9)).pack(pady=(0, 4))

    # ── vgmstream status ─────────────────────────────────────
    vgs_text = f"vgmstream: {'✓ Found' if vgmstream else '✗ Not found (place vgmstream-cli.exe here)'}"
    vgs_color = '#4ade80' if vgmstream else '#f87171'
    tk.Label(root, text=vgs_text, bg='#1e1e2e', fg=vgs_color,
             font=('Segoe UI', 8)).pack(pady=(0, 8))

    # ── File list label ───────────────────────────────────────
    frame = tk.Frame(root, bg='#1e1e2e')
    frame.pack(fill='x', padx=20, pady=(0, 4))

    file_var = tk.StringVar(value='No files selected')
    tk.Label(frame, textvariable=file_var, bg='#2d2d3f', fg='#e5e7eb',
             font=('Consolas', 8), anchor='w', justify='left',
             wraplength=600, padx=8, pady=6).pack(fill='x')

    selected_files = []

    def _set_files(files):
        selected_files.clear()
        selected_files.extend(files)
        n = len(files)
        if n == 0:
            file_var.set('No files selected')
        elif n == 1:
            file_var.set(os.path.basename(files[0]))
        else:
            file_var.set(f'{n} PSARC files selected')

    def browse_files():
        files = filedialog.askopenfilenames(
            title='Select PSARC files',
            filetypes=[('PSARC files', '*.psarc'), ('All files', '*.*')])
        if files:
            _set_files(list(files))

    def browse_folder():
        d = filedialog.askdirectory(title='Select folder containing PSARC files')
        if d:
            found = sorted(Path(d).glob('*.psarc'))
            if not found:
                file_var.set(f'No .psarc files found in {os.path.basename(d)}')
                selected_files.clear()
            else:
                _set_files([str(f) for f in found])

    output_dir = [os.path.join(os.path.expanduser('~'), 'ImmerrockCustomSongs')]

    def browse_output():
        d = filedialog.askdirectory(title='Select output folder',
                                    initialdir=output_dir[0])
        if d:
            output_dir[0] = d
            out_var.set(d)

    # ── Controls row 1: file selection ───────────────────────
    ctrl1 = tk.Frame(root, bg='#1e1e2e')
    ctrl1.pack(fill='x', padx=20, pady=(0, 4))
    ttk.Button(ctrl1, text='📄  Select Files', command=browse_files).pack(
        side='left', padx=(0, 8))
    ttk.Button(ctrl1, text='📂  Select Folder', command=browse_folder).pack(
        side='left', padx=(0, 8))

    # ── Controls row 2: output folder ────────────────────────
    ctrl2 = tk.Frame(root, bg='#1e1e2e')
    ctrl2.pack(fill='x', padx=20, pady=(0, 8))
    out_var = tk.StringVar(value=output_dir[0])
    ttk.Button(ctrl2, text='📁  Output Folder', command=browse_output).pack(
        side='left', padx=(0, 8))
    tk.Label(ctrl2, textvariable=out_var, bg='#1e1e2e', fg='#9ca3af',
             font=('Segoe UI', 8)).pack(side='left')

    # ── Progress bar + status ─────────────────────────────────
    prog_frame = tk.Frame(root, bg='#1e1e2e')
    prog_frame.pack(fill='x', padx=20, pady=(0, 4))
    progress = ttk.Progressbar(prog_frame, mode='determinate', style='TProgressbar')
    progress.pack(fill='x')
    status_var = tk.StringVar(value='')
    tk.Label(prog_frame, textvariable=status_var, bg='#1e1e2e', fg='#9ca3af',
             font=('Segoe UI', 8)).pack(anchor='w')

    # ── Log ──────────────────────────────────────────────────
    log = scrolledtext.ScrolledText(root, height=12, bg='#0f0f1a', fg='#d1d5db',
                                    font=('Consolas', 8), state='disabled',
                                    relief='flat', borderwidth=0)
    log.pack(fill='both', expand=True, padx=20, pady=(0, 8))

    def log_print(msg):
        log.configure(state='normal')
        log.insert('end', msg + '\n')
        log.see('end')
        log.configure(state='disabled')

    class LogRedirect:
        def write(self, s):
            if s.strip():
                root.after(0, log_print, s.rstrip())
        def flush(self):
            pass

    convert_btn = ttk.Button(root, text='▶  Convert')
    convert_btn.pack(pady=(0, 12))

    def run_conversion():
        if not selected_files:
            log_print('⚠  No files selected.')
            return
        convert_btn.configure(state='disabled')
        os.makedirs(output_dir[0], exist_ok=True)

        total = len(selected_files)
        progress['maximum'] = total
        progress['value'] = 0

        def worker():
            old_stdout = sys.stdout
            sys.stdout = LogRedirect()
            ok = err = skipped = 0
            try:
                for i, psarc_path in enumerate(selected_files, 1):
                    name = os.path.basename(psarc_path)
                    root.after(0, status_var.set,
                               f'Converting {i}/{total}: {name}')
                    result = convert_psarc(psarc_path, output_dir[0], vgmstream)
                    if result:
                        ok += 1
                    else:
                        err += 1
                    root.after(0, progress.__setitem__, 'value', i)
            finally:
                sys.stdout = old_stdout

            def finish():
                status_var.set(
                    f'Done — {ok} converted, {skipped} skipped, {err} failed')
                log_print(f'\n{"─"*40}')
                log_print(f'  Complete: {ok} converted, {err} failed')
                log_print(f'  Output: {output_dir[0]}')
                convert_btn.configure(state='normal')
                if os.path.isdir(output_dir[0]):
                    os.startfile(output_dir[0])
            root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    convert_btn.configure(command=run_conversion)
    root.mainloop()


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        run_gui()
        return

    args = sys.argv[1:]

    # Determine output directory and collect input PSARCs.
    # Supports:
    #   file.psarc [file2.psarc ...] [output_dir]
    #   input_folder [output_dir]
    output_dir = '.'
    psarc_files = []
    input_folder = None

    for arg in args:
        p = Path(arg)
        if p.is_dir():
            if input_folder is None and not psarc_files:
                # First directory with no files yet → input folder
                input_folder = str(p)
            else:
                output_dir = str(p)
        elif arg.lower().endswith('.psarc') and p.is_file():
            psarc_files.append(str(p))
        else:
            # Non-psarc, non-directory arg after files → output dir
            output_dir = arg

    if input_folder:
        found = sorted(Path(input_folder).glob('*.psarc'))
        if not found:
            print(f"No .psarc files found in: {input_folder}")
            sys.exit(1)
        psarc_files = [str(f) for f in found]
        print(f"Found {len(psarc_files)} PSARC(s) in {input_folder}")
        if output_dir == '.':
            output_dir = input_folder  # default output beside input

    if not psarc_files:
        print("Usage:")
        print("  python rs_to_immerrock.py <file.psarc> [output_folder]")
        print("  python rs_to_immerrock.py <input_folder> [output_folder]")
        print("  python rs_to_immerrock.py  (GUI mode)")
        sys.exit(1)

    vgmstream = find_vgmstream()
    if not vgmstream:
        print("⚠  WARNING: vgmstream-cli.exe not found.")
        print("   Audio conversion will be skipped.")
        print("   Download from: https://github.com/vgmstream/vgmstream/releases")
        print("   Place vgmstream-cli.exe in the same folder as this script.\n")

    os.makedirs(output_dir, exist_ok=True)

    ok = err = 0
    for psarc_path in psarc_files:
        if convert_psarc(psarc_path, output_dir, vgmstream):
            ok += 1
        else:
            err += 1

    print(f"\n{'═'*60}")
    print(f"  Complete: {ok} converted, {err} failed")
    print(f"  Output folder: {os.path.abspath(output_dir)}")
    print(f"{'═'*60}")


if __name__ == '__main__':
    main()
