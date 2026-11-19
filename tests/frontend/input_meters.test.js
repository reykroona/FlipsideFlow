const fs = require('fs'), path = require('path'), assert = require('assert');
const read = name => fs.readFileSync(path.join(__dirname, '../../backend/static', name), 'utf8');
const vinylApp = new Function(read('flow.js') + '\n' + read('app.js') + ';return vinylApp;')();
(async () => {
    const app = vinylApp();
    assert.equal(app.recordingMeterWidth(-12), 80);
    assert.equal(app.recordingMeterWidth(-6), 90);
    assert.equal(app.recordingMeterWidth(0), 100);
    assert.equal(app.recordingMeterWidth(-120), 0);
    assert.equal(app.recordingMeterWidth(undefined), 0);
    assert.equal(app.recordingDbLabel(-120), '−∞');
    assert.equal(app.recordingDbLabel(-12), '-12.0');
    app.recorder = {state:'recording', id:'record', levels:[-30,-40], peak_hold:[-6,0], clipping:[false,true], seconds:42};
    assert.equal(app.recordingPeak(0), -6);
    assert.equal(app.recordingClipChannels, 'R');
    app.recorder.clipping = [true,true];
    assert.equal(app.recordingClipChannels, 'L & R');
    // A status reply begun before Reset cannot restore the stale held peak.
    let deliver;
    app.recordingRequest = () => new Promise(resolve => { deliver = resolve; });
    const oldPoll = app.pollRecording();
    app.recordingRequest = async action => {
        assert.equal(action, 'meters/reset');
        return {state:'recording',id:'record',seconds:42,levels:[-30,-40],peak_hold:[-120,-120],clipping:[false,false]};
    };
    await app.recordingAction('meters/reset');
    deliver({enabled:true,state:'recording',id:'record',levels:[-30,-40],peak_hold:[0,0],clipping:[true,true]});
    await oldPoll;
    assert.equal(app.recordingPeak(0), -120);
    assert.equal(app.recordingClipChannels, '');
    assert.equal(app.recorder.state, 'recording');
    assert.equal(app.recorder.seconds, 42);
    app.destroy();
    // Input metering is available in both Record and Workbench.
    const html = read('index.html');
    assert.equal((html.match(/class="vf-meter-target"/g) || []).length, 2);
    assert.equal((html.match(/Reset peaks &amp; warning/g) || []).length, 2);
})().catch(error => {console.error(error);process.exit(1)});
