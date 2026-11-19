const fs = require('fs');
const path = require('path');
const assert = require('assert');
const vinylApp = new Function(fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8') + '; return vinylApp;')();
const release = {id: 123, artist: 'Artist', title: 'Album', tracks: [
    {position: 'A1', title: 'Song', duration: '3:00'},
    {position: 'B1', title: 'Other song', duration: '3:00'},
]};
const match = (file_id, extra = {}) => ({file_id, source: 'import', state: 'matched',
    song: {artist: 'Artist', title: 'Song', album: 'Album'}, candidates: [release], selected_release: release, ...extra});
function setup() {
    const app = vinylApp();
    app.notify = () => {};
    app.fetchScan = () => {};
    app.destroyWaveform = () => {};
    app.afterTargetsChanged = () => { app.detections = (app.detections || 0) + 1; };
    app.searchDiscogs = () => {};
    app.recordingEnabled = true;
    app.songRecognition.configured = true;
    app.uploadedFiles = ['one', 'two'].map(id => ({id, filename: id + '.aiff', duration: 360, musicDuration: 360, recording_side_markers: []}));
    return app;
}
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
    const app = setup();
    const calls = [];
    app.importedRecognitionRequest = async (id, method) => { calls.push([id, method]); return match(id); };
    app.selectFile('one');
    await flush();
    assert.deepStrictEqual(calls, [['one', 'POST']]);
    assert.strictEqual(app.selectedRelease.id, 123);
    assert.strictEqual(app.detections, 1);
    assert.deepStrictEqual(app.selectedSides, ['A', 'B'], 'imports use duration to infer sides, not empty recording markers');
    app.syncImportedRecognition(match('one'));
    assert.strictEqual(app.detections, 1);
    assert.strictEqual(app.songRecognition.song, null, 'import match never changes the live recorder');

    for (const mode of ['opt_out', 'missing_token']) {
        const a = setup();
        if (mode === 'opt_out') a.identifyImports = false;
        else a.songRecognition.configured = false;
        a.importedRecognitionRequest = () => { throw new Error('must not upload'); };
        a.selectFile('one');
        assert.strictEqual(a.currentFile.recognition, undefined);
    }

    // Late matches are saved to their own file, never applied to a different file.
    const late = setup();
    let complete;
    late.importedRecognitionRequest = () => new Promise(resolve => { complete = resolve; });
    late.selectFile('one');
    late.identifyImports = false;
    late.selectFile('two');
    complete(match('one'));
    await flush();
    assert.strictEqual(late.selectedRelease, null);
    assert.strictEqual(late.uploadedFiles[0].recognition.song.title, 'Song');
    late.selectFile('one');
    assert.strictEqual(late.selectedRelease.id, 123);

    for (const edit of ['release', 'boundaries', 'tracks', 'removed']) {
        const a = setup();
        let done;
        a.importedRecognitionRequest = () => new Promise(resolve => { done = resolve; });
        a.selectFile('one');
        if (edit === 'release') a.selectRelease({...release, id: 456});
        if (edit === 'boundaries') a.markEdited();
        if (edit === 'tracks') a.detectedTracks = [{start: 0, end: 300}];
        if (edit === 'removed') { a.uploadedFiles = []; a.currentFile = null; a.currentFileId = null; }
        const detections = a.detections;
        done(match('one'));
        await flush();
        assert.strictEqual(a.detections, detections, edit);
        if (edit === 'release') assert.strictEqual(a.selectedRelease.id, 456);
    }

    const ambiguous = setup();
    ambiguous.importedRecognitionRequest = async id => match(id, {selected_release: null});
    ambiguous.selectFile('one');
    await flush();
    assert.strictEqual(ambiguous.selectedRelease, null);
    assert.strictEqual(ambiguous.currentFile.recognition.candidates.length, 1);
    ambiguous.importedRecognitionRequest = async (id, method, selected) => match(id, {selection: 'manual'});
    await ambiguous.chooseImportedAlbum(123);
    assert.strictEqual(ambiguous.selectedRelease.id, 123);

    const failure = setup();
    failure.importedRecognitionRequest = async () => { throw new Error('AudD unavailable'); };
    failure.selectFile('one');
    await flush();
    assert.strictEqual(failure.importedRecognitionBusy, false);
    assert.strictEqual(failure.currentFile.recognitionNotice, 'AudD unavailable');
    assert.strictEqual(failure.recordingError, '');

    // Resume progress after switching back; once matched, polling applies the result.
    const poll = setup();
    poll.identifyImports = false;
    poll.selectFile('one');
    poll.currentFile.recognition = match('one', {state: 'looking_up', selected_release: null});
    poll.importedRecognitionRequest = async id => match(id);
    await poll.pollImportedRecognition('one');
    assert.strictEqual(poll.selectedRelease.id, 123);
    assert.strictEqual(poll.currentFile.recognitionPolling, false);
})().catch(error => { console.error(error); process.exit(1); });
