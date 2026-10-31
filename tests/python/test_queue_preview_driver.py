"""Diagnostic parser/admission only: no shell or Core access."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

path=Path(__file__).parents[2]/'tools/queue-preview/driver.py'
spec=importlib.util.spec_from_file_location('queue_preview_driver',path)
driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)


class QueuePreviewDriverTests(unittest.TestCase):
    def result(self,value,code=0,err=''):
        return SimpleNamespace(returncode=code,stdout=value,stderr=err)
    def test_closed_popup_is_not_misreported_as_transition_failure(self):
        with self.assertRaisesRegex(driver.PreviewError,'not admitted'):
            driver.require_open({'opened':False,'queueOpen':True},True)
        value={'opened':True,'queueOpen':True}
        self.assertIs(driver.require_open(value,True),value)
    def test_original_ipc_error_is_preserved_and_bounded(self):
        with self.assertRaises(driver.PreviewError) as caught:
            driver.decode(self.result('Unknown function'+'x'*10000,1,'bounded reason'),'diagnostics')
        error=json.loads(str(caught.exception))
        self.assertEqual(error['method'],'diagnostics');self.assertEqual(error['exit_code'],1)
        self.assertEqual(len(error['stdout']),2048);self.assertEqual(error['stderr'],'bounded reason')
    def test_wrong_generation_non_json_and_oversize_fail_closed(self):
        for value in ('not registered',json.dumps({'build':'older'}),'x'*65537):
            with self.subTest(size=len(value)), self.assertRaises(driver.PreviewError):
                driver.decode(self.result(value),'diagnostics')
        value={'build':'queue-diag-v1','instance':'fixture-instance','history':{'events':[]}}
        self.assertEqual(driver.decode(self.result(json.dumps(value)),'diagnostics'),value)
