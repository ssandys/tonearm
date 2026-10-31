const test=require('node:test'), assert=require('node:assert/strict'), M=require('../Model.js')
const st={status:'ok',zone:{id:'fixture-zone'},selection:{mode:'pinned',zone_id:'fixture-zone',availability:'available'}}
const row={can_play:true,can_navigate:true}
test('browse play binds row level action and available selected identity',()=>{
 const r=M.browsePlayRequest(st,row,2,17,'fixture-request','play_now')
 assert.deepEqual(r,{index:2,level_id:17,zone_id:'fixture-zone',request_id:'fixture-request',action:'play_now'})
 for(const action of ['play_next','add_to_queue']){
  const queued=M.browsePlayRequest(st,row,2,17,'fixture-request',action)
  assert.equal(queued.action,action)
 }
 for(const s of [null,{...st,status:'unreachable'},{...st,selection:{...st.selection,availability:'missing'}},{...st,selection:{...st.selection,zone_id:'other'}}])assert.equal(M.browsePlayRequest(s,row,2,17,'r','play_now'),null)
 for(const [i,l,r] of [[true,17,'r'],[-1,17,'r'],[2,-1,'r'],[2,17,'x'.repeat(65)]])assert.equal(M.browsePlayRequest(st,row,i,l,r,'add_to_queue'),null)
 assert.equal(M.browsePlayRequest(st,{can_play:false},2,17,'r','play_next'),null)
 for(const action of [undefined,'Queue','Add Next','Play Now','start_radio','',null])assert.equal(M.browsePlayRequest(st,row,2,17,'r',action),null)
})
test('browse outcome copy requires matching action and never says played or queued',()=>{
 const req={request_id:'fixture-request',zone_id:'fixture-zone',action:'play_now'}
 const reply={v:1,...req,outcome:'attempted',error:null}
 assert.match(M.browsePlayReplyText(req,reply),/Play requested/i)
 assert.doesNotMatch(M.browsePlayReplyText(req,reply),/played|queued/i)
 for(const bad of [null,{...reply,request_id:'wrong'},{...reply,zone_id:'other'},{...reply,outcome:'success'},{...reply,action:'play_next'},{...reply,action:'Queue'}])assert.match(M.browsePlayReplyText(req,bad),/unknown/i)
 assert.match(M.browsePlayReplyText(req,{...reply,outcome:'refused',error:'unsupported_prompt'}),/input|confirmation/i)
 assert.match(M.browsePlayReplyText(req,{...reply,outcome:'unknown',error:'roon_error'}),/unknown/i)
 assert.match(M.browsePlayReplyText(req,{...reply,outcome:'refused',error:'no_action'}),/No Play Now/)
 for(const [action,attempted,missing] of [['play_next',/Play Next requested/,/No Play Next/],['add_to_queue',/Add to Queue requested/,/No Add to Queue/]]){
  const queued={...req,action}
  const ok={...reply,action,outcome:'attempted',error:null}
  assert.match(M.browsePlayReplyText(queued,ok),attempted)
  assert.doesNotMatch(M.browsePlayReplyText(queued,ok),/added|queued|played/i)
  assert.match(M.browsePlayReplyText(queued,{...ok,outcome:'refused',error:'no_action'}),missing)
  assert.match(M.browsePlayReplyText(queued,{...ok,outcome:'unknown',error:'roon_error'}),/unknown/i)
  assert.match(M.browsePlayReplyText(queued,{...ok,action:'play_now'}),/unknown/i)
 }
})
