import copy
from finish_quality import choose_scope, quality

SONG = {'artist': 'Example Artist', 'title': 'One', 'album': 'Example Album'}
RELEASE = {'artist': 'Example Artist', 'title': 'Example Album', 'tracks': [
    {'position': 'A1', 'title': 'One', 'duration': '3:00'},
    {'position': 'A2', 'title': 'Two', 'duration': '4:00'},
    {'position': 'B1', 'title': 'Three', 'duration': '5:00'}]}
ANALYSIS = {'matched': True, 'method': 'durations', 'tracks': [{'duration': 180}, {'duration': 240}],
            'boundaries': [{'index': 1, 'snapped': True, 'gap': {'start': 178, 'end': 182}}]}


def test_exact_identity_and_real_splits_pass():
    scope = choose_scope(RELEASE, SONG, 1, 422)
    assert scope['sides'] == ['A']
    assert quality(SONG, RELEASE, scope, ANALYSIS)['eligible']
    assert quality(SONG, RELEASE, scope, ANALYSIS)['score'] == 100


def test_forced_even_splits_fail_even_if_timings_look_perfect():
    scope = choose_scope(RELEASE, SONG, 1, 420)
    analysis = {**ANALYSIS, 'boundaries': []}
    result = quality(SONG, RELEASE, scope, analysis)
    assert result['score'] == 90
    assert not result['eligible']  # 90 alone is insufficient without silence evidence


def test_no_album_wrong_artist_or_partial_recording_need_review():
    scope = choose_scope(RELEASE, SONG, 1, 420)
    for song in [{**SONG, 'album': None}, {**SONG, 'artist': 'Other artist'}]:
        assert not quality(song, RELEASE, scope, ANALYSIS)['eligible']
    partial = {**ANALYSIS, 'tracks': [{'duration': 100}, {'duration': 240}]}
    assert not quality(SONG, RELEASE, scope, partial)['eligible']
    assert quality(None, RELEASE, scope, ANALYSIS, manual=True)['eligible']


def test_full_record_and_b_side_respect_song_and_flip_count():
    assert choose_scope(RELEASE, SONG, 2, 720)['breaks'] == [2]
    assert choose_scope(RELEASE, {**SONG, 'title': 'Three'}, 1, 300)['sides'] == ['B']
    assert choose_scope(RELEASE, SONG, 3, 720) is None
    incomplete = copy.deepcopy(RELEASE)
    incomplete['tracks'][0]['duration'] = ''
    assert choose_scope(incomplete, SONG, 1, 420) is None


def test_similar_sides_without_recognized_song_are_ambiguous():
    release = {**RELEASE, 'tracks': [
        {'position': 'A1', 'title': 'One', 'duration': '3:00'},
        {'position': 'B1', 'title': 'Two', 'duration': '3:02'}]}
    assert choose_scope(release, None, 1, 181) is None
