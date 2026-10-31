pragma Singleton
import QtQuick
import "Fixtures.js" as Fixtures
Item {
  id: root
  property var state: Fixtures.states.navigation4
  property double receivedAt: Date.now()
  property string playerError: ""
  property string fixtureName: "navigation4"
  property int consumers: 0
  property int blockedCommands: 0
  property int blockedBrowse: 0
  property var calls: []
  property var pending: []
  property bool hold: false
  property string behavior: "normal"
  property int level: 0
  property var current: ({ok:true,v:1,level_id:0,count:0,offset:0,path:[],rows:[]})
  function attach() { consumers++ }
  function detach() { consumers=Math.max(0,consumers-1) }
  function send() { blockedCommands++; return false }
  function selectFixture(name) {
    if (!Object.prototype.hasOwnProperty.call(Fixtures.states,name)) return "unknown-fixture"
    state=Fixtures.states[name];receivedAt=Date.now();fixtureName=name;return "ok"
  }
  function adopt(offset,path) {
    level++
    var total=behavior === "empty" ? 0 : 205, rows=[]
    for (var i=offset;i<Math.min(total,offset+100);i++) {
      var nav=i!==1 && i!==2
      rows.push({title:nav ? "Preview folder "+String(i) : i===1 ? "Playback action (disabled)" : "Input action (disabled)",
        subtitle:"Synthetic library row",image_key:null,can_descend:nav,can_play:!nav,can_navigate:nav})
    }
    current={v:1,ok:true,level_id:level,count:total,offset:offset,path:path,rows:rows}
    return current
  }
  function browse(session,args,callback) {
    blockedBrowse++
    calls=calls.concat([{session:session,args:args.slice()}])
    var op=args[0], reply
    if (behavior === "error" && op!=="reset") reply={v:1,ok:false,error:"roon_error",message:"Preview load failed"}
    else if (behavior === "malformed" && op!=="reset") reply={v:1,ok:true,level_id:level+1,count:205,offset:0,path:["Library"],rows:{bad:true}}
    else if (op === "reset") {
      level++;current={v:1,ok:true,level_id:level,count:0,offset:0,path:[],rows:[]};reply=current
    } else if (op === "library") reply=adopt(0,["Library"])
    else if ((op === "page_nav" || op === "back_nav") && Number(args[2])!==level) {
      reply=Object.assign({},current,{ok:false,error:"stale",message:"Preview stale view"})
    } else if (op === "page_nav") reply=adopt(Number(args[1]),current.path.slice())
    else if (op === "back_nav") reply=adopt(Number(args[1]),current.path.slice(0,-1))
    else if (op === "enter_nav" && Number(args[2])===level && current.rows[Number(args[1])] && current.rows[Number(args[1])].can_navigate) {
      reply=adopt(0,current.path.concat([current.rows[Number(args[1])].title]))
    } else reply={v:1,ok:false,error:"preview_only",message:"Preview blocks search/play/other operations"}
    if (hold) {
      if (pending.length>=16) throw new Error("Preview pending bound reached")
      pending=pending.concat([{callback:callback,reply:reply}])
    } else Qt.callLater(function(){callback(reply)})
  }
  function flushFirst() {
    if (!pending.length) return 0
    var first=pending[0];pending=pending.slice(1);first.callback(first.reply);return 1
  }
  function flush() {
    var batch=pending;pending=[]
    for (var i=0;i<batch.length;i++) batch[i].callback(batch[i].reply)
    return batch.length
  }
}
