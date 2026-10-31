const test=require('node:test')
const assert=require('node:assert/strict')
const Q=require('../QueueModel.js')
const state=(id='a')=>({status:'ok',core:{host:'synthetic'},zone:{id,name:'Fixture room'},selection:{zone_id:id,availability:'available',mode:'pinned'}})
const request=()=>Q.request(state(),'fixture-1')
const reply=()=>({v:1,type:'queue_state',zone_id:'a',request_id:'fixture-1',status:'ready',error:null,items:[{id:1,title:'Synthetic track',subtitle:'Fixture artist'}],revision:1,limit:100,schema:{}})
test('request only uses available selected identity, never missing-pin fallback',()=>{
 assert.deepEqual(request(),{cmd:'queue_status',v:1,request_id:'fixture-1',zone_id:'a'})
 for(const availability of ['missing','unknown','none'])assert.equal(Q.request({...state(),selection:{zone_id:'missing',availability}},'r'),null)
 assert.equal(Q.request({...state(),status:'unreachable'},'r'),null)
 assert.equal(Q.request({...state(),zone:{id:'b'}},'r'),null)
 assert.equal(Q.request(state(),'x'.repeat(65)),null)
 assert.equal(Q.request({status:'ok',zone:{id:'legacy'}},'r').zone_id,'legacy')
})
test('identity context changes on selection/core/status, not metadata',()=>{
 assert.equal(Q.context(state()),Q.context({...state(),zone:{...state().zone,position:42}}))
 assert.notEqual(Q.context(state()),Q.context(state('b')))
 assert.notEqual(Q.context(state()),Q.context({...state(),core:{host:'other'}}))
})
test('reply is bounded, correlated, detached and action-free',()=>{
 let a=reply(),value=Q.reply(a,request());assert.equal(value.items.length,1)
 a.items[0].title='mutated';assert.equal(value.items[0].title,'Synthetic track')
 for(const bad of [{...reply(),request_id:'old'},{...reply(),zone_id:'b'},{...reply(),items:Array(101).fill(reply().items[0])},{...reply(),status:'loading'},{...reply(),revision:true}])assert.equal(Q.reply(bad,request()),null)
 a=reply();a.items[0].play='address';assert.equal(Q.reply(a,request()),null)
 a=reply();a.items[0].title='🙂'.repeat(256);assert.ok(Q.reply(a,request()))
 a.items[0].title='🙂'.repeat(257);assert.equal(Q.reply(a,request()),null)
})
test('empty success and honest unavailable/loading/error copy remain distinct',()=>{
 for(const status of ['unknown','loading','ready','unavailable','disconnected','error']){
  const r={...reply(),status,items:[]};assert.equal(Q.reply(r,request()).status,status)
 }
 assert.match(Q.message('ready',0),/No items/)
 assert.match(Q.message('loading',0),/Loading/)
 assert.match(Q.message('unknown',0),/Waiting/)
 assert.match(Q.message('unavailable',0),/unavailable/)
 assert.match(Q.message('error',0),/could not/)
})
test('closed, replaced or stale views cannot accept replies',()=>{
 const token={view:2,context:Q.context(state()),request:request()}
 assert.equal(Q.current(token,token,2,Q.context(state()),true),true)
 for(const args of [[token,token,3,Q.context(state()),true],[token,token,2,Q.context(state('b')),true],[token,token,2,Q.context(state()),false],[token,{...token},2,Q.context(state()),true]])assert.equal(Q.current(...args),false)
})

test('duplicate IDs, excess encoded bytes and unsupported status fail closed',()=>{
 let a=reply();a.items.push({...a.items[0]});assert.equal(Q.reply(a,request()),null)
 a=reply();a.items=Array.from({length:100},(_,i)=>({id:i,title:'🙂'.repeat(256),subtitle:'🙂'.repeat(256)}))
 assert.equal(Q.reply(a,request()),null)
 assert.equal(Q.reply({...reply(),status:'surprise'},request()),null)
})

test('header uses the selected whole-queue total, including zero and hours',()=>{
 const timed=(seconds,extra={})=>({...state(),zone:{...state().zone,state:'playing',queue_time_remaining:seconds,...extra}})
 assert.equal(Q.header(timed(3572)),'Queue (59:32)')
 assert.equal(Q.header(timed(3571)),'Queue (59:31)')
 assert.equal(Q.header(timed(0)),'Queue (0:00)')
 assert.equal(Q.header(timed(3723.9)),'Queue (1:02:03)')
 assert.equal(Q.header(timed(7200)),'Queue (2:00:00)')
 assert.equal(Q.header(timed(3572,{state:'paused'})),'Queue (59:32)')
 assert.equal(Q.header(timed(4000)),'Queue (1:06:40)') // addition/seek: use the latest total
 for(const seconds of [undefined,null,true,'3572',-1,NaN,Infinity,2147483649])
  assert.equal(Q.header(timed(seconds)),'Queue')
 assert.equal(Q.header({...timed(3572),status:'unreachable'}),'Queue')
 assert.equal(Q.header({...timed(3572),selection:{zone_id:'b',availability:'available'}}),'Queue')
 assert.equal(Q.header({...timed(3572),selection:{zone_id:'a',availability:'missing'}}),'Queue')
 assert.equal(Q.header(null),'Queue')
})
