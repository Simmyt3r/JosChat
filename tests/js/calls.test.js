// Execute the actual joinCallChannel function with controlled transport/browser
// boundaries. These tests do not claim live Supabase or NAT traversal coverage.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../static/js/app.js'), 'utf8');
const join = source.slice(source.indexOf('async function joinCallChannel()'), source.indexOf('\nfunction teardownCall('));
function fixture(fetch) {
  const events = {}, state = { pcs: [], channels: [], ended: [], notices: [] };
  const channel = { on(type, config, callback) { events[`${type}:${config.event}`] = callback; return this; },
    subscribe(cb) { state.status = cb; return this; }, track: async () => {}, send() {}, presenceState: () => ({}) };
  const client = { realtime: { setAuth: async (token) => { state.token = token; } },
    channel: (topic, config) => { state.channels.push({ topic, config }); return channel; } };
  const call = { id: 7, role: 'caller', phase: 'ringing-out', localStream: { getTracks: () => [] } };
  const ctx = vm.createContext({ activeCall: call, currentProfile: {id: 'alice'}, accessToken: 'test-token',
    ensureSupabaseClient: () => client,
    apiFetch: fetch || (async () => ({ ok: true, json: async () => ({ ice_servers: [{urls: 'turns:relay.test:5349', username:'temporary', credential:'temporary'}] }) })),
    readJson: (r) => r.json(),
    RTCPeerConnection: class { constructor(config) {state.pcs.push(config);} addTrack() {} },
    endCallRow: (id,status) => state.ended.push({id,status}),
    teardownCall: (notice) => { state.notices.push(notice); ctx.activeCall = null; },
    console, clearTimeout, startDurationTimer() {}, $: () => ({ dataset: {} }) });
  vm.runInContext(join, ctx);
  return { ctx, state, call };
}
test('calls use private channels, authenticated Realtime and configured relay servers', async () => {
  const {ctx,state} = fixture(); await ctx.joinCallChannel();
  assert.equal(state.token,'test-token');
  assert.equal(state.channels[0].topic,'call-7');
  assert.equal(state.channels[0].config.config.private,true);
  assert.equal(state.pcs[0].iceServers[0].urls,'turns:relay.test:5349');
});
test('unavailable call configuration fails closed before opening a channel', async () => {
  const {ctx,state} = fixture(async () => ({ok:false,json:async()=>({error:'unavailable'})}));
  await ctx.joinCallChannel();
  assert.equal(state.channels.length,0); assert.equal(state.pcs.length,0);
  assert.equal(state.ended[0].status,'failed'); assert.equal(ctx.activeCall,null);
});
test('hanging up while configuration loads does not create a stale connection', async () => {
  let finish; const pending = new Promise(resolve => { finish=resolve; });
  const {ctx,state} = fixture(() => pending);
  const joined=ctx.joinCallChannel(); ctx.activeCall=null;
  finish({ok:true,json:async()=>({ice_servers:[{urls:'stun:relay.test'}]})}); await joined;
  assert.equal(state.channels.length,0); assert.equal(state.pcs.length,0);
});
test('private channel rejection fails the call and clears its state', async () => {
  const {ctx,state} = fixture(); await ctx.joinCallChannel(); await state.status('CHANNEL_ERROR');
  assert.equal(ctx.activeCall,null); assert.equal(state.ended[0].status,'failed');
});
