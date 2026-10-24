import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import post_authors as pa
from workflow import Workflow, Store, js, now, unpack
from test_workflow import CONFIG, FakeProvider, profile

POST='https://www.instagram.com/reel/Exact123/'
def media(owner='real_creator',code='Exact123'):
    return '<script type="application/json">'+json.dumps({'shortcode':code,'owner':{'username':owner},'caption':'Thanks @tagged_person','comments':[{'username':'commenter'}]})+'</script>'

class AuthorTests(unittest.TestCase):
    def test_owner_not_mentions_or_comments(self):
        self.assertEqual(pa.from_html(media(),POST)[0],'real_creator')
        self.assertEqual(pa.from_html(media(code='different'),POST)[0],'')
    def test_linked_author_matches_exact_post(self):
        data={'url':POST,'author':{'url':'https://instagram.com/real_creator/'}}
        html='<script type="application/ld+json">'+json.dumps(data)+'</script>'
        self.assertEqual(pa.from_html(html,POST)[0],'real_creator')
        self.assertEqual(pa.from_html(html,POST.replace('Exact123','other'))[0],'')
    def test_explicit_author_link_requires_post_identity(self):
        html=f'<meta property="og:url" content="{POST}"><a rel="author" href="/real_creator/">Creator</a><a href="/someone_else/">Tagged</a>'
        self.assertEqual(pa.from_html(html,POST)[0],'real_creator')
        self.assertEqual(pa.from_html(html.replace('og:url','other'),POST)[0],'')
    def test_author_headers(self):
        for title in ['Real Name (@real_creator) on Instagram: hello @tagged','21 likes, 2 comments - real_creator on October 19, 2026: hello @tagged']:
            html=f'<meta property="og:url" content="{POST}"><meta property="og:description" content="{title}">'
            self.assertEqual(pa.from_html(html,POST)[0],'real_creator')
    def test_conflicting_authors_unresolved(self):
        name,evidence=pa.from_html(media()+media('different'),POST)
        self.assertEqual(name,'');self.assertEqual(len(evidence),2)
    def test_search_requires_exact_post_attribution(self):
        self.assertEqual(pa.from_search({'link':POST,'source':'Instagram · real_creator'},POST)[0],'real_creator')
        self.assertEqual(pa.from_search({'link':POST,'snippet':'By @tagged'},POST)[0],'')
        self.assertEqual(pa.from_search({'link':POST+'other','source':'Instagram · real_creator'},POST)[0],'')
        self.assertEqual(pa.from_search({'link':POST,'source':'Instagram · Real Name'},POST)[0],'')
    def test_reserved_and_non_instagram_urls(self):
        for url in ['https://instagram.com/popular/travel/','https://evil.test/p/Exact123/','https://user:pass@instagram.com/p/Exact123/']:
            self.assertFalse(pa.shortcode(url))
        self.assertEqual(pa.profile_handle('https://instagram.com/popular/'),'')
        self.assertTrue(pa.same_post(POST,'https://instagram.com/p/Exact123/'))

class ResolutionFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'test.db')
        self.config={**CONFIG,'queries':['travel query']};FakeProvider.hook=None;FakeProvider.searches=[]
    def tearDown(self):
        FakeProvider.hook=None;self.store.close();self.temp.cleanup()
    def prepare(self,page):
        f=Workflow(self.store,self.config,provider_factory=FakeProvider,page_get=page)
        self.store.put('run_log',{'run_id':f.run_id,'mode':'discovery','started_at':now()},'run_id')
        f.api=FakeProvider('',self.store,f.run_id,15)
        return f
    def test_post_only_runs_profile_email_and_draft_and_caches(self):
        calls=[]
        def hook(provider,p):
            if p['engine']=='google':return {'organic_results':[{'link':POST}]}
            self.assertEqual(p['profile_id'],'real_creator')
            return profile(name='real_creator')
        FakeProvider.hook=hook
        def page(u):calls.append(u);return 200,{'content-type':'text/html'},media().encode()
        for _ in range(2):
            r=Workflow(self.store,self.config,provider_factory=FakeProvider,page_get=page).run()
            self.assertEqual(r['status'],'complete');self.assertEqual(r['post_authors_resolved'],1)
        self.assertEqual(len(calls),1);self.assertEqual(len(self.store.all('creators')),1)
        self.assertEqual(len(self.store.all('outreach_drafts')),1)
        self.assertEqual(self.store.all('outreach_drafts')[0]['channel'],'email')
    def test_current_queries_do_not_reprocess_historical_creators(self):
        old=self.store.creator('historical')
        self.store.put('search_results',{'result_key':'old','query':'old query','canonical_url':'https://instagram.com/historical/','identifiable_username':'historical'},'result_key')
        FakeProvider.hook=lambda provider,p:{'organic_results':[]}
        r=Workflow(self.store,self.config,provider_factory=FakeProvider,page_get=lambda u:None).run()
        self.assertEqual(r['creators_processed'],0);self.assertEqual(r['contacts_processed'],0)
        self.assertFalse(self.store.one('creators','creator_key',old['creator_key'])['enriched_at'])
    def test_login_redirect_not_followed_exact_post_search_resolves(self):
        calls=[]
        def page(u):calls.append(u);return 302,{'location':'https://instagram.com/accounts/login/'},b''
        FakeProvider.hook=lambda provider,p:{'organic_results':[{'link':POST,'author':{'url':'https://instagram.com/real_creator/'}}]}
        f=self.prepare(page);name,evidence,status=f.resolve_post(POST)
        self.assertEqual(name,'real_creator');self.assertEqual(calls,[POST])
        self.assertEqual(FakeProvider.searches[0]['q'],'"'+POST+'"')
        self.assertEqual(self.store.all('search_results')[0]['search_kind'],'post_author_lookup')
    def test_redirect_and_fallback_request_caps(self):
        f=self.prepare(lambda u:(302,{'location':POST},b''))
        FakeProvider.hook=lambda provider,p:{'organic_results':[]}
        for _ in range(4):f.resolve_post(POST)
        row=self.store.one('run_log','run_id',f.run_id)
        self.assertEqual(row['post_page_requests'],8);self.assertEqual(row['requests_attempted'],3)
        self.store.update('run_log','run_id',f.run_id,{'requests_attempted':14})
        self.assertEqual(f.resolve_post(POST)[2],'post_author_deferred_quota')
        self.assertEqual(len(FakeProvider.searches),3)
    def test_conflicting_page_does_not_trigger_guessing_search(self):
        f=self.prepare(lambda u:(200,{'content-type':'text/html'},(media()+media('other')).encode()))
        self.assertEqual(f.resolve_post(POST)[2],'post_author_conflict')
        self.assertEqual(FakeProvider.searches,[])
