#!/usr/bin/env python3
"""
Slide-support validation harness.

Compares the MIDI our converter produces from Motanum's debug arrangement
against her hand-authored reference MIDI (the ground truth for the new
slide encoding).  Run from the repo root:

    python tests/validate_slides.py

Inputs (in Updates-2026-08/):
    PART REAL_GUITAR_RS2.xml          RS2014 (v7) EoF export we parse
    Mid_GGLead_DebugSongs_Slides.mid  reference MIDI (what we must match)

Why a bespoke reader lives here: the production `parse_arrangement()` expects
capitalised/flat XML and returns 0 notes on real RS/EoF XML (lowercase, nested
under <levels>).  This harness reads the real format directly so we can drive
`build_midi()` and diff against the reference while we iterate on slide support.

Exit code 0 = match, 1 = mismatch, so it can gate CI later.
"""

import os
import sys
import xml.etree.ElementTree as ET

# Import the converter from the repo root regardless of CWD.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import rs_to_immerrock as R  # noqa: E402

UPDATES = os.path.join(_REPO, 'Updates-2026-08')
XML_PATH = os.path.join(UPDATES, 'PART REAL_GUITAR_RS2.xml')
REF_MID  = os.path.join(UPDATES, 'Mid_GGLead_DebugSongs_Slides.mid')

# Ch15 note numbers → human labels (from NoteEffectChart.png)
SLIDE_NOTES = {20: 'Legato', 21: 'Shift', 22: 'SlideOutDown', 23: 'SlideOutUp'}
NUM_STRINGS = 6


# ────────────────────────────────────────────────────────────────
#  Read the real RS/EoF arrangement XML into build_midi's arr dict
# ────────────────────────────────────────────────────────────────
def read_rs_xml(path: str) -> dict:
    root = ET.parse(path).getroot()

    def f(el, a, d=0.0):
        v = el.get(a)
        return float(v) if v is not None else d

    def i(el, a, d=0):
        v = el.get(a)
        return int(v) if v is not None else d

    tuning_el = root.find('tuning')
    tuning = [i(tuning_el, f'string{n}') for n in range(6)] if tuning_el is not None else [0] * 6

    beats = []
    beats_el = root.find('ebeats')
    if beats_el is not None:
        for eb in beats_el.findall('ebeat'):
            beats.append({'time': f(eb, 'time'), 'measure': i(eb, 'measure', -1)})

    sections = []
    sec_el = root.find('sections')
    if sec_el is not None:
        for s in sec_el.findall('section'):
            sections.append({'name': s.get('name', ''), 'time': f(s, 'startTime')})

    # chordId → template: name, EoF displayName, and per-string fret/finger.
    # Used for chord text meta and to recognise arpeggio handshapes.
    chord_templates = []
    ct_el = root.find('chordTemplates')
    if ct_el is not None:
        for ct in ct_el.findall('chordTemplate'):
            chord_templates.append({
                'name':    ct.get('chordName', ''),
                'display': ct.get('displayName', ''),
                'frets':   [i(ct, f'fret{n}', -1)   for n in range(NUM_STRINGS)],
                'fingers': [i(ct, f'finger{n}', -1) for n in range(NUM_STRINGS)],
            })
    chord_names = {idx: t['name'] for idx, t in enumerate(chord_templates)}

    notes = []
    arpeggios = []

    def add_note(el, chord_name=''):
        xml_string = i(el, 'string')
        # RS/EoF string index is 0=low E; flip to build_midi's 0=high e convention
        # (matches _parse_sng_binary's flip so channel == xml_string in the output).
        rs_string = (NUM_STRINGS - 1) - xml_string
        fret     = i(el, 'fret')
        slide_to = i(el, 'slideTo', -1)
        slide_un = i(el, 'slideUnpitchTo', -1)
        link     = i(el, 'linkNext', 0)

        effects = set()
        if slide_to >= 0:
            effects.add('slide')          # current build_midi → note 20 + pitch bend

        notes.append({
            'time': f(el, 'time'), 'sustain': f(el, 'sustain', 0.0),
            'string': rs_string, 'fret': fret, 'finger': 0,
            'effects': effects, 'vibrato': 0,
            'slide_semitones': (slide_to - fret) if slide_to >= 0 else 0,
            'bend_data': [], 'chord_name': chord_name,
            # new fields for the upcoming build_midi slide logic:
            'slide_to': slide_to, 'slide_unpitch_to': slide_un, 'link_next': link,
        })

    lvl = root.find('levels/level')
    if lvl is not None:
        notes_el = lvl.find('notes')
        if notes_el is not None:
            for n in notes_el.findall('note'):
                add_note(n)
        chords_el = lvl.find('chords')
        if chords_el is not None:
            for ch in chords_el.findall('chord'):
                name = chord_names.get(i(ch, 'chordId'), '')
                for cn in ch.findall('chordNote'):
                    add_note(cn, chord_name=name)

        # Arpeggio regions: a handShape whose chord template EoF exported as an
        # arpeggio (displayName ends in "-arp"). The notes inside are plain
        # individual notes; the shape supplies the frame + finger data.
        # Strings are flipped to build_midi's convention, same as add_note.
        hs_el = lvl.find('handShapes')
        if hs_el is not None:
            for hs in hs_el.findall('handShape'):
                cid = i(hs, 'chordId', -1)
                if not (0 <= cid < len(chord_templates)):
                    continue
                t = chord_templates[cid]
                if not t['display'].endswith('-arp'):
                    continue
                strings = [((NUM_STRINGS - 1) - s, t['frets'][s], t['fingers'][s])
                           for s in range(NUM_STRINGS) if t['frets'][s] >= 0]
                arpeggios.append({
                    'start': f(hs, 'startTime'), 'end': f(hs, 'endTime'),
                    'chord_name': t['name'], 'strings': strings,
                })

    notes.sort(key=lambda x: x['time'])
    return {
        'arr_type': 'lead', 'title': root.findtext('title', ''),
        'tuning': tuning, 'avg_tempo': f(root, 'averageTempo', 120.0) or 120.0,
        'song_length': f(root, 'songLength'),
        'beats': beats, 'sections': sections, 'notes': notes, 'vocals': [],
        'arpeggios': arpeggios,
    }


# ────────────────────────────────────────────────────────────────
#  Normalise a MIDI track into comparable event sets
# ────────────────────────────────────────────────────────────────
def normalise(mid):
    track = mid.tracks[1] if len(mid.tracks) > 1 else mid.tracks[0]
    real_notes, markers, texts = [], [], []
    nonzero_pb = 0
    t = 0
    for msg in track:
        t += msg.time
        ch = getattr(msg, 'channel', None)
        if msg.type == 'note_on' and msg.velocity > 0 and ch is not None and ch < 15:
            real_notes.append((t, ch, msg.note))
        elif msg.type == 'note_on' and msg.velocity > 0 and ch == 15 and msg.note in SLIDE_NOTES:
            markers.append((t, msg.note, msg.velocity))
        elif msg.type == 'pitchwheel' and msg.pitch != 0:
            nonzero_pb += 1
        elif msg.type == 'text':
            texts.append((t, msg.text))
    return {
        'real': sorted(real_notes), 'markers': sorted(markers),
        'texts': sorted(texts), 'nonzero_pb': nonzero_pb,
    }


def diff_section(title, ref, ours, fmt):
    ref_s, ours_s = set(ref), set(ours)
    missing = sorted(ref_s - ours_s)
    extra   = sorted(ours_s - ref_s)
    ok = not missing and not extra
    print(f"\n{'PASS' if ok else 'FAIL'} {title}: "
          f"{len(ref_s)} expected, {len(ours_s)} produced"
          f"{'' if ok else f' - {len(missing)} missing, {len(extra)} extra'}")
    for m in missing:
        print(f"    MISSING (in reference, not ours):  {fmt(m)}")
    for e in extra:
        print(f"    EXTRA   (in ours, not reference):  {fmt(e)}")
    return ok


def main():
    import mido
    ref  = normalise(mido.MidiFile(REF_MID))
    arr  = read_rs_xml(XML_PATH)
    ours = normalise(R.build_midi(arr, 'REAL_GUITAR', is_bass=False))

    print(f"Parsed {len(arr['notes'])} notes / {len(arr['beats'])} beats from XML.")

    ok = True
    ok &= diff_section(
        'Slide markers (ch15 20/21/22/23)', ref['markers'], ours['markers'],
        lambda m: f"tick={m[0]:>6} note={m[1]} ({SLIDE_NOTES[m[1]]:12}) vel={m[2]}")
    ok &= diff_section(
        'Real note-ons', ref['real'], ours['real'],
        lambda m: f"tick={m[0]:>6} ch={m[1]} note={m[2]}")

    # Pitch bend must be entirely absent for this (slides-only) arrangement.
    pb_ok = ours['nonzero_pb'] == 0
    ok &= pb_ok
    print(f"\n{'PASS' if pb_ok else 'FAIL'} Pitch bend: reference has 0 non-zero events, "
          f"ours has {ours['nonzero_pb']}"
          f"{'' if pb_ok else '  (slides must NOT emit pitch bend - Motanum: multiple tails)'}")

    diff_section(  # informational only — chord naming is pre-existing behaviour
        'Text / chord names (informational)', ref['texts'], ours['texts'],
        lambda m: f"tick={m[0]:>6} {m[1]!r}")

    print("\n" + ("=" * 60))
    print("RESULT:", "PASS" if ok else "FAIL (expected until slide support lands)")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
