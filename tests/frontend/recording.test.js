const fs = require('fs');
const path = require('path');
const assert = require('assert');
const vinylApp = new Function(fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8') + '; return vinylApp;')();
const devices = [{id: 1, name: 'Built-in', channels: 2}, {id: 3, name: 'Scarlett USB', channels: 4}];

(async () => {
    const app = vinylApp();
    app.recordingRequest = async () => ({devices});
    await app.refreshRecordingDevices();
    assert.strictEqual(app.recordingInput.device, 3);
    assert.deepStrictEqual(app.recordingChannels, [1, 2, 3, 4]);
    app.recorder.state = 'paused';
    app.recordingInput.left = 3;
    await app.refreshRecordingDevices();
    assert.strictEqual(app.recordingInput.left, 3, 'reconnecting must not reset active channel settings');

    // An old status response must not overwrite a newer Pause/Stop result.
    let deliver;
    app.recordingRequest = () => new Promise(resolve => { deliver = resolve; });
    const polling = app.pollRecording();
    app._recordingSeq++;
    deliver({enabled: true, state: 'recording'});
    await polling;
    assert.strictEqual(app.recorder.state, 'paused');
    app.destroy();

    // A lost response can be retried without duplicate queue entries or split resets.
    const a = vinylApp();
    a.notify = () => {};
    let selections = 0;
    a.selectFile = id => { if (a.currentFileId !== id) selections++; a.currentFileId = id; };
    const file = {id: 'recording-id', duration: 120, filename: 'Album.wav'};
    a.acceptRecordedFile(file);
    a.acceptRecordedFile(file);
    assert.strictEqual(a.uploadedFiles.length, 1);
    assert.strictEqual(selections, 1);

    a.recorder = {state: 'recording'};
    a.recordingRequest = async (action, body) => {
        assert.strictEqual(action, 'pause');
        assert.strictEqual(body.side_flip, true);
        return {state: 'paused', seconds: 120};
    };
    await a.recordingAction('pause', {side_flip: true});
    assert.strictEqual(a.recorder.state, 'paused');
    assert.strictEqual(a.recordingBusy, false);

    a.recordingRequest = async () => { throw new Error('Microphone access is denied'); };
    await a.recordingAction('monitor', {});
    assert.match(a.recordingError, /Microphone/);
    assert.strictEqual(a.recordingBusy, false);

    const drawn = [];
    a.currentFile = {recording_side_markers: [0, 100, 200, 400]};
    a.waveformRegions = {addRegion: marker => { drawn.push(marker); return {}; }};
    a.addRecordingMarkers(300);
    assert.deepStrictEqual(drawn.map(x => x.start), [100, 200]);
    assert.ok(drawn.every(x => !x.drag && !x.resize && x.id.startsWith('recorded-flip-')));
})().catch(error => { console.error(error); process.exit(1); });
