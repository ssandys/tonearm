"""Prepare (never enable) a full-popup in-memory queue acceptance fixture."""
import argparse
import shutil
import json
import re
from pathlib import Path
import subprocess
import sys

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('output',type=Path)
parser.add_argument('--id',default='io.github.cbayschm74.tonearm-preview')
args=parser.parse_args()
if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{1,127}",args.id):
    parser.error("invalid preview ID")
root=Path(__file__).resolve().parents[2]
subprocess.run([sys.executable,str(root/'tools/search-preview/prepare.py'),str(args.output)],check=True)
p=args.output/'Service.qml'
service=p.read_text().rstrip().replace('import QtQuick', 'import QtQuick\nimport "Journal.js" as Journal',1)
shutil.copyfile(root/'tools/queue-preview/Journal.js',args.output/'Journal.js')
service=service.replace('function send() { blockedCommands++; return false }', '''function send(verb) {
    blockedCommands++
    var known=["playpause","play","pause","previous","next","seek","volume","mute","unmute","zone","transfer"]
    note("blocked-send",{verb:known.indexOf(verb)>=0 ? verb : "unknown"})
    return false
  }''')
assert service.endswith('}')
addition='''
  property string diagnosticBuild: "queue-diag-v1"
  property string instanceId: Date.now().toString(36)+"-"+Math.random().toString(36).slice(2,8)
  property var history: Journal.empty()
  function note(kind, fields) { history=Journal.append(history,kind,fields) }
  Component.onCompleted: note("instance",{instance:instanceId})
  property int queueReads: 0
  property string queueMode: "ready"
  property bool queueHold: false
  property var queuePending: []
  function queue(request, callback) {
    queueReads++
    note("queue-request",{request:request.request_id,zone:request.zone_id,mode:queueMode,pending:queuePending.length})
    var status=queueMode, rows=[]
    if (status === "ready" || status === "large") {
      var count=status === "large" ? 100 : 2
      status="ready"
      for (var i=0;i<count;i++) rows.push({id:i,title:"Synthetic queue item "+i,subtitle:"Fixture artist"})
    } else if (status === "empty") status="ready"
    var result={type:"queue_state",v:1,request_id:request.request_id,zone_id:request.zone_id,
                status:status,error:status === "error" ? "invalid_queue_event" : null,
                items:rows,revision:queueReads,limit:100,schema:{}}
    if (queueMode === "mismatch") result.request_id="stale-fixture"
    if (queueHold && queuePending.length<8) queuePending=queuePending.concat([{callback:callback,result:result}])
    else { note("queue-reply",{request:request.request_id,status:result.status,rows:rows.length}); callback(result) }
  }
  function flushQueue() {
    var pending=queuePending;queuePending=[]
    for (var i=0;i<pending.length;i++) { note("queue-flush",{request:pending[i].result.request_id,status:pending[i].result.status});pending[i].callback(pending[i].result) }
    return pending.length
  }
'''
service=service[:-1]+addition+'}\n'
for token in ('Process','Socket','WebSocket','XMLHttpRequest','tonearmctl','file://','http://'):
    assert token not in service, token
p.write_text(service)
p=args.output/'Panel.qml';panel=p.read_text().replace('io.github.cbayschm74.tonearm-preview',args.id)
manifest_path=args.output/'manifest.json'
manifest=json.loads(manifest_path.read_text());manifest['id']=args.id
manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
# Change only generated fixture signal bodies, never the production Panel.
for name,kind,fields in (
    ('onMoveRequested','key-move','dx:dx,dy:dy'),
    ('onActivateRequested','key-activate',''),
    ('onCloseRequested','key-close','')):
    line=next(line for line in panel.splitlines() if name+':' in line)
    offset=line.index('{')+1
    logging=' Service.note("'+kind+'",{open:root.opened,focus:keyCatcher.activeFocus,queueOpen:queuePane.expanded,cursor:queuePane.cursor'+(','+fields if fields else '')+'});'
    panel=panel.replace(line,line[:offset]+logging+line[offset:],1)
line=next(line for line in panel.splitlines() if 'onOpenedChanged:' in line)
body=line.split(':',1)[1].strip()
panel=panel.replace(line,'  onOpenedChanged: { Service.note("popup-open",{open:root.opened,queueOpen:queuePane.expanded,focus:keyCatcher.activeFocus}); '+body+' }',1)
panel=panel.replace('      blocked: browsePane.editing','''      blocked: browsePane.editing
      onActiveFocusChanged: Service.note("focus",{focus:keyCatcher.activeFocus,open:root.opened,queueOpen:queuePane.expanded})''',1)
panel=panel.replace('          onExpandedChanged: if (expanded)', '          onExpandedChanged: { Service.note("queue-open",{queueOpen:expanded,open:root.opened,focus:keyCatcher.activeFocus}); if (expanded)',1)
# Close the new outer generated-only body at the same one-line handler.
line=next(line for line in panel.splitlines() if 'onExpandedChanged:' in line)
panel=panel.replace(line,line+' }',1)

marker='    function inspect(): string {'
assert marker in panel
panel=panel.replace(marker,'''    function diagnostics(): string {
      return JSON.stringify({build:Service.diagnosticBuild,instance:Service.instanceId,
        open:root.opened,focus:keyCatcher.activeFocus,queueOpen:queuePane.expanded,
        cursor:queuePane.cursor,blockedCommands:Service.blockedCommands,history:Service.history})
    }
    function arm(): string {
      root.open()
      queuePane.expanded=true
      Qt.callLater(function(){keyCatcher.forceActiveFocus();Service.note("armed",{open:root.opened,focus:keyCatcher.activeFocus,queueOpen:queuePane.expanded})})
      return Service.instanceId
    }
    function queue(op: string, value: string): string {
      Service.note("fixture-op",{action:op,mode:value,open:root.opened,queueOpen:queuePane.expanded})
      if (op === "open") queuePane.expanded=true
      else if (op === "close") queuePane.expanded=false
      else if (op === "mode") Service.queueMode=value
      else if (op === "hold") Service.queueHold=value === "true"
      else if (op === "flush") return String(Service.flushQueue())
      else if (op === "down") queuePane.move(1)
      else if (op === "enter") queuePane.activate()
      else if (op === "back") queuePane.back()
      else return "unknown-queue-op"
      return "ok"
    }
'''+marker,1)
needle='        header: Model.headerStatus(root.st), tooltip: Model.tooltipText(root.st),'
assert needle in panel
panel=panel.replace(needle,needle+'''
        queueOpen:queuePane.expanded,queueRows:queuePane.rows.length,queueStatus:queuePane.status,
        queueBusy:queuePane.busy,queueReads:Service.queueReads,queuePending:Service.queuePending.length,
        queueCursor:queuePane.cursor,queueHeight:queuePane.height,
        columnFits:contentColumn.implicitHeight <= panel.contentHeight-panel.verticalContentInset,
        innerHeight:panel.contentHeight-panel.verticalContentInset,''',1)
p.write_text(panel)
print('Prepared full-popup queue fixture; not installed/enabled; all requests stay in memory.')
