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

ARP = os.path.join(_REPO, 'Updates-2026-08', 'Arpeggio')

# (label, xml, reference midi, num_strings, is_bass, track name, onset tolerance)
# Onset tolerance: EoF writes note times to 3 decimals, dropping up to ~1 ms
# (~0.7 tick at 90 BPM) versus the positions its own MIDI was rendered from.
# Grid-aligned notes (the guitar test) survive that rounding, so it stays
# strict; the hand-placed, off-grid bass test needs +/-1 tick. Production
# (SNG float32 times) is unaffected.
CASES = [
    ('Guitar', 'PART REAL_GUITAR_RS2.xml', 'GGLead.mid', 6, False, 'REAL_GUITAR', 0),
    ('Bass',   'PART REAL_BASS_RS2.xml',   'GGBass.mid', 4, True,  'REAL_BASS',   1),
]

FINGER_NOTES = {31: 'Index', 32: 'Middle', 33: 'Ring', 34: 'Little', 35: 'Thumb'}
SLIDE_NOTES  = {20, 21, 22, 23}


def normalise(mid, num_strings):
    """Split a track into the event classes the arpeggio encoding uses.
    Played notes live on channels < num_strings; frame ghost notes on
    num_strings..14 (Immerrock offsets the frame by the string count)."""
    NUM_STRINGS = num_strings
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


FRAME_OFF_TOL = 2  # ticks


def diff_frames(ref, ours, tol=FRAME_OFF_TOL):
    """Frame notes must match exactly on (start, channel, note); the note-off
    may differ by up to `tol` ticks. EoF writes handShape endTime to 3 decimals
    so the reference's off tick carries sub-tick precision the XML has lost;
    the resulting +/-1 tick (~1.4 ms) is noise, not a converter error."""
    ours_left = list(ours)
    missing = []
    for st, en, ch, note in ref:
        hit = next((o for o in ours_left
                    if o[0] == st and o[2] == ch and o[3] == note and abs(o[1] - en) <= tol), None)
        if hit is None:
            missing.append((st, en, ch, note))
        else:
            ours_left.remove(hit)
    ok = not missing and not ours_left
    print(f"\n{'PASS' if ok else 'FAIL'} Frame ghost notes (ch6-14; off tick +/-{tol}): "
          f"{len(ref)} expected, {len(ours)} produced"
          f"{'' if ok else f' - {len(missing)} missing, {len(ours_left)} extra'}")
    for m in missing:
        print(f"    MISSING (in reference, not ours):  tick={m[0]:>6}->{m[1]:<6} ch={m[2]} note={m[3]}")
    for e in ours_left:
        print(f"    EXTRA   (in ours, not reference):  tick={e[0]:>6}->{e[1]:<6} ch={e[2]} note={e[3]}")
    return ok


def diff_onsets(title, ref, ours, tol, fmt):
    """Like diff_section, but the leading tick may differ by up to `tol`;
    every other field must match exactly. tol=0 is an exact set diff."""
    if tol == 0:
        return diff_section(title, ref, ours, fmt)
    ours_left = list(ours)
    missing = []
    for r in ref:
        hit = next((o for o in ours_left if o[1:] == r[1:] and abs(o[0] - r[0]) <= tol), None)
        if hit is None:
            missing.append(r)
        else:
            ours_left.remove(hit)
    ok = not missing and not ours_left
    print(f"\n{'PASS' if ok else 'FAIL'} {title} (tick +/-{tol}): "
          f"{len(ref)} expected, {len(ours)} produced"
          f"{'' if ok else f' - {len(missing)} missing, {len(ours_left)} extra'}")
    for m in missing:
        print(f"    MISSING (in reference, not ours):  {fmt(m)}")
    for e in ours_left:
        print(f"    EXTRA   (in ours, not reference):  {fmt(e)}")
    return ok


def run_case(label, xml, ref_mid, num_strings, is_bass, track, tol):
    import mido
    print(f"\n{'#' * 60}\n#  {label} — {xml} vs {ref_mid}\n{'#' * 60}")
    ref  = normalise(mido.MidiFile(os.path.join(ARP, ref_mid)), num_strings)
    arr  = read_rs_xml(os.path.join(ARP, xml), num_strings=num_strings)
    ours = normalise(R.build_midi(arr, track, is_bass=is_bass), num_strings)

    print(f"Parsed {len(arr['notes'])} notes / {len(arr['beats'])} beats / "
          f"{len(arr['arpeggios'])} arpeggio regions from XML.")
    for a in arr['arpeggios']:
        strs = ', '.join(f"s{s}:f{fr}/{fg}" for s, fr, fg in a['strings'])
        print(f"    {a['chord_name']:6} {a['start']:6.3f}->{a['end']:6.3f}  [{strs}]")

    ok = True
    ok &= diff_onsets(
        f'Played notes (ch0-{num_strings - 1} onsets)', ref['played'], ours['played'], tol,
        lambda m: f"tick={m[0]:>6} ch={m[1]} note={m[2]}")
    ok &= diff_frames(ref['frames'], ours['frames'])
    ok &= diff_onsets(
        'Finger markers (ch15 31-35)', ref['fingers'], ours['fingers'], tol,
        lambda m: f"tick={m[0]:>6} note={m[1]} ({FINGER_NOTES[m[1]]:6}) vel={m[2]}")
    ok &= diff_section(
        'Chord-name text', ref['texts'], ours['texts'],
        lambda m: f"tick={m[0]:>6} {m[1]!r}")

    # This arrangement has no slides and no bends: both must stay silent.
    quiet = not ours['slides'] and ours['nonzero_pb'] == 0
    ok &= quiet
    print(f"\n{'PASS' if quiet else 'FAIL'} No stray signals: slides={len(ours['slides'])} "
          f"nonzero_pb={ours['nonzero_pb']} (reference: 0 / 0)")

    print(f"\n{label}: {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    results = {label: run_case(label, *rest) for label, *rest in CASES}
    print("\n" + ("=" * 60))
    for label, ok in results.items():
        print(f"  {label:8} {'PASS' if ok else 'FAIL'}")
    all_ok = all(results.values())
    print("RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
