"""Real localhost server checks with synthetic data and no external requests."""
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workflow import Store, ROOT, make_message_draft

class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.db=Path(cls.temp.name)/'test.db'
        cls.config=Path(cls.temp.name)/'config.json'
        cls.config.write_text((ROOT/'config.json').read_text())
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));cls.port=sock.getsockname()[1]
        cls.origin=f'http://127.0.0.1:{cls.port}'
        env={k:v for k,v in os.environ.items() if k!='SERPAPI_KEY'}
        cls.process=subprocess.Popen([sys.executable,str(ROOT/'app.py'),'--db',str(cls.db),'--config',str(cls.config),'serve','--port',str(cls.port),'--no-open'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        cls.process.stdout.readline()
        cls.entry=cls.process.stdout.readline().strip().split('Open this local session: ')[1]
        cls.cookies=http.cookiejar.CookieJar()
        cls.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cls.cookies))
        cls.client.open(cls.entry,timeout=5).read()
    @classmethod
    def tearDownClass(cls):
        cls.process.terminate();cls.process.communicate(timeout=5);cls.temp.cleanup()
    def request(self,path,data=None,origin=None):
        req=urllib.request.Request(self.origin+path,data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type':'application/json','X-Nous-Request':'local-review','Origin':origin or self.origin})
        return self.client.open(req,timeout=5)
    def test_session_required(self):
        with self.assertRaises(urllib.error.HTTPError) as err:urllib.request.urlopen(self.origin+'/api/state')
        self.assertEqual(err.exception.code,403)
    def test_index_and_state(self):
        html=self.request('/').read().decode()
        self.assertIn('Run full workflow',html)
        self.assertIn('Process saved creators',html)
        data=json.load(self.request('/api/state'))
        self.assertIn('creators',data['tables'])
        self.assertFalse(data['running'])
    def test_key_stays_in_memory(self):
        self.request('/api/key',{'key':'FAKE_TEST_KEY'})
        data=json.load(self.request('/api/state'))
        self.assertTrue(data['key_present'])
        self.assertNotIn('FAKE_TEST_KEY',json.dumps(data))
        self.assertNotIn('FAKE_TEST_KEY',self.config.read_text())
        self.assertNotIn(b'FAKE_TEST_KEY',self.db.read_bytes())
        self.request('/api/key',{'key':''})
    def test_cross_origin_mutation_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as err:self.request('/api/key',{'key':'bad'},origin='https://example.com')
        self.assertEqual(err.exception.code,403)
    def test_exclusions_apply_on_save_and_can_be_removed(self):
        original=json.loads(self.config.read_text())
        store=Store(self.db)
        c=store.creator('brand_fixture')
        store.update('creators','creator_key',c['creator_key'],{'followers':20000,'manual_decision':'approved','reviewer_notes':'Previous decision'})
        store.close()
        try:
            self.request('/api/config',{**original,'excludedUsernames':['@Brand_Fixture']})
            data=json.load(self.request('/api/state'))
            c=next(c for c in data['tables']['creators'] if c['username']=='brand_fixture')
            self.assertEqual(c['qualification_status'],'excluded_competitor')
            self.assertFalse(c['contact_candidate'])
            self.request('/api/config',{**original,'excludedUsernames':[]})
            data=json.load(self.request('/api/state'))
            c=next(c for c in data['tables']['creators'] if c['username']=='brand_fixture')
            self.assertEqual(c['qualification_status'],'manually_approved')
            self.assertTrue(c['contact_candidate'])
        finally:
            self.request('/api/config',original)
    def test_invalid_config_rejected(self):
        config=json.loads(self.config.read_text());config['maxSerpApiRequestsPerRun']=100
        with self.assertRaises(urllib.error.HTTPError) as err:self.request('/api/config',config)
        self.assertEqual(err.exception.code,400)
    def test_message_export_and_email_supersession(self):
        store=Store(self.db)
        try:
            c=store.creator('message_fixture')
            store.update('creators','creator_key',c['creator_key'],{'qualification_status':'manually_approved','manual_decision':'approved','contact_lookup_status':'no_contact_found','biography':'UK family budgeting.'})
            c=store.one('creators','creator_key',c['creator_key'])
            draft=make_message_draft(c,{'text':c['biography'],'url':c['profile_url'],'kind':'biography'},json.loads(self.config.read_text()))
            draft.update(draft_key='message_fixture_draft',created_at='2026-10-19')
            store.put('outreach_drafts',draft,'draft_key')
            csv=self.request('/api/export').read().decode('utf-8-sig')
            self.assertIn('instagram_message',csv)
            self.assertIn('which business email',csv)
            state=json.load(self.request('/api/state'))
            self.assertTrue(next(d for d in state['tables']['outreach_drafts'] if d['draft_key']=='message_fixture_draft')['is_current'])
            store.put('contacts',{'contact_key':'fixture_email','creator_key':c['creator_key'],'contact_type':'email','contact_value':'hello@fixture.example','attribution_status':'creator','review_status':'human_confirmed'},'contact_key')
            self.assertNotIn('instagram_message',self.request('/api/export').read().decode('utf-8-sig'))
        finally:
            store.db.execute("DELETE FROM contacts WHERE contact_key='fixture_email'")
            store.db.execute("DELETE FROM outreach_drafts WHERE draft_key='message_fixture_draft'")
            store.db.execute("DELETE FROM creators WHERE username='message_fixture'")
            store.db.commit();store.close()
    def test_empty_csv_export(self):
        response=self.request('/api/export')
        self.assertIn('text/csv',response.headers['Content-Type'])
        self.assertIn('username,qualification,qualification_pending,followers,channel,instagram_url,email',response.read().decode('utf-8-sig'))

if __name__=='__main__':unittest.main()
