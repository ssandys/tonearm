"""Render/test the unchanged QueuePane in Qt offscreen; no shell/backend/config."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

root=Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix='tonearm-queue-qml-') as directory:
    target=Path(directory);(target/'view').mkdir()
    # Avoid the production qmldir: it eagerly imports the real Service singleton.
    for name in ('QueuePane.qml','QueueModel.js'):
        shutil.copyfile(root/name,target/'view'/name)
    # Exercise actual Panel handlers with the installed pure QtQuick dispatcher.
    # No Quickshell engine or shell process is started.
    dispatcher=Path('/usr/share/omarchy/shell/Ui/PanelKeyCatcher.qml')
    shutil.copyfile(dispatcher,target/'view'/'PanelKeyCatcher.qml')
    panel=(root/'Panel.qml').read_text()
    handlers=[]
    for marker in ('onMoveRequested:', 'onActivateRequested:', 'onCloseRequested:', 'onExpandedChanged:'):
        line=next(line.strip() for line in panel.splitlines() if marker in line)
        handlers.append(line)
    (target/'view'/'Integration.qml').write_text('''import QtQuick
Item {
 id: root
 width:390;height:300
 property bool closed:false
 property int browseActivations:0
 property alias queue:queuePane
 property alias keys:keyCatcher
 function close() { closed=true }
 Item {
  id:browsePane
  property bool editing:false
  function handleMove(dx,dy) {}
  function handleActivate() { root.browseActivations++ }
  function handleBack() { return false }
 }
 PanelKeyCatcher {
  id:keyCatcher
  anchors.fill:parent
  blocked:browsePane.editing
'''+"\n".join(handlers[:3])+'''
  QueuePane {
   id:queuePane
   width:parent.width
   panelOpen:true
'''+handlers[3]+'''
  }
 }
}
''')
    test=(root/'tests/qml/queue-view/tst_queue.qml').read_text()
    (target/'tst_queue.qml').write_text(test.replace('import "../../../" as Tonearm','import "./view" as Tonearm'))
    env=dict(os.environ,QT_QPA_PLATFORM='offscreen',QT_QPA_PLATFORMTHEME='generic',QT_QUICK_CONTROLS_STYLE='Basic',QT_QUICK_BACKEND='software')
    env.pop('DISPLAY',None)
    raise SystemExit(subprocess.run(['/usr/lib/qt6/bin/qmltestrunner','-input',str(target)],env=env).returncode)
