"""Exercise the executable CLI, not just argument parsing; no sockets/SDK."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch
from tests.python import fakes
from tonearm_lib import queue_wire


class QueueCliTests(unittest.TestCase):
    def call(self, reply):
        module=runpy.run_path(str(Path(__file__).parents[2]/'scripts/tonearmctl'))
        req={'cmd':'queue_status','v':1,'request_id':'fixture-q','zone_id':'fixture-a'}
        class Connection:
            def __init__(self):self.sent=[];self.reads=0;self.closed=False
            def settimeout(self,value):pass
            def connect(self,path):pass
            def sendall(self,value):self.sent.append(json.loads(value))
            def makefile(self,mode):
                self.reads+=1
                return io.BytesIO(reply)
            def close(self):self.closed=True
        conn=Connection();stdout=io.StringIO();stderr=io.StringIO()
        with patch.object(module['socket'],'socket',return_value=conn), contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            code=module['main'](['queue',json.dumps(req)])
        self.assertEqual(conn.sent,[req]);self.assertTrue(conn.closed)
        return code,stdout.getvalue(),stderr.getvalue(),conn.reads
    def test_queue_prints_one_correlated_reply(self):
        req={'request_id':'fixture-q','zone_id':'fixture-a'}
        reply=queue_wire.result(req,'ready')
        code,out,err,reads=self.call((json.dumps(reply)+'\n').encode())
        self.assertEqual(code,0);self.assertEqual(json.loads(out),reply)
        self.assertEqual(err,'');self.assertEqual(reads,1)
    def test_queue_rejects_eof_oversize_malformed_and_wrong_identity(self):
        req={'request_id':'wrong','zone_id':'fixture-a'}
        for blob in (b'',b'x'*65537,b'not-json\n',(json.dumps(queue_wire.result(req,'ready'))+'\n').encode()):
            with self.subTest(blob_size=len(blob)):
                code,out,err,reads=self.call(blob)
                self.assertEqual(code,4);self.assertEqual(out,'');self.assertTrue(err)
                self.assertEqual(reads,1)
    def test_valid_remote_error_is_printed_and_nonzero(self):
        req={'request_id':'fixture-q','zone_id':'fixture-a'}
        reply=queue_wire.result(req,'error','unsupported')
        code,out,_,_=self.call((json.dumps(reply)+'\n').encode())
        self.assertEqual(code,1);self.assertEqual(json.loads(out),reply)
