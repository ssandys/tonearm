// Pure read-only queue request/response rules, shared by Qt and node tests.
function ident(value, limit) { return typeof value === "string" && value.length > 0 && value.length <= limit }
function integer(value) { return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 }
function selection(state) {
  if (!state || state.status !== "ok") return {status:"disconnected", id:null}
  var chosen = state.selection
  if (chosen) {
    if (chosen.availability === "unknown") return {status:"unknown", id:null}
    if (chosen.availability !== "available") return {status:"unavailable", id:null}
    if (!state.zone || chosen.zone_id !== state.zone.id) return {status:"unavailable", id:null}
  }
  var id = state.zone ? state.zone.id : null
  return ident(id,128) ? {status:"available",id:id} : {status:"unavailable",id:null}
}
function context(state) {
  var s=selection(state)
  return JSON.stringify([state ? state.status : null, state && state.core ? state.core.host : null,
                         s.status, s.id, state ? state.selection : null])
}
function request(state, id) {
  var s=selection(state)
  return s.status === "available" && ident(id,64) ? {cmd:"queue_status",v:1,request_id:id,zone_id:s.id} : null
}
function bytes(text) {
  // UTF-8 bound without TextEncoder (not a portable QML global).
  var total=0
  for (var i=0;i<text.length;i++) {
    var c=text.charCodeAt(i)
    if (c<128) total++
    else if (c<2048) total+=2
    else if (c>=0xd800 && c<=0xdbff && i+1<text.length && text.charCodeAt(i+1)>=0xdc00 && text.charCodeAt(i+1)<=0xdfff) { total+=4;i++ }
    else total+=3
  }
  return total
}
function text(value) { return typeof value === "string" && value.length <= 512 && Array.from(value).length <= 256 }
function reply(value, req) {
  if (!req || !value || value.type !== "queue_state" || value.v !== 1 || value.request_id !== req.request_id || value.zone_id !== req.zone_id
      || ["unknown","loading","ready","unavailable","disconnected","error"].indexOf(value.status)<0
      || !integer(value.revision) || value.limit !== 100 || !Array.isArray(value.items) || value.items.length>100
      || (value.status !== "ready" && value.items.length>0)) return null
  if (bytes(JSON.stringify(value))+1>65536) return null
  var rows=[], seen={}
  for (var i=0;i<value.items.length;i++) {
    var row=value.items[i]
    if (!row || Object.keys(row).sort().join(",") !== "id,subtitle,title" || !(integer(row.id)||ident(row.id,128)) || !text(row.title) || !text(row.subtitle)) return null
    var key=typeof row.id+":"+row.id
    if (seen[key]) return null
    seen[key]=true
    rows.push({id:row.id,title:row.title,subtitle:row.subtitle})
  }
  return {status:value.status,items:rows,revision:value.revision}
}
function current(token, active, view, ctx, open) {
  return !!open && token === active && token.view === view && token.context === ctx
}
// Roon publishes the whole-queue countdown on its existing zone/seek
// subscription. Use those updates directly: no second countdown clock, no
// summing the limited row window, and no made-up zero for unknown totals.
function header(state) {
  var selected = selection(state), zone = state && state.zone
  var seconds = zone && zone.queue_time_remaining
  if (selected.status !== "available" || typeof seconds !== "number" ||
      !isFinite(seconds) || seconds < 0 || seconds > 2147483648) return "Queue"
  seconds = Math.floor(seconds)
  var minutes = Math.floor(seconds / 60), tail = String(seconds % 60).padStart(2, "0")
  var time = minutes < 60 ? minutes + ":" + tail :
    Math.floor(minutes / 60) + ":" + String(minutes % 60).padStart(2, "0") + ":" + tail
  return "Queue (" + time + ")"
}
function message(status,count) {
  if (status === "ready") return count ? "" : "No items in the current queue window."
  if (status === "loading") return "Loading queue…"
  if (status === "unknown") return "Waiting for the selected output…"
  if (status === "unavailable") return "Selected output unavailable."
  if (status === "disconnected") return "Roon Core disconnected."
  return "Queue could not be loaded."
}
if (typeof module !== "undefined") module.exports={header:header,selection:selection,context:context,request:request,reply:reply,current:current,message:message}
