const fs = require('fs'), path = require('path'), assert = require('assert');
const read = name => fs.readFileSync(path.join(__dirname, '../../backend/static', name), 'utf8');
const vinylApp = new Function(read('flow.js') + '\n' + read('app.js') + ';return vinylApp;')();
(async () => {
    const app = vinylApp();
    app.recorder = { state: 'recording', id: 'first' };
    app.view = 'record'; app.flow.step = 'record';
    app.flow.release = {id: 42}; app.flow.releaseOrigin = 'manual';
    app.currentFileId = 'editing-other'; app.selectedRelease = {id: 99};
    app.notify = () => {}; app.loadSavedRecordings = async () => {};
    app.selectFile = () => { throw Error('Queued stop must not switch the editor'); };
    app.recordingRequest = async (action, body) => {
        assert.equal(action, 'stop'); assert.equal(body.auto_export, true); assert.equal(body.release_id, 42);
        return {id: 'first', state: 'finished', file: {id: 'first', filename: 'First.wav'}, queued_job: {id: 'first', state: 'queued'}};
    };
    await app.flowFinish();
    assert.equal(app.flow.step, 'record'); assert.equal(app.flow.release, null);
    assert.equal(app.currentFileId, 'editing-other'); assert.equal(app.selectedRelease.id, 99);
    assert.equal(app.recordingBusy, false); assert.equal(app.activeFinishJobs.length, 1);
    app.songRecognition = {recording_id:'first', selected_release:{id:42, tracks:[{}]}};
    app.onRecognitionUpdated(app.songRecognition);
    assert.equal(app.flow.release, null, 'old recognition cannot repopulate next record');
    app.recorder = {state:'recording', id:'second'};
    app._recordingDisposed = true; // no real polling timer in unit test
    app.recordingRequest = async () => ({jobs:[{id:'first',state:'needs_review',score:90}]});
    app._recordingDisposed = false;
    const timeout = global.setTimeout; global.setTimeout = () => 0;
    await app.pollFinishQueue(); global.setTimeout = timeout;
    assert.equal(app.recorder.id, 'second'); assert.equal(app.reviewFinishJobs.length, 1);
    app.savedRecordings = [{id:'first'}, {id:'unqueued'}];
    assert.deepEqual(app.unfinishedRecordings, [{id:'unqueued'}]);
    app.destroy();
})().catch(error => {console.error(error); process.exit(1)});
