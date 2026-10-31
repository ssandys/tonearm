#!/usr/bin/python
"""Synthetic CLI for the real QML Service: never imports or connects to Roon."""
import json
from pathlib import Path
import sys
import time

if sys.argv[1:] == ['subscribe']:
    states=json.loads((Path(__file__).parent/'states.json').read_text())
    print(json.dumps(states['playing']),flush=True)
    while True:
        time.sleep(1)
elif len(sys.argv)==3 and sys.argv[1]=='player':
    req=json.loads(sys.argv[2])
    if req.get('verb') == 'next': time.sleep(.25)  # deterministic late-reply fixture
    root=Path.home()/'.local/state/omarchy-roon/player-preview-2026-10-29'
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (root/'calls.jsonl').open('a') as f:
        f.write(json.dumps(req)+'\n')
    reply={'v':1,'type':'command_result','request_id':req['request_id'],
           'zone_id':req['zone_id'],'verb':req['verb'],
           'outcome':'refused' if req['verb']=='next' else 'attempted',
           'error':'selection_changed' if req['verb']=='next' else None}
    print(json.dumps(reply),flush=True)
elif sys.argv[1:2] == ['browse']:
    print(json.dumps({'v':1,'ok':True,'rows':[],'path':[],'level_id':1}),flush=True)
else:
    raise SystemExit('Fixture refuses all other operations')
