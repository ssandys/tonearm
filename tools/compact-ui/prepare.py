"""Stage isolated component checks; no shell, daemon, scripts or credentials."""
import argparse
from pathlib import Path
import shutil

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('output', type=Path)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
source = Path(__file__).resolve().parents[2]
for name in ('BrowsePane.qml', 'Model.js', 'QueuePane.qml', 'QueueModel.js'):
    shutil.copyfile(source / name, args.output / name)
shutil.copyfile(source / 'tests/qml/compact-ui/tst_compact.qml', args.output / 'tst_compact.qml')
# Presentation stand-ins only. Production components are copied unchanged;
# the actual installed Quattro tokens must still be verified on adoption.
stubs = {
    'qs/Commons/qmldir': 'module qs.Commons\nsingleton Style 1.0 Style.qml\nsingleton Color 1.0 Color.qml\nsingleton Util 1.0 Util.qml\n',
    'qs/Commons/Style.qml': '''pragma Singleton
import QtQuick
QtObject {
    property real scale: 1
    function space(px) { return px * scale }
    property QtObject font: QtObject { property int caption: 13; property int body: 15 }
}
''',
    'qs/Commons/Color.qml': '''pragma Singleton
import QtQuick
QtObject { property color foreground: "#eeeeee"; property color muted: "#999999" }
''',
    'qs/Commons/Util.qml': '''pragma Singleton
import QtQuick
QtObject { function alpha(color, opacity) { return Qt.rgba(color.r,color.g,color.b,opacity) } }
''',
    'qs/Ui/qmldir': 'module qs.Ui\nTextField 1.0 TextField.qml\n',
    'qs/Ui/TextField.qml': '''import QtQuick.Controls
TextField { font.pixelSize: 15 }
''',
}
for name, data in stubs.items():
    path = args.output / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
print(args.output)
