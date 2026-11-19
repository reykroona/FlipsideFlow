"""Conservative match-quality checks for unattended exports (not probabilities)."""
import re
from difflib import SequenceMatcher
from recognition import normalized

THRESHOLD = 90


def seconds(value):
    try:
        parts = str(value).split(':')
        if len(parts) not in (2, 3): return None
        total = 0
        if any(not p.isdigit() for p in parts) or any(int(p) >= 60 for p in parts[1:]): return None
        for part in parts: total = total * 60 + int(part)
        return total if total > 0 else None
    except (TypeError, ValueError):
        return None


def choose_scope(release, song, side_count, duration):
    """Select a unique contiguous run of recorded sides, never guess A from a B song."""
    groups = {}
    for track in release.get('tracks', []):
        match = re.fullmatch(r'([A-Z])\d*', track.get('position', '').strip())
        if not match: return None
        groups.setdefault(match[1], []).append(track)
    sides = list(groups)
    candidates = []
    for start in range(len(sides) - side_count + 1):
        chosen = sides[start:start + side_count]
        tracks = [t for side in chosen for t in groups[side]]
        lengths = [seconds(t.get('duration')) for t in tracks]
        if not lengths or None in lengths: continue
        if song and not any(SequenceMatcher(None, normalized(song['title']), normalized(t['title'])).ratio() >= .95 for t in tracks): continue
        difference = abs(sum(lengths) - duration)
        candidates.append((difference, chosen, tracks, lengths))
    candidates.sort(key=lambda c: c[0])
    if not candidates or (len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 10): return None
    _, chosen, tracks, lengths = candidates[0]
    return {'sides': chosen, 'tracks': tracks, 'lengths': lengths,
            'breaks': [sum(len(groups[s]) for s in chosen[:i]) for i in range(1, len(chosen))]}


def quality(song, release, scope, analysis, *, manual=False):
    reasons, score = [], 0
    if manual:
        score = 70
    else:
        def similarity(a, b):
            return SequenceMatcher(None, normalized(a), normalized(b)).ratio() if normalized(a) and normalized(b) else 0
        artist = similarity((song or {}).get('artist'), release.get('artist')) if song else 0
        title = max((similarity((song or {}).get('title'), t.get('title')) for t in scope['tracks']), default=0) if song else 0
        album = similarity((song or {}).get('album'), release.get('title')) if song and song.get('album') else 0
        score += 25 if artist == 1 else 15 if artist >= .9 else 0
        score += 25 if title == 1 else 20 if title >= .95 else 0
        score += 20 if album == 1 else 10 if album >= .95 else 0
        if artist < .9 or title < .95: reasons.append('Artist or song does not closely match the selected album.')
        if album < .95: reasons.append('The recognized album title is missing or different.')
    tracks = analysis.get('tracks', [])
    count_ok = analysis.get('matched') and len(tracks) == len(scope['tracks'])
    if count_ok: score += 10
    else: reasons.append('Detected track count differs from the selected sides.')
    lengths = scope['lengths']
    timing_ok = count_ok and all(abs(t['duration'] - d) <= max(4, d * .02) for t, d in zip(tracks, lengths)) and not analysis.get('scale_clamped')
    if timing_ok: score += 10
    else: reasons.append('Track timings need review.')
    required = set(range(1, len(tracks))) - set(scope['breaks'])
    supported = {b['index'] for b in analysis.get('boundaries', []) if b.get('snapped') and b.get('gap')}
    gaps_ok = analysis.get('method') == 'durations' and required <= supported
    if gaps_ok: score += 10
    else: reasons.append('Some split points have no clear silence evidence.')
    return {'score': score, 'threshold': THRESHOLD, 'eligible': score >= THRESHOLD and not reasons,
            'reasons': reasons}
