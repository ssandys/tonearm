// Synthetic-preview diagnostics only; bounded and never attached to live Service.
function empty() { return {sequence:0,events:[]} }
function append(history,kind,fields) {
  var allowed=["open","focus","queueOpen","cursor","pending","request","zone","status","verb","mode","dx","dy","instance","action","rows","reason"]
  var event={sequence:history.sequence+1,kind:String(kind).slice(0,40)}
  fields=fields || {}
  for (var i=0;i<allowed.length;i++) {
    var key=allowed[i],value=fields[key]
    if (typeof value === "string") event[key]=value.slice(0,96)
    else if (typeof value === "boolean" || (typeof value === "number" && isFinite(value))) event[key]=value
  }
  return {sequence:event.sequence,events:history.events.slice(-127).concat([event])}
}
if (typeof module !== "undefined") module.exports={empty:empty,append:append}
