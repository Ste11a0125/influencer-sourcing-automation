import unittest,tempfile,copy
from pathlib import Path
from test_workflow import CONFIG,FakeProvider,profile
from workflow import Store,Workflow,assess,js,unpack,validate_config

class RelaxedPostTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'test.db')
        self.config={**copy.deepcopy(CONFIG),'requireLatest20Posts':False,'queries':['one query']}
        self.c=self.store.creator('creator')
        self.store.update('creators','creator_key',self.c['creator_key'],{'followers':25000,'biography':'UK household budgeting tips.','review_json':js({'uk_suitability':True,'uk_evidence':'Saved UK household examples reviewed.'})})
    def tearDown(self):
        FakeProvider.hook=None;self.store.close();self.temp.cleanup()
    def check(self,**changes):
        if changes:self.store.update('creators','creator_key',self.c['creator_key'],changes)
        return assess(self.store,self.store.one('creators','creator_key',self.c['creator_key']),self.config)
    def test_biography_can_qualify_without_any_post_sample(self):
        self.assertEqual(self.check(),'qualified')
        saved=self.store.one('creators','creator_key',self.c['creator_key'])
        self.assertEqual(unpack(saved['missing_information'],[]),[])
        self.assertFalse(unpack(saved['screening_results'])['latest_20_required'])
    def test_uk_evidence_still_required(self):
        self.assertEqual(self.check(review_json='{}'),'needs_review')
        self.assertEqual(unpack(self.store.one('creators','creator_key',self.c['creator_key'])['missing_information'],[]),['UK household suitability needs supporting evidence'])
    def test_range_relevance_and_rejection_still_apply(self):
        self.assertEqual(self.check(followers=999),'not_qualified')
        self.assertEqual(self.check(followers=25000,biography='Beautiful sunsets.'),'needs_review')
        self.assertEqual(self.check(biography='UK budgeting',manual_decision='rejected'),'rejected')
    def test_reenable_restores_strict_criteria_and_preserves_review(self):
        review={'uk_suitability':True,'uk_evidence':'UK content','latest_post_ids':['old'],'chronological_evidence':'Earlier review'}
        self.assertEqual(self.check(review_json=js(review)),'qualified')
        self.config['requireLatest20Posts']=True
        self.assertEqual(self.check(),'needs_review')
        self.assertEqual(unpack(self.store.one('creators','creator_key',self.c['creator_key'])['review_json']),review)
    def test_no_extra_profile_page_when_skipped(self):
        FakeProvider.searches=[]
        def hook(provider,p):
            if p['engine']=='google':return {'organic_results':[{'link':'https://instagram.com/samplecreator/'}]}
            self.assertNotIn('next_page_token',p)
            result=profile(total=2);result['serpapi_pagination']={'next_page_token':'MORE'};return result
        FakeProvider.hook=hook
        result=Workflow(self.store,self.config,provider_factory=FakeProvider,page_get=lambda u:None).run('discovery')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(len([p for p in FakeProvider.searches if p['engine']=='instagram_profile']),1)
        self.assertEqual(len(self.store.all('post_evidence')),2)
    def test_switch_is_boolean_and_legacy_config_valid(self):
        validate_config(copy.deepcopy(CONFIG));validate_config(self.config)
        for value in ['false',0,None]:
            with self.assertRaises(ValueError):validate_config({**self.config,'requireLatest20Posts':value})
