const fs = require('fs');
const path = require('path');
const assert = require('assert');
const vinylApp = new Function(fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8') + '; return vinylApp;')();
const release = {id: 123, artist: 'Artist', title: 'Album', tracks: [
    {position: 'A1', title: 'Song', duration: '3:00'},
    {position: 'B1', title: 'Other song', duration: '3:00'},
]};
const result = (id, extra = {}) => ({recording_id: id, configured: true, state: 'matched',
    song: {artist: 'Artist', title: 'Song', album: 'Album'}, candidates: [release], selected_release: release, ...extra});
function setup() {
    const app = vinylApp();
    app.notify = () => {};
    app.fetchScan = () => {};
    app.destroyWaveform = () => {};
    app.afterTargetsChanged = () => { app.detections = (app.detections || 0) + 1; };
    app.searchDiscogs = () => { app.searches = (app.searches || 0) + 1; };
    app.discogsConfigured = true;
    return app;
}
const file = (id, recognition) => ({id, filename: 'Recording.wav', duration: 360, musicDuration: 360, recognition});

(async () => {
    // Live capture shows the likely album without detecting a growing file or touching another edit.
    const app = setup();
    app.uploadedFiles = [file('imported')];
    app.selectFile('imported');
    app.recorder = {id: 'live', state: 'recording'};
    app.syncRecognition(result('live'));
    assert.strictEqual(app.songRecognition.selected_release.id, 123);
    assert.strictEqual(app.selectedRelease, null);
    assert.strictEqual(app.detections, undefined);
    // Stop/save registers an immutable original, then selects its album exactly once.
    app.recorder.state = 'stopped';
    app.acceptRecordedFile(file('live', result('live')));
    assert.strictEqual(app.selectedRelease.id, 123);
    assert.strictEqual(app.recognitionReleaseId, 123);
    assert.strictEqual(app.detections, 1);
    app.syncRecognition(result('live'));
    app.acceptRecordedFile(file('live', result('live')));
    assert.strictEqual(app.detections, 1);
    app.selectFile('imported');
    app.selectFile('live');
    assert.strictEqual(app.selectedRelease.id, 123, 'reopening a queue entry still loads its album');

    // A match that arrives after stop is applied if the editor is still untouched.
    const late = setup();
    late.acceptRecordedFile(file('late'));
    late.syncRecognition(result('late'));
    assert.strictEqual(late.selectedRelease.id, 123);
    assert.strictEqual(late.detections, 1);
    // An explicit correction from the suggestions also updates an already-open editor.
    const correction = {...release, id: 789};
    late.recordingRequest = async () => result('late', {selected_release: correction, selection: 'manual'});
    await late.recognitionAction('select', {recording_id: 'late', release_id: 789});
    assert.strictEqual(late.selectedRelease.id, 789);
    assert.strictEqual(late.detections, 2);
    late.recordingRequest = async () => result('late', {selected_release: null, selection: 'manual'});
    await late.recognitionAction('select', {recording_id: 'late', release_id: null});
    assert.strictEqual(late.selectedRelease, null);

    // A manual album, manual boundaries, or completed detection must win over a late match.
    for (const edit of ['release', 'boundaries', 'tracks']) {
        const a = setup();
        a.acceptRecordedFile(file('edited'));
        if (edit === 'release') a.selectedRelease = {...release, id: 456};
        if (edit === 'boundaries') a.boundariesEdited = true;
        if (edit === 'tracks') a.detectedTracks = [{start: 0, end: 300}];
        a.syncRecognition(result('edited'));
        assert.strictEqual(a.detections, undefined, edit);
        if (edit === 'release') assert.strictEqual(a.selectedRelease.id, 456);
    }
    // Busy work defers application; an ambiguous result leaves the manual search available.
    const busy = setup();
    busy.acceptRecordedFile(file('busy'));
    busy.analyzing = true;
    busy.syncRecognition(result('busy'));
    assert.strictEqual(busy.detections, undefined);
    busy.analyzing = false;
    busy.syncRecognition(result('busy'));
    assert.strictEqual(busy.detections, 1);
    const ambiguous = setup();
    ambiguous.acceptRecordedFile(file('ambiguous', result('ambiguous', {selected_release: null})));
    assert.strictEqual(ambiguous.selectedRelease, null);
    assert.strictEqual(ambiguous.searchQuery, 'Artist Album');
    assert.strictEqual(ambiguous.searches, 1);

    // A late action response for an old capture cannot replace the new live status.
    app.recorder.id = 'new';
    app.syncRecognition(result('new', {song: null, selected_release: null}));
    app.syncRecognition(result('live'));
    assert.strictEqual(app.songRecognition.recording_id, 'new');

    // Saving the key clears the form; recognition errors never become recording errors.
    app.recognitionTokenInput = 'secret';
    app.recordingRequest = async () => ({configured: true});
    await app.recognitionAction('token', {token: 'secret'});
    assert.strictEqual(app.recognitionTokenInput, '');
    app.recordingRequest = async () => { throw new Error('AudD unavailable'); };
    await app.recognitionAction('identify');
    assert.strictEqual(app.recognitionBusy, false);
    assert.match(app.recognitionNotice, /AudD unavailable/);
    assert.strictEqual(app.recordingError, '');
})().catch(error => { console.error(error); process.exit(1); });
