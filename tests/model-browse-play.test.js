const test=require('node:test'), assert=require('node:assert/strict'), M=require('../Model.js')
const st={features:{browse_actions:1},status:'ok',zone:{id:'fixture-zone'},selection:{mode:'pinned',zone_id:'fixture-zone',availability:'available'}}
const row={can_play:true,can_navigate:true}
test('synthetic feature contract accepts only numeric version 1 and chooses legacy routes before dispatch',()=>{
 for(const features of [undefined,null,[],true,{}, {browse_actions:true},{browse_actions:'1'},{browse_actions:0},{browse_actions:2}]){
  const state={...st,features}
  assert.equal(M.browseActionsSupported(state),false)
  assert.equal(M.browsePlayRequest(state,row,0,1,'r','play_now'),null)
  for(const op of ['enter','back','page']) assert.equal(M.browseNavRoute(state,op),op)
 }
 assert.equal(M.browseActionsSupported(st),true)
 for(const op of ['enter','back','page']) assert.equal(M.browseNavRoute(st,op),op+'_nav')
 for(const op of ['library','constructor','toString']) assert.equal(M.browseNavRoute(st,op),null)
 for(const selection of [null,[],{}, {...st.selection,mode:'foreign'}])
  assert.equal(M.browsePlayRequest({...st,selection},row,0,1,'r','play_now'),null)
})
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
 for(const action of [undefined,'Queue','Add Next','Play Now','start_radio','',null,{toString:()=> 'play_now'}])assert.equal(M.browsePlayRequest(st,row,2,17,'r',action),null)
 assert.equal(M.browsePlayRequest(st,{can_play:true,can_descend:true,can_navigate:false},2,17,'r','play_now'),null)
})
test('browse outcome copy requires matching action and never says played or queued',()=>{
 const req={request_id:'fixture-request',zone_id:'fixture-zone',action:'play_now'}
 const reply={v:1,ok:true,...req,outcome:'attempted',error:null}
 assert.match(M.browsePlayReplyText(req,reply),/Play requested/i)
 assert.doesNotMatch(M.browsePlayReplyText(req,reply),/played|queued/i)
 for(const bad of [null,{...reply,request_id:'wrong'},{...reply,zone_id:'other'},{...reply,outcome:'success'},{...reply,action:'play_next'},{...reply,action:'Queue'}])assert.match(M.browsePlayReplyText(req,bad),/unknown/i)
 assert.match(M.browsePlayReplyText(req,{...reply,ok:false,outcome:'refused',error:'unsupported_prompt'}),/input|confirmation/i)
 assert.match(M.browsePlayReplyText(req,{...reply,ok:false,outcome:'unknown',error:'roon_error'}),/unknown/i)
 assert.match(M.browsePlayReplyText(req,{...reply,ok:false,outcome:'refused',error:'no_action'}),/No Play Now/)
 for(const [action,attempted,missing] of [['play_next',/Play Next requested/,/No Play Next/],['add_to_queue',/Add to Queue requested/,/No Add to Queue/]]){
  const queued={...req,action}
  const ok={...reply,action,outcome:'attempted',error:null}
  assert.match(M.browsePlayReplyText(queued,ok),attempted)
  assert.doesNotMatch(M.browsePlayReplyText(queued,ok),/added|queued|played/i)
  assert.match(M.browsePlayReplyText(queued,{...ok,ok:false,outcome:'refused',error:'no_action'}),missing)
  assert.match(M.browsePlayReplyText(queued,{...ok,ok:false,outcome:'unknown',error:'roon_error'}),/unknown/i)
  assert.match(M.browsePlayReplyText(queued,{...ok,action:'play_now'}),/unknown/i)
 }
})
test('synthetic result requires present own mandatory fields and consistent boolean ok',()=>{
 const req={request_id:'fixture-request',zone_id:'fixture-zone',action:'play_now'}
 const good={v:1,ok:true,...req,outcome:'attempted',error:null}
 for(const field of ['v','ok','request_id','zone_id','action','outcome','error']) {
  const bad={...good};delete bad[field]
  assert.equal(M.validBrowsePlayReply(req,bad),false,`missing ${field}`)
  Object.setPrototypeOf(bad,{[field]:good[field]})
  assert.equal(M.validBrowsePlayReply(req,bad),false,`inherited ${field}`)
 }
 for(const [field,value] of [['v',true],['v','1'],['ok',false],['ok',1],['ok','true'],['request_id',null],['zone_id',[]],['action',{}],['outcome',true],['error',false],['error','']])
  assert.equal(M.validBrowsePlayReply(req,{...good,[field]:value}),false,field)
 for(const bad of [null,[],true,'result',42,Object.assign([] ,good),Object.assign(function(){},good)])
  assert.equal(M.validBrowsePlayReply(req,bad),false)
 for(const outcome of ['refused','unknown']) {
  const reply={...good,ok:false,outcome,error:'no_action'}
  for(const [field,value] of [['ok',true],['ok',0],['ok',null],['error',null],['error',''],['error','x'.repeat(65)],['error',{}],['outcome',[]],['outcome','success']])
   assert.equal(M.validBrowsePlayReply(req,{...reply,[field]:value}),false,`${outcome} ${field}`)
 }
})
test('synthetic valid result envelopes keep optional current page fields',()=>{
 const req={request_id:'fixture-request',zone_id:'fixture-zone',action:'play_now'}
 for(const [outcome,error] of [['attempted',null],['refused','no_action'],['unknown','roon_error']]) {
  const reply={v:1,ok:outcome==='attempted',...req,outcome,error,rows:[],path:['Library'],level_id:18,count:0,offset:0}
  assert.equal(M.validBrowsePlayReply(req,reply),true,outcome)
 }
})
