#!/usr/bin/env python3
"""
Arpeggio-support validation harness.

Compares the MIDI our converter produces from Motanum's arpeggio debug
arrangement against her reference MIDI (what Immerrock expects). Run from
the repo root:

    python tests/validate_arpeggio.py

Inputs (in Updates-2026-08/Arpeggio/):
    PART REAL_GUITAR_RS2.xml   RS2014 (v7) EoF export we parse
    GGLead.mid                 reference MIDI (what we must match)

The arpeggio encoding has NO dedicated ch15 note. Per arpeggio handShape
region (a chord template EoF exports with displayName "...-arp"):
  * chord-name text meta at the region start
  * "soft chord frame" ghost notes: one per template string on MIDI channel
    string_channel + 6, at the template fret, spanning startTime->endTime
  * finger markers (ch15 31-35, vel = string*5+1) from the template: a burst
    of every string's finger at the region start, then one per played note
  * the played notes themselves on channels 0-5 as normal

Reuses the real-RS-XML reader and diff helper from validate_slides.py.
Exit code 0 = match, 1 = mismatch.
"""

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, 'tests'))
import rs_to_immerrock as R                      # noqa: E402
from validate_slides import read_rs_xml, diff_section  # noqa: E402

ARP      = os.path.join(_REPO, 'Updates-2026-08', 'Arpeggio')
XML_PATH = os.path.join(ARP, 'PART REAL_GUITAR_RS2.xml')
REF_MID  = os.path.join(ARP, 'GGLead.mid')

FINGER_NOTES = {31: 'Index', 32: 'Middle', 33: 'Ring', 34: 'Little', 35: 'Thumb'}
SLIDE_NOTES  = {20, 21, 22, 23}
NUM_STRINGS  = 6


def normalise(mid):
    """Split a track into the event classes the arpeggio encoding uses."""
    track = mid.tracks[1] if len(mid.tracks) > 1 else mid.tracks[0]
    played, frames, fingers, texts, slides = [], [], [], [], []
    frame_on = {}
    nonzero_pb = 0
    t = 0
    for msg in track:
        t += msg.time
        ch = getattr(msg, 'channel', None)
        is_on  = msg.type == 'note_on' and msg.velocity > 0
        is_off = msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0)
        if ch is None:
            if msg.type == 'text':
                texts.append((t, msg.text))
            continue
        if ch < NUM_STRINGS:                       # played notes: compare onsets
            if is_on:
                played.append((t, ch, msg.note))
        elif ch < 15:                              # frame ghost notes: on AND off matter
            if is_on:
                frame_on[(ch, msg.note)] = t
            elif is_off:
                st = frame_on.pop((ch, msg.note), None)
                if st is not None:
                    frames.append((st, t, ch, msg.note))
        elif ch == 15 and is_on:
            if msg.note in FINGER_NOTES:
                fingers.append((t, msg.note, msg.velocity))
            elif msg.note in SLIDE_NOTES:
                slides.append((t, msg.note, msg.velocity))
        if msg.type == 'pitchwheel' and msg.pitch != 0:
            nonzero_pb += 1
    return {
        'played': sorted(played), 'frames': sorted(frames),
        'fingers': sorted(fingers), 'texts': sorted(texts),
        'slides': sorted(slides), 'nonzero_pb': nonzero_pb,
    }


def main():
    import mido
    ref  = normalise(mido.MidiFile(REF_MID))
    arr  = read_rs_xml(XML_PATH)
    ours = normalise(R.build_midi(arr, 'REAL_GUITAR', is_bass=False))

    print(f"Parsed {len(arr['notes'])} notes / {len(arr['beats'])} beats / "
          f"{len(arr['arpeggios'])} arpeggio regions from XML.")
    for a in arr['arpeggios']:
        strs = ', '.join(f"s{s}:f{fr}/{fg}" for s, fr, fg in a['strings'])
        print(f"    {a['chord_name']:6} {a['start']:6.3f}->{a['end']:6.3f}  [{strs}]")

    ok = True
    ok &= diff_section(
        'Played notes (ch0-5 onsets)', ref['played'], ours['played'],
        lambda m: f"tick={m[0]:>6} ch={m[1]} note={m[2]}")
    ok &= diff_section(
        'Frame ghost notes (ch6-14, on->off)', ref['frames'], ours['frames'],
        lambda m: f"tick={m[0]:>6}->{m[1]:<6} ch={m[2]} note={m[3]}")
    ok &= diff_section(
        'Finger markers (ch15 31-35)', ref['fingers'], ours['fingers'],
        lambda m: f"tick={m[0]:>6} note={m[1]} ({FINGER_NOTES[m[1]]:6}) vel={m[2]}")
    ok &= diff_section(
        'Chord-name text', ref['texts'], ours['texts'],
        lambda m: f"tick={m[0]:>6} {m[1]!r}")

    # This arrangement has no slides and no bends: both must stay silent.
    quiet = not ours['slides'] and ours['nonzero_pb'] == 0
    ok &= quiet
    print(f"\n{'PASS' if quiet else 'FAIL'} No stray signals: slides={len(ours['slides'])} "
          f"nonzero_pb={ours['nonzero_pb']} (reference: 0 / 0)")

    print("\n" + ("=" * 60))
    print("RESULT:", "PASS" if ok else "FAIL (expected until arpeggio support lands)")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
