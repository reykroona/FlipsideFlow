// Load the actual guided-flow mixin, not only the legacy Workbench component.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const read = name => fs.readFileSync(path.join(__dirname, '../../backend/static', name), 'utf8');
const vinylApp = new Function(read('flow.js') + '\n' + read('app.js') + ';return vinylApp;')();
const album = {id: 10, artist: 'Artist', title: 'Album', cover_url: 'cover.jpg', tracks: [
    {position: 'A1', title: 'First song', duration: '3:00'},
    {position: 'B1', title: 'Second song', duration: '4:00'},
]};
const status = (id, extra = {}) => ({recording_id: id, enabled: true, configured: true,
    state: 'matched', message: 'Likely album loaded.', selection: 'auto',
    song: {artist: 'Artist', title: 'First song'}, candidates: [album], selected_release: album, ...extra});
function setup() {
    const app = vinylApp();
    app.view = 'record';
    app.flow.step = 'record';
    app.notify = () => {};
    app.fetchScan = () => {};
    app.destroyWaveform = () => {};
    app.afterTargetsChanged = () => { app.detections = (app.detections || 0) + 1; };
    app.recorder = {id: 'live', state: 'recording', seconds: 120, side: 'A', side_seconds: 120};
    return app;
}

(async () => {
    const app = setup();
    // The active workbench may contain another album; recognition must not edit it.
    app.currentFile = {id: 'other'};
    app.currentFileId = 'other';
    app.selectedRelease = {...album, id: 99};
    app.syncRecognition(status('live', {state: 'waiting', song: null, selected_release: null}));
    assert.match(app.flowRecognitionMessage, /Listening while recording/);
    app.syncRecognition(status('live'));
    assert.strictEqual(app.flow.release.id, 10, 'album is visible before stop/save');
    assert.strictEqual(app.flowTitle, 'Artist — Album');
    assert.strictEqual(app.flowRuler[0].title, 'First song');
    assert.strictEqual(app.recorder.state, 'recording');
    assert.strictEqual(app.selectedRelease.id, 99, 'other editor remains unchanged');
    assert.strictEqual(app.detections, undefined, 'never split a growing WAV');
    assert.strictEqual(app.uploadedFiles.length, 0);
    app.syncRecognition(status('old', {selected_release: {...album, id: 88}}));
    assert.strictEqual(app.flow.release.id, 10, 'old captures cannot replace the live album');
    app.recorder.state = 'paused';
    app.syncRecognition(status('live', {selection: 'manual', selected_release: null}));
    assert.strictEqual(app.flow.release, null, 'dismissal works during a flip');
    app.syncRecognition(status('live', {selection: 'manual'}));
    assert.strictEqual(app.flow.release.id, 10);

    // Stop/save hands the recognized album to the editor once, using actual recorded sides.
    app.recorder.state = 'finished';
    const file = {id: 'live', filename: 'Record.wav', duration: 420, musicDuration: 420,
        recording_side_markers: [180], recognition: status('live')};
    app.acceptRecordedFile(file);
    assert.strictEqual(app.flow.step, 'review');
    assert.strictEqual(app.selectedRelease.id, 10);
    assert.strictEqual(app.recognitionReleaseId, 10);
    assert.deepStrictEqual(app.selectedSides, ['A', 'B']);
    assert.strictEqual(app.detections, 1, 'handoff must not start detection twice');
    app.acceptRecordedFile(file);
    assert.strictEqual(app.detections, 1);

    // Manual selections win. An unfinished old capture cannot populate the next setup screen.
    const manual = setup();
    manual.flow.release = {...album, id: 77};
    manual.flow.releaseOrigin = 'manual';
    manual.syncRecognition(status('live'));
    assert.strictEqual(manual.flow.release.id, 77);
    const next = setup();
    next.recorder.state = 'finished';
    next.syncRecognition(status('live'));
    assert.strictEqual(next.flow.release, null);
    assert.strictEqual(next.flowRecognition, null);

    // A reloaded window restores the running capture's live result.
    const restored = setup();
    restored.recordingEnabled = true;
    restored.flow.step = 'add';
    restored.songRecognition = status('live');
    restored.loadLibrary = async () => {};
    restored.pollFinishQueue = async () => {};
    global.window = {addEventListener() {}};
    await restored.initFlow();
    assert.strictEqual(restored.flow.release.id, 10);

    // Starting without a manual pick enables recognition; selecting a known album skips it.
    const starting = setup();
    starting.recorder.state = 'monitoring';
    starting.recordingAction = async (action, body) => { starting.startBody = body; };
    await starting.flowStart();
    assert.strictEqual(starting.startBody.identify_album, true);
    starting.flow.release = album;
    starting.flow.releaseOrigin = 'manual';
    await starting.flowStart();
    assert.strictEqual(starting.startBody.identify_album, false);

    // Token setup and a missing Discogs connection are visible directly in the main flow.
    const missing = setup();
    missing.songRecognition = {configured: false};
    assert.match(missing.flowRecognitionMessage, /token in Settings/);
    const html = read('index.html');
    const record = html.slice(html.indexOf('<main x-show="view === \'record\'"'), html.indexOf('<!-- Settings Modal -->'));
    assert.ok(record.includes('id="live-recognition"'));
    assert.ok(record.includes('Identify the album while recording'));
    const settings = html.slice(html.indexOf('<!-- Settings Modal -->'), html.indexOf('<!-- Context Menu for Waveform Splits -->'));
    assert.ok(settings.includes('id="recognition-settings"'));
    assert.ok(settings.includes('recognitionTokenInput'));
    assert.ok(settings.includes('Save AudD token'));
})().catch(error => { console.error(error); process.exit(1); });
