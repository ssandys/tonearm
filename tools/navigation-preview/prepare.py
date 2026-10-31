"""Prepare a synthetic read-only Library UI, never the real Roon service."""
import argparse,json,shutil,subprocess,sys
from pathlib import Path
parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path);args=parser.parse_args()
source=Path(__file__).resolve().parents[2]
subprocess.run([sys.executable,str(source/'tools/visual-preview/prepare.py'),str(args.output)],check=True)
service=(source/'tools/navigation-preview/Service.qml').read_text()
for word in ('Process','Socket','XMLHttpRequest','tonearmctl','http://','file://'):
    assert word not in service
(args.output/'Service.qml').write_text(service)
states=json.loads((source/'tools/visual-preview/states.json').read_text())
states['navigation4']=json.loads(json.dumps(states['paused']))
states['navigation4']['zones'] += [{'id':'fixture-third','name':'Preview third room','state':'paused'},{'id':'fixture-fourth','name':'Preview fourth room','state':'stopped'}]
(args.output/'Fixtures.js').write_text('var states = '+json.dumps(states)+'\n')
p=args.output/'Panel.qml';s=p.read_text();needle='    function inspect(): string {\n';assert needle in s
s=s.replace(needle,'''    function nav(op: string, value: string): string {
      if (op === "library") browsePane.openLibrary()
      else if (op === "next") browsePane.changePage(1)
      else if (op === "previous") browsePane.changePage(-1)
      else if (op === "cursor") browsePane.cursor=Number(value)
      else if (op === "enter") browsePane.handleActivate()
      else if (op === "back") browsePane.handleBack()
      else if (op === "down") browsePane.handleMove(0,1)
      else if (op === "up") browsePane.handleMove(0,-1)
      else if (op === "reset") browsePane.resetPane()
      else if (op === "hold") Service.hold=value === "true"
      else if (op === "behavior") Service.behavior=value
      else if (op === "flush") return String(Service.flush())
      else if (op === "flush_one") return String(Service.flushFirst())
      else return "unknown-nav"
      return "ok"
    }
'''+needle)
needle='''        header: Model.headerStatus(root.st), tooltip: Model.tooltipText(root.st),'''
assert needle in s
s=s.replace(needle,needle+'''
        navRows:browsePane.rows.length, navOffset:browsePane.pageOffset, navCount:browsePane.totalCount,
        navLevel:browsePane.levelId, navPath:browsePane.path, navCursor:browsePane.cursor,
        navBusy:browsePane.busy, navError:browsePane.errorText, navParents:browsePane.navParents.length,
        navReadOnly:browsePane.readOnlyNavigation, navCalls:Service.calls, pending:Service.pending.length,
        inset:panel.verticalContentInset,''')
p.write_text(s)
print('Prepared in-memory navigation fixture with four synthetic zones; no backend/ctl copied.')
