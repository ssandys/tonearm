const test = require('node:test')
const assert = require('node:assert/strict')
const M = require('../Model.js')
const fixture = require('../tools/visual-preview/states.json')
function state() {
  const st = JSON.parse(JSON.stringify(fixture.playing))
  st.zone.controls = {transport:true,seek:true,volume:true,volume_output_id:'out-a',mute_output_ids:['out-a']}
  return st
}
test('player request captures zone and appropriate output identity at click', () => {
  const st = state()
  const req = M.playerRequest(st,'volume',20,'test-1')
  assert.equal(req.cmd,'player_targeted');assert.equal(req.zone_id,'fixture-dac')
  assert.equal(req.output_id,'out-a');assert.equal(req.arg,20)
  st.zone.id='other';st.zone.controls.volume_output_id='out-b'
  assert.equal(req.zone_id,'fixture-dac');assert.equal(req.output_id,'out-a')
  const mute=M.playerRequest(state(),'mute',undefined,'m')
  assert.deepEqual(mute.output_ids,['out-a'])
})
test('unknown/missing/disconnected and old-daemon snapshots disable player controls', () => {
  for (const name of ['initial','connecting','unknown','missing','disconnected']) {
    assert.equal(M.canPlayer(fixture[name],'playpause'),false,name)
    assert.equal(M.playerRequest(fixture[name],'playpause',undefined,'x'),null)
  }
  const st=state();delete st.zone.controls
  assert.equal(M.canPlayer(st,'pause'),false)
  const missing=state();missing.selection.zone_id='missing'
  assert.equal(M.canPlayer(missing,'pause'),false)
})
test('seek/volume permission and numeric/range checks do not use presentation defaults', () => {
  const st=state();st.zone.controls.seek=false
  assert.equal(M.playerRequest(st,'seek',2,'x'),null)
  st.zone.controls.volume=false
  assert.equal(M.playerRequest(st,'volume',2,'x'),null)
  for (const arg of [true,NaN,Infinity,-1,101,1.5]) assert.equal(M.playerRequest(state(),'volume',arg,'x'),null)
  assert.equal(M.playerRequest(state(),'seek',241,'x'),null)
  const fixed=state();fixed.zone.volume=null;fixed.zone.controls.volume=false
  assert.equal(M.canPlayer(fixed,'volume'),false)
})
test('unsupported operations cannot use player protocol or legacy fallback', () => {
  for (const verb of ['transfer','zone','browse','unknown']) assert.equal(M.playerRequest(state(),verb,undefined,'x'),null)
})
test('reply must match request and conveys refusal/unknown rather than remote success', () => {
  const req=M.playerRequest(state(),'pause',undefined,'r')
  const ok={v:1,type:'command_result',request_id:'r',zone_id:req.zone_id,verb:'pause',outcome:'attempted',error:null}
  assert.equal(M.playerReplyText(req,ok),'')
  assert.match(M.playerReplyText(req,{...ok,request_id:'wrong'}),/unknown/i)
  assert.match(M.playerReplyText(req,{...ok,outcome:'unknown',error:'dispatch_failed'}),/unknown/i)
  assert.match(M.playerReplyText(req,{...ok,outcome:'refused',error:'selection_changed'}),/selection/i)
  assert.match(M.playerReplyText(req,{...ok,outcome:'refused',error:'constructor'}),/unknown/i)
})

test('incremental output can mute without fabricating an absolute slider', () => {
  const st=state();st.zone.volume=null
  st.zone.controls.volume=false;st.zone.controls.volume_output_id=null;st.zone.controls.muted=true
  assert.equal(M.canPlayer(st,'volume'),false)
  assert.equal(M.canPlayer(st,'mute'),true)
  assert.equal(M.playerMuted(st),true)
  assert.deepEqual(M.playerRequest(st,'unmute',undefined,'m').output_ids,['out-a'])
})
