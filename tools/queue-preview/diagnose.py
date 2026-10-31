"""Test generated diagnostic fixture offscreen, without enabling a shell plugin."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
root=Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix='tonearm-queue-diagnostics-') as tmp:
    target=Path(tmp);fixture=target/'fixture';view=target/'view';view.mkdir()
    subprocess.run([sys.executable,str(root/'tools/queue-preview/prepare.py'),str(fixture)],check=True)
    for name in ('QueuePane.qml','QueueModel.js'):
        shutil.copyfile(root/name,view/name)
    shutil.copyfile('/usr/share/omarchy/shell/Ui/PanelKeyCatcher.qml',view/'PanelKeyCatcher.qml')
    panel=(fixture/'Panel.qml').read_text()
    handlers=[]
    for name in ('onMoveRequested:','onActivateRequested:','onCloseRequested:','onExpandedChanged:','onOpenedChanged:','onActiveFocusChanged:'):
        handlers.append(next(line.strip() for line in panel.splitlines() if name in line))
    (view/'Integration.qml').write_text('''import QtQuick
import "../fixture"
Item {
 id:root
 width:390;height:300
 property bool opened:true
 property int browseActivations:0
 property alias queue:queuePane
 property alias keys:keyCatcher
 function close() { opened=false }
'''+handlers[4]+'''
 Item {
  id:browsePane
  property bool editing:false
  function handleMove(dx,dy) {}
  function handleActivate() { root.browseActivations++ }
  function handleBack() { return false }
  function resetPane() {}
 }
 PanelKeyCatcher {
  id:keyCatcher
  anchors.fill:parent
'''+ '\n'.join(handlers[:3]+[handlers[5]])+'''
  QueuePane {
   id:queuePane
   width:parent.width
   panelOpen:root.opened
'''+handlers[3]+'''
  }
 }
}
''')
    shutil.copyfile(root/'tests/qml/queue-diagnostics/tst_diagnostics.qml',target/'tst_diagnostics.qml')
    env=dict(os.environ,QT_QPA_PLATFORM='offscreen',QT_QPA_PLATFORMTHEME='generic',QT_QUICK_CONTROLS_STYLE='Basic',QT_QUICK_BACKEND='software')
    env.pop('DISPLAY',None)
    raise SystemExit(subprocess.run(['/usr/lib/qt6/bin/qmltestrunner','-input',str(target)],env=env).returncode)
