import unittest,tempfile,copy
from pathlib import Path
from competitor_bios import competitor_bio_flags
from test_workflow import CONFIG,FakeProvider,profile
from workflow import Store,Workflow,assess,contact_skip_reason,current_draft,js,unpack

class BioRuleTests(unittest.TestCase):
    def test_provider_bios_flag_with_source_evidence(self):
        bios=["Welcome to Aviva UK 💛 Insurance, Savings, Investments and Retirement: protecting what matters most. Our Social Team’s here Mon–Fri.","All the services you need to run your home. Powered by local people.","Specialist insurance broker in taxi, motorhome and van.","We manage your household bills so you don't have to.","Your trusted energy supplier.","An insurance agency specialising in family cover.","We offer affordable home insurance.","Household admin management service for busy families.","Energy and broadband on one bill."]
        for bio in bios:
            with self.subTest(bio=bio):
                flags=competitor_bio_flags(bio,'https://instagram.com/provider/')
                self.assertTrue(flags)
                self.assertTrue(all(f['supporting_text'] in bio and f['matched_text'] in bio and f['source_url'].endswith('/provider/') for f in flags))
    def test_independent_creator_bios_not_flagged(self):
        bios=['UK mum sharing insurance tips and money saving advice.','I review insurance providers and compare energy bills.','I used an insurance broker to save on my cover.','Previously worked for an energy supplier. Now a budgeting creator.','I manage my household bills and share tips.','Helping families save money on household bills.','Independent reviews of Aviva UK and Utility Warehouse.','I am not an insurance broker. Just sharing my experience.','Former insurance broker, now a lifestyle blogger.','My experience with a household admin management service.','Insurance, savings, investments and retirement tips for UK families.','']
        for bio in bios:
            with self.subTest(bio=bio):self.assertFalse(competitor_bio_flags(bio))
    def test_commentary_does_not_mask_separate_provider_claim(self):
        self.assertTrue(competitor_bio_flags('Sharing money saving tips. We are an insurance broker.'))

class BioWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'test.db')
        self.config={**copy.deepcopy(CONFIG),'requireLatest20Posts':False,'queries':['one query']}
    def tearDown(self):
        FakeProvider.hook=None;self.store.close();self.temp.cleanup()
    def test_block_qualification_contacts_and_existing_drafts(self):
        c=self.store.creator('provider');self.store.update('creators','creator_key',c['creator_key'],{'followers':20000,'biography':'We manage your household bills.','manual_decision':'approved','review_json':js({'uk_suitability':True,'uk_evidence':'UK households'}),'contact_lookup_status':'no_contact_found'})
        c=self.store.one('creators','creator_key',c['creator_key'])
        self.assertEqual(assess(self.store,c,self.config),'flagged_competitor')
        self.assertEqual(contact_skip_reason(c,self.config),'flagged_competitor')
        d={'draft_key':'manual','creator_key':c['creator_key'],'channel':'instagram_message','draft_status':'approved','draft_body':'Saved edit','manually_edited':1}
        self.store.put('outreach_drafts',d,'draft_key')
        Workflow(self.store,self.config).run('replay')
        self.assertFalse(current_draft(self.store,d,self.config))
        self.assertEqual(self.store.one('outreach_drafts','draft_key','manual')['draft_body'],'Saved edit')
        self.assertTrue(unpack(self.store.one('creators','creator_key',c['creator_key'])['screening_results'])['competitor_bio_flags'])
    def test_new_provider_skips_pagination_contacts_and_drafts(self):
        self.config['requireLatest20Posts']=True;FakeProvider.searches=[]
        def hook(provider,p):
            if p['engine']=='google':return {'organic_results':[{'link':'https://instagram.com/provider/'}]}
            self.assertNotIn('next_page_token',p)
            result=profile(name='provider',bio='We are an insurance broker. Enquiries hello@provider.example',total=2)
            result['serpapi_pagination']={'next_page_token':'MORE'};return result
        FakeProvider.hook=hook
        result=Workflow(self.store,self.config,provider_factory=FakeProvider,page_get=lambda u:None).run()
        self.assertEqual(result['contacts_processed'],0);self.assertEqual(result['requests_attempted'],2)
        self.assertEqual(self.store.all('outreach_drafts'),[]);self.assertEqual(self.store.all('contacts'),[])
        self.assertIn('flagged_competitor',result['skipped_creators'])
    def test_refreshed_nonprovider_bio_clears_flag(self):
        c=self.store.creator('creator');self.store.update('creators','creator_key',c['creator_key'],{'followers':20000,'biography':'Insurance broker','review_json':js({'uk_suitability':True,'uk_evidence':'UK advice'})})
        self.assertEqual(assess(self.store,self.store.one('creators','creator_key',c['creator_key']),self.config),'flagged_competitor')
        self.store.update('creators','creator_key',c['creator_key'],{'biography':'UK budgeting creator sharing insurance tips.'})
        self.assertEqual(assess(self.store,self.store.one('creators','creator_key',c['creator_key']),self.config),'qualified')
