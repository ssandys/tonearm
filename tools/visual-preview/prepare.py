"""Prepare a no-I/O preview copy; does not enable it or touch live Tonearm."""
import argparse
import json
from pathlib import Path
import shutil

ID = "io.github.cbayschm74.tonearm-preview"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("output", type=Path)
args = parser.parse_args()
source = Path(__file__).resolve().parents[2]
assets = Path(__file__).resolve().parent
if args.output.exists():
    parser.error("output exists; refusing to overwrite")
service = (assets / "Service.qml").read_text()
for forbidden in ("Process", "Socket", "WebSocket", "XMLHttpRequest", "tonearmctl", "file://", "http://"):
    if forbidden in service:
        parser.error("unexpected I/O surface in fixture service")
args.output.mkdir(parents=True)
for name in ("Model.js", "BrowsePane.qml", "QueueModel.js", "QueuePane.qml", "qmldir"):
    shutil.copy2(source / name, args.output / name)
panel = (source / "Panel.qml").read_text()
panel = panel.replace('import Quickshell\n', 'import Quickshell\nimport Quickshell.Io\n', 1)
panel = panel.replace('moduleName: "ssandys.tonearm"', f'moduleName: "{ID}"')
panel = panel.replace('ipcTarget: "ssandys.tonearm"', f'ipcTarget: "{ID}"')
assert panel.rstrip().endswith("}")
panel = panel.rstrip()[:-1] + (assets / "Panel.instrumentation.qmlpart").read_text() + "}\n"
(args.output / "Panel.qml").write_text(panel)
(args.output / "Service.qml").write_text(service)
states = json.loads((assets / "states.json").read_text())
(args.output / "Fixtures.js").write_text("var states = " + json.dumps(states) + "\n")
manifest = json.loads((source / "manifest.json").read_text())
manifest.update(id=ID, name="Tonearm synthetic preview", description="Temporary visual fixture; all I/O blocked")
manifest["barWidget"]["displayName"] = "Tonearm synthetic preview"
(args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print("Prepared " + str(args.output))
