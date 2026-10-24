import unittest,tempfile,copy
from pathlib import Path
from test_workflow import CONFIG,FakeProvider,profile
from workflow import Store,Workflow,assess,js,validate_config,contact_skip_reason,current_draft

class ExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'test.db')
        self.config={**copy.deepcopy(CONFIG),'requireLatest20Posts':False,'excludedUsernames':['avivauk','welcometouw'],'queries':['one query']}
        FakeProvider.searches=[];FakeProvider.hook=None
    def tearDown(self):
        FakeProvider.hook=None;self.store.close();self.temp.cleanup()
    def creator(self,name):
        c=self.store.creator(name);self.store.update('creators','creator_key',c['creator_key'],{'followers':20000,'biography':'UK insurance advice','review_json':js({'uk_suitability':True,'uk_evidence':'UK household content'})});return self.store.one('creators','creator_key',c['creator_key'])
    def test_normalise_handle_url_and_duplicates(self):
        c=validate_config({**self.config,'excludedUsernames':['@AvivaUK','https://www.instagram.com/welcometouw/','avivauk']})
        self.assertEqual(c['excludedUsernames'],['avivauk','welcometouw'])
        for value in ['Aviva UK','https://example.com/avivauk/','https://instagram.com/p/code/',42]:
            with self.assertRaises(ValueError):validate_config({**self.config,'excludedUsernames':[value]})
    def test_exclusion_overrides_approval_without_erasing_it(self):
        c=self.creator('avivauk');self.store.update('creators','creator_key',c['creator_key'],{'manual_decision':'approved','reviewer_notes':'Earlier approval'})
        c=self.store.one('creators','creator_key',c['creator_key'])
        self.assertEqual(assess(self.store,c,self.config),'excluded_competitor')
        self.assertEqual(contact_skip_reason(c,self.config),'excluded_competitor')
        self.assertEqual(self.store.one('creators','creator_key',c['creator_key'])['manual_decision'],'approved')
    def test_mentions_and_similar_handles_not_excluded(self):
        c=self.creator('avivauk_fan');self.store.update('creators','creator_key',c['creator_key'],{'biography':'UK insurance advice reviewing Aviva UK and Utility Warehouse.'})
        self.assertEqual(assess(self.store,self.store.one('creators','creator_key',c['creator_key']),self.config),'qualified')
    def test_removal_reassesses_normally(self):
        c=self.creator('welcometouw');self.assertEqual(assess(self.store,c,self.config),'excluded_competitor')
        self.config['excludedUsernames']=[]
        self.assertEqual(assess(self.store,self.store.one('creators','creator_key',c['creator_key']),self.config),'qualified')
    def test_discovery_skips_profile_and_email_requests(self):
        def hook(provider,p):
            if p['engine']=='google':return {'organic_results':[{'link':'https://instagram.com/avivauk/'},{'link':'https://instagram.com/independent/'}]}
            self.assertEqual(p['profile_id'],'independent');return profile(name='independent')
        FakeProvider.hook=hook
        result=Workflow(self.store,self.config,provider_factory=FakeProvider,page_get=lambda u:None).run()
        self.assertEqual(result['creators_processed'],1)
        self.assertEqual(result['contacts_processed'],1)
        self.assertEqual(self.store.one('creators','username','avivauk')['qualification_status'],'excluded_competitor')
        self.assertEqual(len(self.store.all('outreach_drafts')),1)
        self.assertIn('excluded_competitor',result['skipped_creators'])
    def test_saved_draft_preserved_but_not_current(self):
        c=self.creator('avivauk');self.store.update('creators','creator_key',c['creator_key'],{'manual_decision':'approved','contact_lookup_status':'no_contact_found'})
        self.store.put('outreach_drafts',{'draft_key':'protected','creator_key':c['creator_key'],'channel':'instagram_message','draft_body':'My edited message','draft_status':'approved','manually_edited':1},'draft_key')
        result=Workflow(self.store,self.config).run('replay')
        d=self.store.one('outreach_drafts','draft_key','protected')
        self.assertEqual(d['draft_body'],'My edited message');self.assertFalse(current_draft(self.store,d,self.config));self.assertEqual(result['requests_attempted'],0)
