const test=require('node:test')
const assert=require('node:assert/strict')
const J=require('../tools/queue-preview/Journal.js')
test('history is bounded, ordered and detached from caller fields',()=>{
 let history=J.empty(),fields={open:true,focus:false,verb:'playpause'}
 history=J.append(history,'blocked-send',fields);fields.verb='mutated'
 assert.equal(history.events[0].verb,'playpause')
 for(let i=0;i<200;i++)history=J.append(history,'key-move',{cursor:i})
 assert.equal(history.events.length,128);assert.equal(history.sequence,201)
 assert.equal(history.events[0].sequence,74);assert.equal(history.events.at(-1).cursor,199)
})
test('only bounded diagnostic scalar fields survive, never bodies or key text',()=>{
 let h=J.append(J.empty(),'key-text',{text:'private input',body:{items:['not allowed']},request:'x'.repeat(200),open:true,cursor:Infinity})
 assert.equal(h.events[0].text,undefined);assert.equal(h.events[0].body,undefined)
 assert.equal(h.events[0].request.length,96);assert.equal(h.events[0].cursor,undefined)
})
test('a close observation preserves the earlier key and blocked-send history',()=>{
 let a=J.append(J.empty(),'key-activate',{queueOpen:true,focus:true})
 const b=J.append(a,'popup-open',{open:false,focus:false})
 assert.equal(a.events.length,1);assert.equal(b.events.length,2)
 assert.equal(b.events[0].queueOpen,true);assert.equal(b.events[1].open,false)
})
