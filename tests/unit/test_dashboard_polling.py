"""Delayed Dashboard requests must not be starved by the polling interval."""
from pathlib import Path
import subprocess


def test_slow_polling_refresh_order_and_unmount():
    path = Path(__file__).resolve().parents[2] / 'hermes/plugins/orchestration/dashboard/dist/index.js'
    source = path.read_text()
    hook = source[source.index('  function usePoll(path) {'):source.index('  // ------------------------------------------------------------ small pieces')]
    program = r'''
const assert = require('node:assert/strict');
let state, timer, cleanup;
const requests = [];
const useState = initial => { state = initial; return [state, value => {state = typeof value === 'function' ? value(state) : value;}]; };
const useRef = current => ({current});
const useCallback = fn => fn;
const useEffect = fn => {cleanup = fn();};
const setInterval = fn => {timer = fn; return 1;};
const clearInterval = () => {};
const POLL_MS = 10000, message = e => e.message;
const api = () => new Promise((resolve, reject) => {requests.push({resolve, reject});});
HOOK
(async () => {
 const view = usePoll('/tasks/T-13');
 assert.equal(requests.length, 1);
 for (let i=0; i<5; ++i) timer();
 assert.equal(requests.length, 1, 'slow requests must not be superseded on each timer tick');
 requests[0].resolve({version: 1});
 await new Promise(setImmediate);
 assert.equal(state.data.version, 1);
 timer();
 assert.equal(requests.length, 2);
 view.reload();
 assert.equal(requests.length, 3, 'an explicit refresh after an action must fetch current state');
 requests[2].resolve({version: 3});
 await new Promise(setImmediate);
 requests[1].resolve({version: 2});
 await new Promise(setImmediate);
 assert.equal(state.data.version, 3, 'an older reply cannot overwrite refreshed state');
 timer();
 assert.equal(requests.length, 4);
 cleanup();
 requests[3].resolve({version: 4});
 await new Promise(setImmediate);
 assert.equal(state.data.version, 3, 'closed views must not accept late replies');
})().catch(e => {console.error(e); process.exit(1);});
'''
    subprocess.run(['node', '-e', program.replace('HOOK', hook)], check=True, capture_output=True, text=True)
