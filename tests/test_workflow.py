import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workflow import *

CONFIG = {
    'targetCountry':'United Kingdom','minFollowers':10000,'maxFollowers':100000,
    'maxProfilesPerRun':5,'targetPostsPerCreator':20,'minimumRelevantPosts':5,
    'maxSerpApiRequestsPerRun':15,'mode':'discovery','senderName':'','senderRole':'',
    'approvedProductBrief':'Nous helps UK households manage insurance admin.',
    'approvedProductFacts':[], 'queries':['fixture query '+str(i) for i in range(5)]
}


def profile(name='samplecreator',count=25000,bio=None,identity='123',total=20):
    return {'profile_results': {'id':identity,'username':name,'full_name':'Sample Creator','followers':count,
        'biography':bio or 'UK family budgeting and money saving. Business enquiries: hello@samplecreator.example',
        'bio_links':[], 'posts':[{'id':str(i),'shortcode':'Fixture'+str(i),'media_captions':[f'Family budgeting: plan household bills and save money. Practical example {i}.'],'owner':{'username':name}} for i in range(total)]}}


class FakeProvider:
    searches=[]
    hook=None
    def __init__(self,key,store,run_id,cap):
        self.store,self.run_id,self.cap=store,run_id,cap
    def check(self):
        pass
    def search(self,**params):
        row=self.store.one('run_log','run_id',self.run_id)
        if row['requests_attempted']>=self.cap:
            raise QuotaStop('Test quota reached.')
        self.store.update('run_log','run_id',self.run_id,{'requests_attempted':row['requests_attempted']+1})
        FakeProvider.searches.append(params)
        if FakeProvider.hook:
            return FakeProvider.hook(self,params)
        if params['engine']=='google':
            return {'organic_results':[{'link':'https://instagram.com/samplecreator/?utm_source=google','title':'Sample','snippet':'UK budgeting'},
                                       {'link':'https://instagram.com/p/shortcode/','title':'Post','snippet':'Unresolved'}]}
        return profile()


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'test.sqlite3')
        self.config=copy.deepcopy(CONFIG)
        FakeProvider.searches=[]
        FakeProvider.hook=None
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def runflow(self,mode='discovery',page_get=None):
        def default_page(u):
            if post_authors.shortcode(u)=='shortcode':
                return 200, {'content-type':'text/html'}, b'<script type="application/json">{"shortcode":"shortcode","owner":{"username":"samplecreator"}}</script>'
            raise AssertionError('Unexpected page fetch')
        return Workflow(self.store,self.config,provider_factory=FakeProvider,page_get=page_get or default_page).run(mode)
    def selected(self,bio=None):
        self.runflow()
        c=self.store.all('creators')[0]
        # Isolate setup for contact tests; discovery now researches pending candidates too.
        self.store.db.execute('DELETE FROM contacts');self.store.db.execute('DELETE FROM outreach_drafts');self.store.db.commit()
        review_creator(self.store,c['creator_key'],{'manual_decision':'approved','reviewer_notes':'Reviewed for this campaign.'},self.config)
        if bio is not None:
            self.store.update('creators','creator_key',c['creator_key'],{'biography':bio})
        return self.store.one('creators','creator_key',c['creator_key'])
    def test_unambiguous_urls(self):
        self.assertEqual(username_from_url('https://instagram.com/Money.Mum/?igsh=abc'),'money.mum')
        for u in ['https://instagram.com/p/abc','https://instagram.com/reel/abc','https://instagram.com/explore','https://instagram.com/accounts','https://instagram.com/abc/tagged/','https://evil.test/money']:
            self.assertEqual(username_from_url(u),'')
    def test_credentials_urls_never_saved(self):
        for u in ['https://site.test/?api_key=SECRET','https://u:p@site.test/','https://serpapi.com/searches/token/x','http://site.test/?access_token=x']:
            self.assertEqual(canonical(u),'')
    def test_save_before_enrichment(self):
        def hook(provider,params):
            if params['engine']=='google':
                return {'organic_results':[{'link':'https://instagram.com/samplecreator/','snippet':'budget'}]}
            # Read through an independent connection to prove commit precedes profile request.
            other=Store(self.store.path)
            self.assertEqual(len(other.all('search_results')),5)
            other.close()
            return profile()
        FakeProvider.hook=hook
        self.assertEqual(self.runflow()['status'],'complete')
    def test_duplicate_free_rerun(self):
        self.runflow();self.runflow()
        self.assertEqual(len(self.store.all('creators')),1)
        self.assertEqual(len(self.store.all('post_evidence')),20)
        self.assertEqual(len(self.store.all('search_results')),10)
        self.assertEqual(len([r for r in self.store.all('search_results') if unpack(r['author_evidence_json'],[])]),5)
    def test_keywords_never_auto_qualify(self):
        self.runflow()
        self.assertEqual(self.store.all('creators')[0]['qualification_status'],'needs_review')
        draft=self.store.all('outreach_drafts')[0]
        self.assertEqual(draft['draft_status'],'needs_review')
        self.assertIn('Provisional draft',draft['missing_information'])
    def test_review_qualifies_only_supported_criteria(self):
        self.runflow();c=self.store.all('creators')[0]
        ids=unpack(c['latest_sample_json'],[])
        r={'relevant_post_ids':ids[:5],'latest_post_ids':ids,'chronological_evidence':'Human checked the profile order against all twenty supplied links.','uk_suitability':True,'uk_evidence':'Captions explicitly discuss UK household bills.'}
        self.assertEqual(review_creator(self.store,c['creator_key'],{'review_json':r},self.config),'qualified')
        r['uk_evidence']=''
        self.assertEqual(review_creator(self.store,c['creator_key'],{'review_json':r},self.config),'needs_review')
    def test_followers_inclusive_and_missing(self):
        for value,expected in [(9999,'not_qualified'),(10000,'needs_review'),(100000,'needs_review'),(100001,'not_qualified'),(None,'needs_review'),('25K','needs_review')]:
            FakeProvider.hook=lambda provider,p,v=value: profile(count=v) if p['engine']=='instagram_profile' else {'organic_results':[{'link':'https://instagram.com/samplecreator/'}]}
            self.runflow()
            c=self.store.all('creators')[0]
            self.assertEqual(c['qualification_status'],expected)
            if value=='25K': self.assertIsNone(c['followers'])
    def test_manual_decision_preserved(self):
        c=self.selected()
        self.runflow()
        after=self.store.one('creators','creator_key',c['creator_key'])
        self.assertEqual(after['manual_decision'],'approved')
        self.assertEqual(after['reviewer_notes'],'Reviewed for this campaign.')
    def test_email_and_evidence_backed_draft(self):
        c=self.selected()
        self.runflow('contact_lookup')
        contacts=self.store.all('contacts');drafts=self.store.all('outreach_drafts')
        self.assertEqual(len(contacts),1);self.assertEqual(len(drafts),1)
        self.assertEqual(contacts[0]['attribution_status'],'creator')
        self.assertEqual(contacts[0]['review_status'],'discovered')
        d=drafts[0]
        self.assertEqual(d['draft_status'],'needs_review')
        self.assertEqual(d['channel'],'email')
        self.assertIn('[Sender name]',d['draft_body'])
        self.assertIn('UK family budgeting',d['draft_body'])
        self.assertEqual(unpack(d['product_facts_used_json'],[]),[CONFIG['approvedProductBrief']])
        self.assertTrue(100<=len(d['draft_body'].split())<=150,len(d['draft_body'].split()))
        self.assertIn('senderName',unpack(d['missing_information'],[]))
    def test_no_email_creates_instagram_message(self):
        self.selected('UK family budgeting and money saving.')
        self.runflow('contact_lookup')
        self.assertEqual(self.store.all('contacts'),[])
        draft=self.store.all('outreach_drafts')[0]
        self.assertEqual(draft['channel'],'instagram_message')
        self.assertEqual(draft['recipient_email'],'')
        self.assertTrue(50<=len(draft['draft_body'].split())<=80)
        self.assertIn('which business email',draft['draft_body'])
        self.assertEqual(self.store.all('creators')[0]['contact_lookup_status'],'no_contact_found')
    def test_protect_manual_draft(self):
        self.selected();self.runflow('contact_lookup')
        d=self.store.all('outreach_drafts')[0]
        edit_draft(self.store,d['draft_key'],{'draft_body':'My manually written email.','draft_status':'approved'})
        self.runflow('contact_lookup');self.runflow('draft_generation')
        self.assertEqual(len(self.store.all('outreach_drafts')),1)
        self.assertEqual(self.store.all('outreach_drafts')[0]['draft_body'],'My manually written email.')
    def test_management_attribution(self):
        self.selected('UK insurance explanations. Management and business enquiries: agent@agency.example')
        self.runflow('contact_lookup')
        c=self.store.all('contacts')[0];d=self.store.all('outreach_drafts')[0]
        self.assertEqual(c['attribution_status'],'representative')
        self.assertIn('Hello management team',d['draft_body'])
        self.assertIn('Sample Creator',d['draft_body'])
    def test_unrelated_emails_ignored(self):
        self.selected('UK family budgeting. Contact support@vendor.example for website help. Privacy privacy@sample.example. Business hello@sample.example')
        self.runflow('contact_lookup')
        self.assertEqual([c['contact_value'] for c in self.store.all('contacts')],['hello@sample.example'])
    def test_search_snippet_unconfirmed(self):
        self.selected('UK family budgeting.')
        FakeProvider.hook=lambda provider,p:{'organic_results':[{'link':'https://agency.example/creator','title':'Sample Creator business enquiries','snippet':'Business enquiries: agent@agency.example'}]}
        self.runflow('contact_lookup',lambda u:(403,{},b''))
        c=self.store.all('contacts')[0]
        self.assertEqual(c['attribution_status'],'unconfirmed')
        message=self.store.all('outreach_drafts')[0]
        self.assertEqual(message['channel'],'instagram_message')
        review_contact(self.store,c['contact_key'],{'attribution_status':'representative','review_status':'human_confirmed','recipient_name':'Alex'})
        self.runflow('draft_generation')
        email=self.store.all('outreach_drafts', 'channel=?', ('email',))[0]
        self.assertIn('Hello Alex',email['draft_body'])
        self.assertFalse(current_draft(self.store,message,self.config))
        self.assertTrue(current_draft(self.store,email,self.config))
    def test_public_page_attribution_and_four_request_cap(self):
        c=self.selected('UK household organisation and life admin.')
        self.store.update('creators','creator_key',c['creator_key'],{'bio_links_json':js(['https://creator.example/'])})
        fetched=[]
        def page(u):
            fetched.append(u)
            return 200,{'content-type':'text/html'},b'<h1>samplecreator</h1><p>Business enquiries hello@creator.example</p>'+b''.join(f'<a href="/contact{i}">Contact</a>'.encode() for i in range(9))
        self.runflow('contact_lookup',page)
        self.assertEqual(len(fetched),1)
        self.assertEqual(len(self.store.all('contacts')),1)
        self.assertEqual(self.store.all('contacts')[0]['attribution_status'],'creator')
    def test_four_page_cap_without_email(self):
        c=self.selected('UK household organisation and life admin.')
        self.store.update('creators','creator_key',c['creator_key'],{'bio_links_json':js(['https://creator.example/'])})
        fetched=[]
        def page(u):
            fetched.append(u)
            return 200,{'content-type':'text/html'},b''.join(f'<a href="/contact{i}">Contact</a>'.encode() for i in range(9))
        self.runflow('contact_lookup',page)
        self.assertEqual(len(fetched),4)
    def test_bio_email_skips_public_pages_and_contact_search(self):
        c=self.selected()
        self.store.update('creators','creator_key',c['creator_key'],{'bio_links_json':js(['https://creator.example/'])})
        FakeProvider.searches=[]
        r=self.runflow('contact_lookup')
        self.assertEqual(r['public_page_requests'],0)
        self.assertEqual(FakeProvider.searches,[])
    def test_replay_offline(self):
        self.selected();self.runflow('contact_lookup')
        class Fail:
            def __init__(self,*args): raise AssertionError('Replay tried to initialise network provider')
        result=Workflow(self.store,self.config,provider_factory=Fail,page_get=lambda u:(_ for _ in ()).throw(AssertionError())).run('replay')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['requests_attempted'],0)
        self.assertEqual(result['account_checks'],0)
        self.assertEqual(result['public_page_requests'],0)
    def test_one_extra_post_page_and_dedup(self):
        def hook(provider,p):
            if p['engine']=='google':return {'organic_results':[{'link':'https://instagram.com/samplecreator/'}]}
            d=profile(total=12 if 'next_page_token' not in p else 25)
            d['serpapi_pagination']={'next_page_token':'next'}
            return d
        FakeProvider.hook=hook;self.runflow()
        self.assertEqual(len([p for p in FakeProvider.searches if p['engine']=='instagram_profile']),2)
        self.assertEqual(len(self.store.all('post_evidence')),20)
    def test_identity_promotion_keeps_key_and_human_fields(self):
        c=self.store.creator('firsthandle')
        self.store.update('creators','creator_key',c['creator_key'],{'manual_decision':'approved','reviewer_notes':'keep'})
        promoted=self.store.creator('firsthandle','abc')
        self.assertEqual(promoted['creator_key'],c['creator_key'])
        renamed=self.store.creator('newhandle','abc')
        self.assertEqual(renamed['creator_key'],c['creator_key'])
        self.assertEqual(renamed['reviewer_notes'],'keep')
        self.assertEqual(len(self.store.all('creators')),1)
    def test_merge_id_reconciles_username_records(self):
        original=self.store.creator('oldhandle','42')
        candidate=self.store.creator('newhandle')
        self.store.update('creators','creator_key',candidate['creator_key'],{'manual_decision':'approved','reviewer_notes':'Human review'})
        merged=self.store.creator('newhandle','42')
        self.assertEqual(merged['creator_key'],original['creator_key'])
        self.assertEqual(merged['manual_decision'],'approved')
        self.assertEqual(len(self.store.all('creators')),1)
    def test_missing_profile_and_empty_results(self):
        FakeProvider.hook=lambda provider,p:{}
        self.assertEqual(self.runflow()['status'],'no_candidates')
        self.assertEqual(self.store.all('creators'),[])
        FakeProvider.hook=lambda provider,p:{'organic_results':[{'link':'https://instagram.com/missing/'}]} if p['engine']=='google' else {}
        r=self.runflow()
        self.assertEqual(r['status'],'partial')
        self.assertEqual(self.store.all('creators')[0]['qualification_status'],'needs_review')
    def test_different_expertise_different_framing(self):
        c=self.selected();self.runflow('contact_lookup');contact=self.store.all('contacts')[0]
        bodies=[]
        for text in ['Family budgeting and saving money tips.','Insurance explanations and renewal information.','Household organisation and life admin tips.']:
            d=make_draft(c,contact,{'text':text,'url':c['profile_url'],'kind':'biography'},self.config)
            bodies.append(d['audience_benefit_rationale'])
            self.assertIn(text,d['draft_body'])
            self.assertTrue(100<=len(d['draft_body'].split())<=150)
        self.assertEqual(len(set(bodies)),3)
    def test_pending_candidate_gets_email_without_manual_approval(self):
        r=self.runflow()
        c=self.store.all('creators')[0]
        self.assertEqual(c['manual_decision'],'')
        self.assertEqual(c['qualification_status'],'needs_review')
        self.assertEqual(r['contacts_processed'],1)
        self.assertEqual(len(self.store.all('contacts')),1)
        self.assertIn('Provisional draft',self.store.all('outreach_drafts')[0]['missing_information'])
    def test_no_candidates_is_explicit_and_makes_no_account_check(self):
        class Fail:
            def __init__(self,*args):raise AssertionError('No need for provider')
        r=Workflow(self.store,self.config,provider_factory=Fail).run('contact_lookup')
        self.assertEqual(r['status'],'no_candidates')
        self.assertEqual(r['account_checks'],0)
        self.assertIn('No email lookup was attempted',r['outcome_message'])
    def test_research_gate_excludes_rejected_missing_and_out_of_range(self):
        self.runflow()
        c=self.store.all('creators')[0]
        for changes,reason in [({'manual_decision':'rejected'},'rejected'),({'followers':9999},'outside_follower_range'),({'followers':None},'follower_count_missing'),({'expertise_evidence':'[]'},'relevant_content_evidence_missing')]:
            modified={**c,**changes}
            self.assertEqual(contact_skip_reason(modified,self.config),reason)
    def test_pending_draft_promotes_without_duplicate(self):
        self.runflow();c=self.store.all('creators')[0];d=self.store.all('outreach_drafts')[0]
        review_creator(self.store,c['creator_key'],{'manual_decision':'approved','reviewer_notes':'Reviewed'},self.config)
        self.runflow('draft_generation')
        drafts=self.store.all('outreach_drafts')
        self.assertEqual(len(drafts),1)
        self.assertEqual(drafts[0]['draft_key'],d['draft_key'])
        self.assertNotIn('Provisional draft',drafts[0]['missing_information'])
    def test_saved_money_advice_is_relevant_candidate_evidence(self):
        self.assertIn('budgeting',topics('The UK Money Blog featuring Money Advice'))
    def test_full_run_automatically_creates_email_and_message(self):
        def hook(provider,p):
            if p['engine']=='google':
                if 'business enquiries' in p.get('q',''):return {'organic_results':[]}
                return {'organic_results':[{'link':'https://instagram.com/with_email/'},{'link':'https://instagram.com/without_email/'}]}
            name=p['profile_id']
            return profile(name=name,identity=name,bio='UK family budgeting. Business enquiries hello@creator.example' if name=='with_email' else 'UK household organisation and life admin.')
        FakeProvider.hook=hook
        r=self.runflow()
        self.assertEqual(r['status'],'complete')
        self.assertEqual(r['contacts_processed'],2)
        self.assertEqual(r['drafts_created'],2)
        self.assertEqual({d['channel'] for d in self.store.all('outreach_drafts')},{'email','instagram_message'})
        self.assertEqual(r['current_stage'],'finished')
        self.assertTrue(all(not c['manual_decision'] for c in self.store.all('creators')))
    def test_message_dedup_and_manual_edit_preservation(self):
        self.selected('UK family budgeting.')
        self.runflow('contact_lookup');d=self.store.all('outreach_drafts')[0]
        edit_draft(self.store,d['draft_key'],{'draft_body':'My manually edited Instagram message.'})
        self.runflow('contact_lookup');self.runflow('replay')
        self.assertEqual(len(self.store.all('outreach_drafts')),1)
        self.assertEqual(self.store.all('outreach_drafts')[0]['draft_body'],'My manually edited Instagram message.')
    def test_offline_replay_does_not_skip_unattempted_lookup(self):
        c=self.selected('UK family budgeting.')
        self.store.update('creators','creator_key',c['creator_key'],{'contact_lookup_status':'not_started'})
        self.runflow('replay')
        self.assertEqual(self.store.all('outreach_drafts'),[])
    def test_quota_and_retrieval_failure_create_honest_fallback(self):
        c=self.selected('UK household organisation and life admin.')
        for status in ('lookup_deferred_quota','retrieval_failed','contact_needs_review'):
            self.store.update('creators','creator_key',c['creator_key'],{'contact_lookup_status':status})
            self.runflow('replay')
            d=self.store.all('outreach_drafts')[0]
            self.assertEqual(d['channel'],'instagram_message')
            self.assertNotIn('No suitable public business email found',d['missing_information'])
    def test_discovery_quota_still_finishes_contact_and_drafting(self):
        c=self.selected('UK family budgeting.')
        self.config['maxSerpApiRequestsPerRun']=1
        r=self.runflow('discovery')
        self.assertEqual(r['requests_attempted'],1)
        self.assertEqual(r['contacts_processed'],1)
        d=self.store.all('outreach_drafts')[0]
        self.assertEqual(d['channel'],'instagram_message')
        self.assertIn('deferred by quota',d['missing_information'])
    def test_rejected_candidate_gets_no_fallback(self):
        c=self.selected('UK family budgeting.')
        self.store.update('creators','creator_key',c['creator_key'],{'manual_decision':'rejected','contact_lookup_status':'retrieval_failed'})
        self.runflow('replay')
        self.assertEqual(self.store.all('outreach_drafts'),[])
    def test_rejected_contact_preserved(self):
        self.selected();self.runflow('contact_lookup')
        contact=self.store.all('contacts')[0]
        review_contact(self.store,contact['contact_key'],{'review_status':'rejected'})
        self.runflow('contact_lookup')
        self.assertEqual(self.store.all('contacts')[0]['review_status'],'rejected')


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'q.db');self.run='run'
        self.store.put('run_log',{'run_id':self.run,'mode':'discovery'},'run_id')
        self.account={'plan_monthly_price':0,'account_status':'Active','plan_searches_left':100,'account_rate_limit_per_hour':100,'this_hour_searches':0,'last_hour_searches':0}
        self.searches=0;self.failures=0
    def tearDown(self):self.store.close();self.temp.cleanup()
    def transport(self,u):
        if '/account.json' in u:return 200,{},json.dumps(self.account).encode()
        self.searches+=1
        if self.failures:
            self.failures-=1;return 503,{},b''
        return 200,{},b'{"organic_results":[]}'
    def api(self,cap=15):return SerpApi('SECRET_NOT_SAVED',self.store,self.run,cap,self.transport)
    def test_paid_plan_blocked(self):
        self.account['plan_monthly_price']=25
        with self.assertRaises(QuotaStop):self.api().search(engine='google')
        self.assertEqual(self.searches,0)
    def test_unknown_price_blocked(self):
        del self.account['plan_monthly_price']
        with self.assertRaises(QuotaStop):self.api().search(engine='google')
    def test_monthly_and_hourly_block(self):
        self.account['plan_searches_left']=0
        with self.assertRaises(QuotaStop):self.api().search(engine='google')
        self.account['plan_searches_left']=100;self.account['last_hour_searches']=100
        with self.assertRaises(QuotaStop):self.api().search(engine='google')
        self.assertEqual(self.searches,0)
    def test_request_cap_includes_retries(self):
        self.failures=1
        api=self.api(2)
        api.search(engine='google')
        with self.assertRaises(QuotaStop):api.search(engine='instagram_profile')
        self.assertEqual(self.searches,2)
        self.assertEqual(self.store.one('run_log','run_id',self.run)['requests_attempted'],2)
    def test_stale_allowance_does_not_allow_overrun(self):
        self.account['plan_searches_left']=1
        api=self.api();api.search(engine='google')
        with self.assertRaises(QuotaStop):api.search(engine='google')
        self.assertEqual(self.searches,1)
    def test_no_credentials_persisted(self):
        self.api().search(engine='google')
        all_data=json.dumps({t:self.store.all(t) for t in TABLES})
        self.assertNotIn('SECRET_NOT_SAVED',all_data)
        self.assertNotIn('api_key',all_data)
    def test_block_private_and_loopback(self):
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',80))]):
            with self.assertRaises(ProviderError):public_addresses('public-looking.example',80)
    def test_retry_count_is_bounded(self):
        self.failures=10
        with self.assertRaises(HttpFailure):self.api().search(engine='google')
        self.assertEqual(self.searches,2)
    def test_provider_error_text_not_logged(self):
        def transport(u):
            if '/account' in u:return self.transport(u)
            return 200,{},b'{"error":"secret URL https://serpapi.com/?api_key=SECRET_NOT_SAVED"}'
        with self.assertRaises(ProviderError) as caught:SerpApi('SECRET_NOT_SAVED',self.store,self.run,15,transport).search(engine='google')
        self.assertNotIn('SECRET_NOT_SAVED',str(caught.exception))

if __name__=='__main__':unittest.main()
