"""Prepare isolated real-Service + fake-CLI preview, with no real daemon path."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('output',type=Path)
args=parser.parse_args()
source=Path(__file__).resolve().parents[2]
subprocess.run([sys.executable,str(source/'tools/visual-preview/prepare.py'),str(args.output)],check=True)
service=(source/'Service.qml').read_text()
old='Qt.resolvedUrl("scripts/tonearmctl")'
assert service.count(old)==1
service=service.replace(old,'Qt.resolvedUrl("fixture-ctl.py")')
service=service.replace('import QtQuick\n','import QtQuick\nimport "Fixtures.js" as Fixtures\n',1)
extra='''
  property string fixtureName: "playing"
  property int blockedCommands: 0
  property int blockedBrowse: 0
  function selectFixture(name) {
    if (!Object.prototype.hasOwnProperty.call(Fixtures.states,name)) return "unknown-fixture"
    state=Fixtures.states[name]; receivedAt=Date.now(); fixtureName=name
    playerError=""
    return "ok"
  }
'''
assert service.rstrip().endswith('}')
service=service.rstrip()[:-1]+extra+'}\n'
(args.output/'Service.qml').write_text(service)
shutil.copy2(source/'tools/player-preview/fixture-ctl.py',args.output/'fixture-ctl.py')
(args.output/'fixture-ctl.py').chmod(0o700)
shutil.copy2(source/'tools/visual-preview/states.json',args.output/'states.json')
p=args.output/'Panel.qml';panel=p.read_text()
needle='    function inspect(): string {\n'
assert needle in panel
panel=panel.replace(needle,'''    function sendPlayer(verb: string, arg: string): string {
      Service.send(verb,arg === "" ? undefined : Number(arg))
      return "fixture-only"
    }
'''+needle)
needle='''        header: Model.headerStatus(root.st), tooltip: Model.tooltipText(root.st),'''
assert needle in panel
panel=panel.replace(needle,needle+'''
        playerError: Service.playerError, playing: root.playing,
        canSeek: Model.canPlayer(root.st,"seek"), canVolume: Model.canPlayer(root.st,"volume"),
        canMute: Model.canPlayer(root.st,"mute"), muted: Model.playerMuted(root.st),''')
p.write_text(panel)
# Hard requirement: the only spawned executable is the fixture, never Roon ctl.
assert 'Qt.resolvedUrl("scripts/tonearmctl")' not in service
assert not (args.output/'scripts').exists()
print('Real QML service points only to the synthetic CLI; no backend or private data copied.')
