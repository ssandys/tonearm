"""Read-only library/page navigation; no Core or playback."""
import unittest
from tests.python.fakes import FakeRoon,yavin_levels
from tonearm_lib import browse,cli


def fixture():
    levels=yavin_levels()
    levels['library']['items']=[{'title':'Folder %03d'%i,'item_key':'folder-%d'%i,'hint':'list','_goes_to':'child'} for i in range(205)]
    levels['library']['items'][1]={'title':'Play action','item_key':'action','hint':'action','_goes_to':'child'}
    levels['library']['items'][2]={'title':'Input action','item_key':'input','hint':'list','input_prompt':{'prompt':'Input'},'_goes_to':'child'}
    levels['library']['list']['count']=205
    levels['child']={'list':{'title':'Child','level':2,'count':0},'items':[]}
    api=FakeRoon(levels)
    return api,browse.BrowseSession(api,'navigation-test')


class LibraryNavigationTest(unittest.TestCase):
    def test_open_library_does_not_search_or_play(self):
        api,s=fixture();out=s.library()
        self.assertEqual(out['path'],['Library']);self.assertEqual(len(out['rows']),100)
        self.assertEqual(out['count'],205);self.assertEqual(out['offset'],0)
        self.assertTrue(out['rows'][0]['can_navigate'])
        self.assertFalse(out['rows'][1]['can_navigate']);self.assertFalse(out['rows'][2]['can_navigate'])
        self.assertEqual(len(api.calls),2);self.assertFalse(any('input' in x for x in api.calls))

    def test_guarded_page_replaces_window_and_bumps_addressing_level(self):
        api,s=fixture();first=s.library();second=s.page_navigation(100,first['level_id'])
        self.assertEqual(second['offset'],100);self.assertEqual(second['rows'][0]['title'],'Folder 100')
        self.assertGreater(second['level_id'],first['level_id'])
        with self.assertRaises(browse.BrowseError):s.enter(0,first['level_id'])
        last=s.page_navigation(200,second['level_id']);self.assertEqual(len(last['rows']),5)
        self.assertEqual(s.page_navigation(100,last['level_id'])['rows'][0]['title'],'Folder 100')

    def test_stale_and_bad_pages_do_not_reach_core(self):
        for offset,level in [(100,999),(-1,1),(True,1),(1.5,1),(100,True),(100,None)]:
            api,s=fixture();s.library();before=len(api.load_calls)
            with self.assertRaises(browse.BrowseError):s.page_navigation(offset,level)
            self.assertEqual(len(api.load_calls),before)

    def test_navigation_refuses_actions_and_input_prompts_before_core(self):
        for index in (1,2):
            api,s=fixture();out=s.library();before=len(api.calls)
            with self.assertRaises(browse.BrowseError):s.enter(index,out['level_id'],navigation_only=True)
            self.assertEqual(len(api.calls),before)

    def test_navigation_descends_and_back_retains_library_root(self):
        api,s=fixture();out=s.library();child=s.enter(0,out['level_id'],navigation_only=True)
        self.assertEqual(child['path'],['Library','Folder 000']);self.assertEqual(child['rows'],[])
        self.assertEqual(s.back()['path'],['Library'])

    def test_guarded_back_restores_parent_page_and_current_row_keys(self):
        api,s=fixture();first=s.library();page=s.page_navigation(100,first['level_id'])
        child=s.enter(42,page['level_id'],navigation_only=True)
        parent=s.back_navigation(100,child['level_id'])
        self.assertEqual(parent['offset'],100);self.assertEqual(parent['rows'][0]['title'],'Folder 100')
        self.assertEqual(s.enter(42,parent['level_id'],navigation_only=True)['path'][-1],'Folder 142')

    def test_guarded_back_refuses_before_popping_parent(self):
        for offset,delta in [(-1,0),(True,0),(1.5,0),(0,99)]:
            api,s=fixture();out=s.library();child=s.enter(0,out['level_id'],navigation_only=True)
            before=(len(api.calls),len(api.load_calls))
            with self.assertRaises(browse.BrowseError):s.back_navigation(offset,child['level_id']+delta)
            self.assertEqual((len(api.calls),len(api.load_calls)),before)

    def test_cli_new_operations_and_legacy_page_are_independent(self):
        self.assertEqual(cli.to_request(['browse','library'])['op'],'library')
        self.assertEqual(cli.to_request(['browse','page_nav','100','8']),{'cmd':'browse','session':'cli','op':'page_nav','offset':100,'level_id':8})
        self.assertEqual(cli.to_request(['browse','enter_nav','0','8'])['op'],'enter_nav')
        self.assertEqual(cli.to_request(['browse','page','100'])['offset'],100)
        with self.assertRaises(ValueError):cli.to_request(['browse','page_nav','100'])


class NavigationWireTest(unittest.TestCase):
    def test_existing_core_wrapper_cli_navigate_without_selected_zone(self):
        import json,os,subprocess,sys,tempfile,threading
        from pathlib import Path
        from unittest.mock import patch
        from tonearm_lib import core,server,art
        api,_=fixture();api.zones={};api._zone_list_generation=1
        with patch.object(core.config,'load',return_value={'strict_pins':True,'pinned_zone_id':'missing'}):
            session=core.RoonSession(lambda _:None)
        session._api=api;session._status='ok'
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'XDG_RUNTIME_DIR':tmp}):
            srv=server.Server(art.CachingSession(session,art.Cache(tmp)))
            worker=threading.Thread(target=srv.serve_forever);worker.start()
            try:
                self.assertTrue(srv.ready.wait(3))
                def call(*args):
                    proc=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[2]/'scripts/tonearmctl'),'browse','--session','nav-wire',*args],capture_output=True,text=True,timeout=5)
                    self.assertEqual(proc.returncode,0,proc.stderr);return json.loads(proc.stdout)
                first=call('library');self.assertEqual(first['path'],['Library'])
                second=call('page_nav','100',str(first['level_id']))
                self.assertEqual(second['rows'][0]['title'],'Folder 100')
                child=call('enter_nav','0',str(second['level_id']))
                self.assertEqual(child['path'],['Library','Folder 100'])
                self.assertFalse(any('zone_or_output_id' in c for c in api.calls+api.load_calls))
            finally:
                srv.shutdown();worker.join(3);self.assertFalse(worker.is_alive())
