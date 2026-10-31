const test=require('node:test')
const assert=require('node:assert/strict')
const M=require('../Model.js')
function page(offset=0,length=100,level=1){return {ok:true,level_id:level,count:205,offset,path:['Library'],rows:Array.from({length},(_,i)=>({title:String(offset+i),can_navigate:true}))}}
test('one bounded page replaces, preserves relative cursor and clamps a short final page',()=>{
 const a=M.browsePage(page(),12);assert.equal(a.rows.length,100);assert.equal(a.cursor,12)
 const b=M.browsePage(page(100,100,2),a.cursor);assert.equal(b.rows[0].title,'100');assert.equal(b.rows.length,100);assert.equal(b.cursor,12)
 const c=M.browsePage(page(200,5,3),99);assert.equal(c.cursor,4);assert.equal(c.rows.length,5)
 assert.equal(M.browsePage(page(0,101),0),null)
})
test('paging offsets use nominal page size for back from a short tail',()=>{
 assert.deepEqual(M.browsePaging(0,100,205),{previous:null,next:100,label:'1–100 of 205'})
 assert.deepEqual(M.browsePaging(200,5,205),{previous:100,next:null,label:'201–205 of 205'})
 assert.deepEqual(M.browsePaging(0,0,0),{previous:null,next:null,label:'0 results'})
})
test('malformed page cannot become render/action state',()=>{
 for(const p of [null,{...page(),level_id:true},{...page(),offset:-1},{...page(),count:NaN},{...page(),rows:{}},{...page(),path:'Library'}])assert.equal(M.browsePage(p,0),null)
})
test('view token rejects replies after close/reset/context change or a newer request',()=>{
 const t={view:1,request:2,context:'ctx'}
 assert.equal(M.browseReplyCurrent(t,{...t},1,'ctx'),true)
 assert.equal(M.browseReplyCurrent(t,null,1,'ctx'),false)
 assert.equal(M.browseReplyCurrent(t,{...t,request:3},1,'ctx'),false)
 assert.equal(M.browseReplyCurrent(t,t,2,'ctx'),false)
 assert.equal(M.browseReplyCurrent(t,t,1,'different'),false)
})
test('seek/volume metadata changes do not invalidate browse; selection/status/session do',()=>{
 const s={status:'ok',core:{host:'fixture'},zone:{id:'a',position:1},selection:{mode:'pinned',zone_id:'a',availability:'available'}}
 const key=M.browseContext(s,'widget')
 assert.equal(M.browseContext({...s,zone:{id:'a',position:2}},'widget'),key)
 assert.notEqual(M.browseContext({...s,selection:{...s.selection,zone_id:'b'}},'widget'),key)
 assert.notEqual(M.browseContext({...s,status:'unreachable'},'widget'),key)
 assert.notEqual(M.browseContext(s,'different'),key)
})
test('navigation admission never turns a playback or input row into activation',()=>{
 assert.equal(M.canNavigateRow({can_navigate:true}),true)
 for(const row of [null,{can_descend:true,can_play:true},{can_navigate:false},{can_navigate:'true'}])assert.equal(M.canNavigateRow(row),false)
})
