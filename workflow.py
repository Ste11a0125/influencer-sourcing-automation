"""Nous discovery fallback. Python standard library only; never sends outreach."""
from __future__ import annotations
import contextlib
import datetime as dt
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import time
import urllib.parse as url
import uuid
from html.parser import HTMLParser
import post_authors
from competitor_bios import competitor_bio_flags

ROOT = Path(__file__).resolve().parent
MODES = ('discovery', 'replay', 'contact_lookup', 'draft_generation')
TOPICS = {
    'insurance': r'\b(?:insurance|renewals?|policy excess|home cover)\b',
    'budgeting': r'\b(?:budget(?:ing)?|saving money|money saving|money advice|money tips|save money|frugal|family finances)\b',
    'household bills': r'\b(?:household bills|energy bills|utility bills|broadband|contracts?)\b',
    'home ownership': r'\b(?:homeowner|home ownership|mortgage|first.time buyer)\b',
    'subscriptions': r'\bsubscriptions?\b',
    'life admin': r'\b(?:life admin|document organi[sz]ation|household organi[sz]ation|paperwork|filing system)\b',
    'parenting mental load': r'\b(?:mental load|parenting|family admin)\b',
}
RESERVED = set('p reel reels popular stories explore accounts direct about developer developers legal privacy terms directory web api challenge oauth tv live share sharing static graphql download press music audio shop ads create settings emails'.split())
EMAIL = re.compile(r'(?<![\w.+-])[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\b')
BUSINESS = re.compile(r'\b(?:business|enquir(?:y|ies)|inquir(?:y|ies)|collab\w*|partnerships?|work with me|management|represented|agent|bookings?|contact)\b', re.I)
REPRESENTATIVE = re.compile(r'\b(?:management|manager|represented|representation|agent|agency)\b', re.I)
JUNK_EMAIL = re.compile(r'^(?:support|privacy|abuse|noreply|no-reply|dpo|webmaster|security|legal|help)@', re.I)
VENDORS = {'wix.com', 'squarespace.com', 'wordpress.com', 'linktr.ee', 'instagram.com', 'facebook.com', 'sentry.io'}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def js(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def unpack(value, default=None):
    try:
        return json.loads(value) if value else ({} if default is None else default)
    except (ValueError, TypeError):
        return {} if default is None else default


def digest(*parts):
    return hashlib.sha256('\0'.join(str(x) for x in parts).encode()).hexdigest()[:32]


def clean_text(value):
    return str(value or '').strip()


def canonical(raw):
    """Drop tracking, fragments and credential-bearing URLs entirely."""
    try:
        p = url.urlsplit(str(raw or '').strip())
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            return ''
        if any(re.search(r'key|token|secret|password|authorization|signature', k, re.I) for k, _ in url.parse_qsl(p.query)):
            return ''
        if p.hostname.lower() == 'serpapi.com':
            return ''
        host = p.hostname.lower()
        if host in ('instagram.com', 'm.instagram.com'):
            host = 'www.instagram.com'
        if p.port and p.port not in (80, 443):
            return ''
        path = p.path or '/'
        if host == 'www.instagram.com':
            path = path.rstrip('/') + '/'
        # Public evidence URLs deliberately exclude query strings.
        return url.urlunsplit((p.scheme, host, path, '', ''))
    except (ValueError, TypeError):
        return ''


def username_from_url(raw):
    p = url.urlsplit(canonical(raw))
    parts = p.path.strip('/').split('/')
    if p.hostname != 'www.instagram.com' or len(parts) != 1:
        return ''
    candidate = parts[0].lower()
    if candidate in RESERVED or not re.fullmatch(r'[a-z0-9_](?:[a-z0-9_.]{0,28}[a-z0-9_])?', candidate):
        return ''
    return candidate


def topics(text):
    return [k for k, pattern in TOPICS.items() if re.search(pattern, text, re.I)]


def validate_config(c):
    exclusions = c.get('excludedUsernames', [])
    if not isinstance(exclusions, list) or len(exclusions) > 200:
        raise ValueError('Provide up to 200 excluded Instagram handles.')
    normalised = []
    for entry in exclusions:
        if not isinstance(entry, str):
            raise ValueError('Each excluded account must be an Instagram handle or profile URL.')
        value = entry.strip().lstrip('@').lower()
        name = username_from_url(value) if '://' in value else username_from_url('https://www.instagram.com/'+value+'/')
        if not name:
            raise ValueError('Use an exact Instagram handle or profile URL for each excluded account.')
        if name not in normalised:
            normalised.append(name)
    if 'excludedUsernames' in c:
        c['excludedUsernames'] = normalised
    if type(c.get('requireLatest20Posts', True)) is not bool:
        raise ValueError('requireLatest20Posts must be true or false.')
    if c.get('targetCountry') != 'United Kingdom':
        raise ValueError('This campaign is configured for the United Kingdom.')
    for k, low, high in [('minFollowers', 0, 100000000), ('maxFollowers', 1, 100000000),
                         ('maxProfilesPerRun', 1, 50), ('targetPostsPerCreator', 20, 20),
                         ('minimumRelevantPosts', 5, 20), ('maxSerpApiRequestsPerRun', 1, 15)]:
        if type(c.get(k)) is not int or not low <= c[k] <= high:
            raise ValueError(f'{k} must be an integer from {low} to {high}.')
    if c['minFollowers'] > c['maxFollowers']:
        raise ValueError('Minimum followers exceeds maximum.')
    if c.get('mode') not in MODES:
        raise ValueError('Unsupported mode.')
    for k in ('senderName', 'senderRole', 'approvedProductBrief'):
        if not isinstance(c.get(k), str) or len(c[k]) > 600:
            raise ValueError(f'Invalid {k}.')
    if not c['approvedProductBrief'].strip():
        raise ValueError('An approved product brief is required.')
    if len(c['approvedProductBrief'].split()) > 25:
        raise ValueError('Keep the approved product brief within 25 words for concise drafts.')
    facts = c.get('approvedProductFacts')
    if not isinstance(facts, list) or any(not isinstance(f, str) or len(f.split()) > 20 for f in facts):
        raise ValueError('Each approved product fact must be text of at most 20 words.')
    queries = c.get('queries')
    if not isinstance(queries, list) or not 1 <= len(queries) <= 5 or any(not isinstance(q, str) or len(q) > 300 for q in queries):
        raise ValueError('Provide 1–5 search queries, each within 300 characters.')
    if set(c) - {'targetCountry','minFollowers','maxFollowers','maxProfilesPerRun','targetPostsPerCreator',
                 'minimumRelevantPosts','maxSerpApiRequestsPerRun','mode','senderName','senderRole',
                 'approvedProductBrief','approvedProductFacts','queries','requireLatest20Posts','excludedUsernames'}:
        raise ValueError('Unknown configuration field. Credentials must not be saved in configuration.')
    return c


SCHEMA = '''
CREATE TABLE IF NOT EXISTS search_results (
 result_key TEXT PRIMARY KEY, query TEXT, canonical_url TEXT, title TEXT, snippet TEXT,
 identifiable_username TEXT, retrieved_at TEXT, processing_status TEXT,
 UNIQUE(query, canonical_url));
CREATE TABLE IF NOT EXISTS creators (
 creator_key TEXT PRIMARY KEY, instagram_id TEXT UNIQUE, username TEXT UNIQUE, full_name TEXT DEFAULT '',
 profile_url TEXT, biography TEXT DEFAULT '', followers INTEGER, bio_links_json TEXT DEFAULT '[]',
 expertise TEXT DEFAULT '', expertise_evidence TEXT DEFAULT '[]', discovery_evidence TEXT DEFAULT '[]',
 screening_results TEXT DEFAULT '{}', qualification_status TEXT DEFAULT 'needs_review',
 missing_information TEXT DEFAULT '[]', manual_decision TEXT DEFAULT '', reviewer_notes TEXT DEFAULT '',
 review_json TEXT DEFAULT '{}', enriched_at TEXT, latest_sample_json TEXT DEFAULT '[]',
 contact_lookup_status TEXT DEFAULT 'not_started', contact_lookup_at TEXT,
 location_evidence TEXT DEFAULT '', audience_geography TEXT DEFAULT 'unknown');
CREATE TABLE IF NOT EXISTS post_evidence (
 creator_key TEXT, post_id TEXT, post_url TEXT, caption TEXT, available_timestamp TEXT,
 matched_topics TEXT, assessment_evidence TEXT, PRIMARY KEY(creator_key, post_id));
CREATE TABLE IF NOT EXISTS contacts (
 contact_key TEXT PRIMARY KEY, creator_key TEXT, contact_type TEXT, contact_value TEXT,
 recipient_name TEXT DEFAULT '', recipient_role TEXT, source_url TEXT, supporting_text TEXT,
 attribution_status TEXT, review_status TEXT DEFAULT 'discovered', discovered_at TEXT,
 UNIQUE(creator_key, contact_type, contact_value));
CREATE TABLE IF NOT EXISTS outreach_drafts (
 draft_key TEXT PRIMARY KEY, creator_key TEXT, contact_key TEXT, channel TEXT, recipient_email TEXT,
 subject TEXT, draft_body TEXT, expertise_reference TEXT, audience_benefit_rationale TEXT,
 evidence_urls_json TEXT, product_facts_used_json TEXT, draft_status TEXT,
 missing_information TEXT, created_at TEXT, updated_at TEXT, manually_edited INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS run_log (
 run_id TEXT PRIMARY KEY, mode TEXT, requests_attempted INTEGER DEFAULT 0, account_checks INTEGER DEFAULT 0,
 public_page_requests INTEGER DEFAULT 0, results_saved INTEGER DEFAULT 0, creators_processed INTEGER DEFAULT 0,
 screening_outcomes TEXT DEFAULT '{}', contacts_found INTEGER DEFAULT 0, drafts_created INTEGER DEFAULT 0,
 errors TEXT DEFAULT '[]', started_at TEXT, duration REAL DEFAULT 0, status TEXT DEFAULT 'running');
CREATE TABLE IF NOT EXISTS request_ledger (run_id TEXT, attempted_at REAL);
'''
TABLES = ('search_results', 'creators', 'post_evidence', 'contacts', 'outreach_drafts', 'run_log')


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)
        existing = {r[1] for r in self.db.execute('PRAGMA table_info(run_log)')}
        for field, definition in {
            'contact_candidates': 'INTEGER DEFAULT 0', 'contacts_processed': 'INTEGER DEFAULT 0',
            'skipped_creators': "TEXT DEFAULT '{}'", 'outcome_message': "TEXT DEFAULT ''",
            'current_stage': "TEXT DEFAULT 'starting'", 'discovery_results': 'INTEGER DEFAULT 0',
            'post_authors_resolved': 'INTEGER DEFAULT 0', 'post_page_requests': 'INTEGER DEFAULT 0',
            'queries_json': "TEXT DEFAULT '[]'"
        }.items():
            if field not in existing:
                self.db.execute(f'ALTER TABLE run_log ADD COLUMN {field} {definition}')
        existing = {r[1] for r in self.db.execute('PRAGMA table_info(search_results)')}
        for field, definition in {'author_evidence_json': "TEXT DEFAULT '[]'",
                'last_run_id': "TEXT DEFAULT ''", 'search_kind': "TEXT DEFAULT 'discovery'"}.items():
            if field not in existing:
                self.db.execute(f'ALTER TABLE search_results ADD COLUMN {field} {definition}')
        self.db.commit()
        os.chmod(self.path, 0o600)

    def all(self, table, where='', args=()):
        assert table in TABLES
        return [dict(r) for r in self.db.execute(f'SELECT * FROM {table}' + (' WHERE '+where if where else ''), args)]

    def one(self, table, field, value):
        rows = self.all(table, f'{field}=?', (value,))
        return rows[0] if rows else None

    def put(self, table, record, key):
        assert table in TABLES
        columns = [r[1] for r in self.db.execute(f'PRAGMA table_info({table})')]
        assert all(k in columns for k in record)
        fields = list(record)
        updates = ','.join(f'{f}=excluded.{f}' for f in fields if f not in key.split(','))
        self.db.execute(f'INSERT INTO {table} ({",".join(fields)}) VALUES ({",".join("?" for _ in fields)}) '
                        f'ON CONFLICT({key}) DO UPDATE SET {updates}', tuple(record[f] for f in fields))
        self.db.commit()

    def update(self, table, key, value, changes):
        assert table in TABLES
        columns = [r[1] for r in self.db.execute(f'PRAGMA table_info({table})')]
        assert key in columns and all(k in columns for k in changes)
        self.db.execute(f'UPDATE {table} SET '+','.join(f'{k}=?' for k in changes)+f' WHERE {key}=?',
                        (*changes.values(), value))
        self.db.commit()

    def close(self):
        self.db.close()

    def creator(self, username, instagram_id=None):
        old = self.one('creators', 'username', username)
        by_id = self.one('creators', 'instagram_id', str(instagram_id)) if instagram_id else None
        if old and by_id and old['creator_key'] != by_id['creator_key']:
            self.merge_creators(old, by_id)
            old = None
        row = by_id or old
        if row:
            # Do not reassign an established Instagram identity to a recycled handle.
            if instagram_id and row['instagram_id'] and row['instagram_id'] != str(instagram_id):
                raise ProviderError('Profile identity conflict; review the saved creator before continuing.')
            self.update('creators', 'creator_key', row['creator_key'], {
                'username': username, 'instagram_id': str(instagram_id) if instagram_id else row['instagram_id'],
                'profile_url': f'https://www.instagram.com/{username}/'})
            return self.one('creators', 'creator_key', row['creator_key'])
        key = str(uuid.uuid4())
        self.put('creators', {'creator_key': key, 'instagram_id': str(instagram_id) if instagram_id else None,
                             'username': username, 'profile_url': f'https://www.instagram.com/{username}/'}, 'creator_key')
        return self.one('creators', 'creator_key', key)

    def merge_creators(self, source, target):
        """Stable internal keys; preserve human edits when an ID joins two records."""
        src, dst = source['creator_key'], target['creator_key']
        with self.db:
            for p in self.all('post_evidence', 'creator_key=?', (src,)):
                p['creator_key'] = dst
                fields = list(p)
                self.db.execute(f'INSERT OR IGNORE INTO post_evidence ({",".join(fields)}) VALUES ({",".join("?" for _ in fields)})', tuple(p.values()))
            self.db.execute('DELETE FROM post_evidence WHERE creator_key=?', (src,))
            for contact in self.all('contacts', 'creator_key=?', (src,)):
                matches = self.all('contacts', 'creator_key=? AND contact_type=? AND contact_value=?',
                                   (dst, contact['contact_type'], contact['contact_value']))
                if matches:
                    survivor = matches[0]
                    if contact['review_status'] in ('human_confirmed', 'approved', 'rejected') and survivor['review_status'] == 'discovered':
                        self.db.execute('UPDATE contacts SET review_status=?,attribution_status=?,recipient_name=?,recipient_role=? WHERE contact_key=?',
                                        (contact['review_status'],contact['attribution_status'],contact['recipient_name'],contact['recipient_role'],survivor['contact_key']))
                    self.db.execute('UPDATE outreach_drafts SET contact_key=? WHERE contact_key=?', (survivor['contact_key'], contact['contact_key']))
                    self.db.execute('DELETE FROM contacts WHERE contact_key=?', (contact['contact_key'],))
                else:
                    self.db.execute('UPDATE contacts SET creator_key=? WHERE contact_key=?', (dst, contact['contact_key']))
            self.db.execute('UPDATE outreach_drafts SET creator_key=? WHERE creator_key=?', (dst, src))
            notes = '\n'.join(x for x in [target['reviewer_notes'], source['reviewer_notes']] if x)
            decisions = {x for x in [target['manual_decision'], source['manual_decision']] if x}
            decision = next(iter(decisions)) if len(decisions) == 1 else ''
            if len(decisions) > 1:
                notes += '\nMerged conflicting manual decisions; review required: '+', '.join(sorted(decisions))
            reviews = unpack(target['review_json']) or unpack(source['review_json'])
            if len(decisions) > 1:
                reviews = {}
            evidence = list(dict.fromkeys(unpack(target['discovery_evidence'], []) + unpack(source['discovery_evidence'], [])))
            self.db.execute('UPDATE creators SET manual_decision=?,reviewer_notes=?,review_json=?,discovery_evidence=? WHERE creator_key=?',
                            (decision, notes, js(reviews), js(evidence), dst))
            self.db.execute('DELETE FROM creators WHERE creator_key=?', (src,))


class ProviderError(Exception):
    pass


class QuotaStop(ProviderError):
    pass


class HttpFailure(ProviderError):
    def __init__(self, status=0):
        self.status = status
        super().__init__(f'Request failed (HTTP {status}).' if status else 'Request failed or timed out.')


def public_addresses(host, port):
    addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ProviderError('Blocked non-public destination.')
    return addresses


def http_get(raw_url, *, max_bytes=2_000_000):
    """Pinned, public-only DNS, HTTPS verification, no automatic redirects or proxies."""
    try:
        p = url.urlsplit(raw_url)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
            raise ProviderError('Unsupported public URL.')
        port = p.port or (443 if p.scheme == 'https' else 80)
        addresses = public_addresses(p.hostname, port)
        conn = http.client.HTTPConnection(p.hostname, port, timeout=18)
        sock = socket.create_connection((addresses[0], port), timeout=18)
        if p.scheme == 'https':
            try:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname)
            except Exception:
                sock.close()
                raise
        conn.sock = sock
        try:
            path = url.urlunsplit(('', '', p.path or '/', p.query, ''))
            conn.request('GET', path, headers={'User-Agent': 'NousResearch/1.0 (public business contact research)', 'Accept': 'application/json,text/html'})
            response = conn.getresponse()
            data = response.read(max_bytes+1)
            if len(data) > max_bytes:
                raise ProviderError('Response exceeded size limit.')
            return response.status, dict((k.lower(), v) for k, v in response.getheaders()), data
        finally:
            conn.close()
    except ProviderError:
        raise
    except Exception:
        # Never propagate an exception containing a key-bearing request URL.
        raise HttpFailure() from None


class SerpApi:
    def __init__(self, key, store, run_id, cap, transport=http_get):
        self.key = key
        self.store = store
        self.run_id = run_id
        self.cap = cap
        self.transport = transport
        self.initial_month = None
        self.used = 0

    def check(self):
        if not self.key:
            raise ProviderError('Enter your SerpApi key locally before running collection.')
        row = self.store.one('run_log', 'run_id', self.run_id)
        self.store.update('run_log', 'run_id', self.run_id, {'account_checks': row['account_checks']+1})
        status, headers, body = self.transport('https://serpapi.com/account.json?'+url.urlencode({'api_key': self.key}))
        if status != 200:
            raise ProviderError('Could not verify the free plan and allowance; collection stopped.')
        try:
            account = json.loads(body)
            price = account['plan_monthly_price']
            if isinstance(price, bool) or float(price) != 0 or str(account.get('account_status', '')).lower() != 'active':
                raise QuotaStop('Only an active zero-price SerpApi plan is permitted.')
            monthly = account['plan_searches_left']
            hourly = account['account_rate_limit_per_hour']
            current = account['this_hour_searches']
            previous = account['last_hour_searches']
            if any(type(x) is not int or x < 0 for x in (monthly, hourly, current, previous)):
                raise ValueError()
        except QuotaStop:
            raise
        except (KeyError, ValueError, TypeError):
            raise QuotaStop('Allowance fields are missing or unrecognised; collection stopped.') from None
        if self.initial_month is None:
            self.initial_month = monthly
        remaining_month = min(monthly, self.initial_month - self.used)
        local_hour = self.store.db.execute('SELECT COUNT(*) FROM request_ledger WHERE attempted_at>?', (time.time()-3600,)).fetchone()[0]
        # Conservatively include the previous clock hour to respect rolling limits.
        remaining_hour = hourly - max(current + previous, local_hour)
        if remaining_month < 1 or remaining_hour < 1:
            raise QuotaStop('Free monthly or hourly allowance exhausted; collection deferred.')

    def search(self, **params):
        for attempt in range(2):
            row = self.store.one('run_log', 'run_id', self.run_id)
            if row['requests_attempted'] >= self.cap:
                raise QuotaStop('Shared per-run search cap reached; collection deferred.')
            self.check()
            # Reserve before the network request. Failures and retries count too.
            with self.store.db:
                self.store.db.execute('UPDATE run_log SET requests_attempted=requests_attempted+1 WHERE run_id=?', (self.run_id,))
                self.store.db.execute('INSERT INTO request_ledger VALUES (?,?)', (self.run_id, time.time()))
            self.used += 1
            try:
                status, _, body = self.transport('https://serpapi.com/search.json?'+url.urlencode({**params, 'api_key': self.key}))
                if status == 429:
                    raise QuotaStop('Provider rate limit reached; collection deferred.')
                if status != 200:
                    raise HttpFailure(status)
                try:
                    data = json.loads(body)
                except (ValueError, TypeError):
                    raise ProviderError('Provider returned invalid JSON.') from None
                if not isinstance(data, dict) or data.get('error') or data.get('search_metadata', {}).get('status') in ('Error', 'Processing'):
                    raise ProviderError('Provider could not return a complete result.')
                return data
            except HttpFailure as exc:
                if attempt == 0 and (exc.status == 0 or exc.status >= 500):
                    time.sleep(0.5)
                    continue
                raise


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.anchors = [], [], []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style', 'noscript'):
            self.skip += 1
        if tag == 'a' and attrs.get('href'):
            item = [attrs['href'], '']
            self.links.append(item)
            self.anchors.append(item)

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.skip = max(0, self.skip-1)
        if tag == 'a' and self.anchors:
            self.anchors.pop()

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)
            if self.anchors:
                self.anchors[-1][1] += data

    @property
    def text(self):
        return re.sub(r'\s+', ' ', ' '.join(self.parts)).strip()


def excluded_account(creator, config):
    return creator['username'].lower() in config.get('excludedUsernames', [])


def assess(store, creator, config):
    posts = store.all('post_evidence', 'creator_key=?', (creator['creator_key'],))
    sample = set(unpack(creator['latest_sample_json'], []))
    review = unpack(creator['review_json'])
    relevant = set(review.get('relevant_post_ids', [])) & sample
    followers = creator['followers']
    in_range = type(followers) is int and config['minFollowers'] <= followers <= config['maxFollowers']
    latest_ids = set(review.get('latest_post_ids', []))
    chronological = bool(review.get('chronological_evidence')) and len(latest_ids) == config['targetPostsPerCreator'] and latest_ids <= sample
    confirmed = len(relevant & latest_ids)
    strict_posts = config.get('requireLatest20Posts', True)
    bio_topics = topics(creator['biography'])
    matched = [p for p in posts if p['post_id'] in sample and unpack(p['matched_topics'], [])]
    content_supported = bool(bio_topics or matched or relevant)
    uk = review.get('uk_suitability') is True and bool(review.get('uk_evidence'))
    missing = []
    if followers is None:
        missing.append('Numeric follower count unavailable')
    elif not in_range:
        missing.append('Follower count outside configured range')
    if strict_posts and not chronological:
        missing.append('Latest 20-post coverage has not been verified')
    if strict_posts and confirmed < config['minimumRelevantPosts']:
        missing.append('At least five substantive relevant posts in the latest 20 need confirmation')
    if not strict_posts and not content_supported:
        missing.append('Relevant biography or post evidence is needed')
    if not uk:
        missing.append('UK household suitability needs supporting evidence')
    content_ok = (chronological and confirmed >= config['minimumRelevantPosts']) if strict_posts else content_supported
    status = 'qualified' if in_range and content_ok and uk else 'needs_review'
    if followers is not None and not in_range:
        status = 'not_qualified'
    if creator['manual_decision'] == 'approved':
        status = 'manually_approved'
    elif creator['manual_decision'] == 'rejected':
        status = 'rejected'
    competitor_flags = competitor_bio_flags(creator['biography'], creator['profile_url'])
    if competitor_flags:
        status = 'flagged_competitor'
        missing.insert(0, 'Red flag: bio presents a possible competing service provider; qualification and outreach blocked')
    if excluded_account(creator, config):
        status = 'excluded_competitor'
        missing.insert(0, 'Excluded by campaign competitor/account list')
    groups = list(dict.fromkeys(bio_topics + [t for p in matched for t in unpack(p['matched_topics'], [])]))
    evidence = ([{'text': creator['biography'], 'url': creator['profile_url'], 'kind': 'biography'}] if bio_topics else [])
    evidence += [{'text': p['caption'], 'url': p['post_url'], 'kind': 'caption'} for p in matched if p['post_url']]
    screening = {'sample_size': len(sample), 'keyword_candidate_posts': len(matched), 'confirmed_relevant_latest_posts': confirmed,
                 'competitor_bio_flags': competitor_flags,
                 'latest_20_required': strict_posts, 'content_evidence_available': content_supported,
                 'latest_20_verified': chronological, 'uk_suitability_verified': uk, 'topic_groups': groups,
                 'review_priority': len(groups)*10 + min(len(sample), 20) + (5 if followers is not None else 0),
                 'note': 'Keywords suggest topics only. No audience demographics or engagement inferred.'}
    store.update('creators', 'creator_key', creator['creator_key'], {'qualification_status': status,
        'missing_information': js(missing), 'screening_results': js(screening), 'expertise': ', '.join(groups),
        'expertise_evidence': js(evidence)})
    return status


def eligible(creator):
    return creator['qualification_status'] in ('qualified', 'manually_approved') and creator['manual_decision'] != 'rejected'


def contact_skip_reason(creator, config):
    """Research eligibility is separate from final campaign qualification."""
    if excluded_account(creator, config):
        return 'excluded_competitor'
    if competitor_bio_flags(creator['biography'], creator['profile_url']):
        return 'flagged_competitor'
    if creator['manual_decision'] == 'rejected' or creator['qualification_status'] == 'rejected':
        return 'rejected'
    if eligible(creator):
        return ''
    followers = creator['followers']
    if type(followers) is not int:
        return 'follower_count_missing'
    if not config['minFollowers'] <= followers <= config['maxFollowers']:
        return 'outside_follower_range'
    if not unpack(creator['expertise_evidence'], []):
        return 'relevant_content_evidence_missing'
    return ''


def contact_candidate(creator, config):
    return not contact_skip_reason(creator, config)


def usable_contacts(store, creator_key):
    return [c for c in store.all('contacts', 'creator_key=?', (creator_key,))
            if c['contact_type'] == 'email' and c['attribution_status'] in ('creator', 'representative')
            and c['review_status'] != 'rejected']


LOOKUP_OUTCOMES = {'contact_found', 'no_contact_found', 'contact_needs_review',
                   'retrieval_failed', 'lookup_deferred_quota'}


def current_draft(store, draft, config):
    creator = store.one('creators', 'creator_key', draft['creator_key'])
    if not creator or not contact_candidate(creator, config) or draft['draft_status'] == 'rejected':
        return False
    contacts = usable_contacts(store, creator['creator_key'])
    if draft['channel'] == 'instagram_message':
        return not contacts and creator['contact_lookup_status'] in LOOKUP_OUTCOMES
    return any(c['contact_key'] == draft['contact_key'] for c in contacts)


class Workflow:
    def __init__(self, store, config, key='', provider_factory=SerpApi, page_get=http_get):
        self.store = store
        self.config = validate_config(config)
        self.key = key
        self.provider_factory = provider_factory
        self.page_get = page_get
        self.run_id = str(uuid.uuid4())
        self.errors = []
        self.api = None
        self.discovery_result_keys = set()
        self.discovery_creator_keys = set()
        self.post_searches = 0

    def error(self, stage, exc):
        # Only our intentionally safe errors are persisted, never raw exception strings.
        message = str(exc) if isinstance(exc, ProviderError) else f'{type(exc).__name__}; stage could not complete'
        if self.key:
            message = message.replace(self.key, '[redacted]')
        self.errors.append({'stage': stage, 'message': message})
        self.store.update('run_log', 'run_id', self.run_id, {'errors': js(self.errors)})

    def increment(self, field, amount=1):
        row = self.store.one('run_log', 'run_id', self.run_id)
        self.store.update('run_log', 'run_id', self.run_id, {field: row[field]+amount})

    def stage(self, value):
        self.store.update('run_log', 'run_id', self.run_id, {'current_stage': value})

    def run(self, mode=None):
        mode = mode or self.config['mode']
        if mode not in MODES:
            raise ValueError('Unsupported mode.')
        start = time.monotonic()
        lock_path = self.store.path.with_suffix('.lock')
        with open(lock_path, 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProviderError('Another run is already active for this database.') from None
            self.store.put('run_log', {'run_id': self.run_id, 'mode': mode, 'started_at': now(), 'queries_json': js(self.config['queries'])}, 'run_id')
            status = 'complete'
            try:
                if mode == 'discovery':
                    self.api = self.provider_factory(self.key, self.store, self.run_id, self.config['maxSerpApiRequestsPerRun'])
                    self.api.check()
                if mode == 'discovery':
                    self.stage('discovery_and_profile_collection')
                    try:
                        self.discover()
                    except QuotaStop as exc:
                        # Preserve collected candidates and finish public-contact/drafting stages.
                        self.error('discovery_quota', exc)
                self.stage('assessing_creators')
                for c in self.store.all('creators'):
                    assess(self.store, c, self.config)
                if mode in ('discovery', 'contact_lookup'):
                    candidates, skipped = [], {}
                    for c in self.store.all('creators'):
                        if mode == 'discovery' and c['creator_key'] not in self.discovery_creator_keys:
                            continue
                        reason = contact_skip_reason(c, self.config)
                        if reason:
                            skipped[reason] = skipped.get(reason, 0) + 1
                        else:
                            candidates.append(c)
                    candidates.sort(key=lambda c: c['contact_lookup_at'] or '')
                    if len(candidates) > self.config['maxProfilesPerRun']:
                        skipped['per_run_profile_limit'] = len(candidates) - self.config['maxProfilesPerRun']
                    self.store.update('run_log', 'run_id', self.run_id, {
                        'contact_candidates': len(candidates), 'skipped_creators': js(skipped)})
                    if candidates and self.api is None:
                        self.api = self.provider_factory(self.key, self.store, self.run_id, self.config['maxSerpApiRequestsPerRun'])
                        self.api.check()
                    if not candidates:
                        status = 'no_candidates'
                    for c in candidates[:self.config['maxProfilesPerRun']]:
                        try:
                            self.stage('finding_email_for_' + c['username'])
                            self.contacts(c)
                        except Exception as exc:
                            self.error('contact_lookup', exc)
                            self.store.update('creators', 'creator_key', c['creator_key'], {'contact_lookup_status': 'retrieval_failed'})
                        finally:
                            self.increment('contacts_processed')
                            self.stage('drafting_for_' + c['username'])
                            self.drafts({c['creator_key']})
                # Catch existing saved candidates too, without separate user-triggered stages.
                self.stage('saving_outreach_drafts')
                self.drafts(self.discovery_creator_keys if mode == 'discovery' else None)
                if self.errors:
                    status = 'partial'
            except QuotaStop as exc:
                self.error(mode, exc)
                status = 'deferred'
                for c in self.store.all('creators'):
                    assess(self.store, c, self.config)
                self.drafts(self.discovery_creator_keys if mode == 'discovery' else None)
            except Exception as exc:
                self.error(mode, exc)
                status = 'failed'
            finally:
                outcomes = {}
                for c in self.store.all('creators'):
                    outcomes[c['qualification_status']] = outcomes.get(c['qualification_status'], 0)+1
                row = self.store.one('run_log', 'run_id', self.run_id)
                if mode in ('discovery', 'contact_lookup'):
                    message = (f"Checked emails for {row['contacts_processed']} of {row['contact_candidates']} candidates; "
                               f"saved {row['contacts_found']} new emails and {row['drafts_created']} new drafts.")
                    if status == 'no_candidates':
                        message = 'No eligible contact-research candidates. No email lookup was attempted; see skipped reasons.'
                    elif status in ('failed', 'deferred'):
                        message = 'Collection stopped or deferred. ' + message
                else:
                    message = f"Processed stored evidence offline; created {row['drafts_created']} new drafts."
                if mode == 'discovery':
                    message = (f"{row['discovery_results']} discovery results; {len(self.discovery_creator_keys)} profile candidates; "
                               f"{row['post_authors_resolved']} post authors resolved. " + message)
                self.store.update('run_log', 'run_id', self.run_id, {'status': status, 'duration': round(time.monotonic()-start, 2),
                    'screening_outcomes': js(outcomes), 'errors': js(self.errors), 'outcome_message': message, 'current_stage': 'finished'})
        return self.store.one('run_log', 'run_id', self.run_id)

    def save_search(self, query, data, kind='discovery'):
        for item in data.get('organic_results') or []:
            if not isinstance(item, dict):
                continue
            link = canonical(item.get('link'))
            if not link:
                continue
            username = username_from_url(link)
            key = digest(query, link)
            previous = self.store.one('search_results', 'result_key', key)
            evidence = unpack(previous.get('author_evidence_json'), []) if previous else []
            if not username and post_authors.shortcode(link):
                resolved, indexed = post_authors.from_search(item, link)
                if resolved:
                    username, evidence = resolved, indexed
                elif indexed:
                    evidence = indexed
                elif evidence and previous['identifiable_username']:
                    username = previous['identifiable_username']
            self.store.put('search_results', {'result_key': key, 'query': query, 'canonical_url': link,
                'title': clean_text(item.get('title')), 'snippet': clean_text(item.get('snippet')),
                'identifiable_username': username, 'retrieved_at': now(), 'last_run_id': self.run_id,
                'search_kind': kind, 'author_evidence_json': js(evidence),
                'processing_status': ('pending' if username else 'unresolved_url')}, 'result_key')
            self.increment('results_saved')
            if kind == 'discovery':
                self.discovery_result_keys.add(key)
                self.increment('discovery_results')

    def discover(self):
        # Commit all available search results before any profile request.
        quota = None
        for query in self.config['queries']:
            try:
                data = self.api.search(engine='google', q=query, google_domain='google.co.uk', gl='uk', hl='en', location='United Kingdom')
                self.save_search(query, data)
            except QuotaStop as exc:
                quota = exc
                break
            except Exception as exc:
                self.error('search', exc)
        current_results = [r for r in self.store.all('search_results') if r['result_key'] in self.discovery_result_keys]
        post_budget = min(self.config['maxProfilesPerRun'], 10)
        attempted_posts = 0
        resolved_cache = {}
        for row in current_results:
            if not post_authors.shortcode(row['canonical_url']):
                continue
            if row['identifiable_username']:
                self.increment('post_authors_resolved')
                continue
            post_code = post_authors.shortcode(row['canonical_url'])
            if post_code in resolved_cache:
                name, evidence, resolution_status = resolved_cache[post_code]
            elif attempted_posts < post_budget:
                attempted_posts += 1
                self.stage('resolving_post_creator')
                name, evidence, resolution_status = self.resolve_post(row['canonical_url'])
                resolved_cache[post_code] = (name, evidence, resolution_status)
            else:
                name, evidence, resolution_status = '', [], 'post_author_deferred_limit'
            self.store.update('search_results', 'result_key', row['result_key'], {
                'identifiable_username': name, 'author_evidence_json': js(evidence), 'processing_status': resolution_status})
            if name:
                self.increment('post_authors_resolved')
        current_results = [r for r in self.store.all('search_results') if r['result_key'] in self.discovery_result_keys]
        names = list(dict.fromkeys(r['identifiable_username'] for r in current_results if r['identifiable_username']))
        candidates = []
        for name in names:
            c = self.store.creator(name)
            evidence = [r['canonical_url'] for r in self.store.all('search_results', 'identifiable_username=?', (name,))]
            self.store.update('creators', 'creator_key', c['creator_key'], {'discovery_evidence': js(evidence)})
            candidates.append(c)
            self.discovery_creator_keys.add(c['creator_key'])
        candidates.sort(key=lambda c: c['enriched_at'] or '')
        # Keep discovered evidence, but do not spend profile requests on excluded accounts.
        for c in candidates:
            if excluded_account(c, self.config):
                assess(self.store, c, self.config)
                self.store.update('search_results', 'identifiable_username', c['username'], {'processing_status': 'excluded_competitor'})
        candidates = [c for c in candidates if not excluded_account(c, self.config)]
        for c in candidates[:self.config['maxProfilesPerRun']]:
            if quota:
                break
            try:
                self.stage('enriching_' + c['username'])
                self.enrich(c)
            except QuotaStop as exc:
                quota = exc
            except Exception as exc:
                self.error('profile @'+c['username'], exc)
                self.store.update('search_results', 'identifiable_username', c['username'], {'processing_status': 'retrieval_failed'})
            finally:
                current = self.store.one('creators', 'username', c['username'])
                if current:
                    assess(self.store, current, self.config)
        if quota:
            raise quota

    def resolve_post(self, post_url):
        """Two public-page requests max (redirect included), then one exact-post search."""
        target = post_url
        failure = False
        for _ in range(2):
            self.increment('post_page_requests')
            try:
                status, headers, body = self.page_get(target)
                if status in (301, 302, 303, 307, 308):
                    redirected = canonical(url.urljoin(target, headers.get('location', '')))
                    # Login redirects and unrelated pages are not followed.
                    if not post_authors.same_post(redirected, post_url):
                        failure = True
                        break
                    target = redirected
                    continue
                if status != 200 or 'text/html' not in headers.get('content-type', ''):
                    failure = True
                    break
                name, evidence = post_authors.from_html(body.decode('utf-8', errors='replace'), post_url)
                if name:
                    return name, evidence, 'post_author_resolved'
                if evidence:
                    return '', evidence, 'post_author_conflict'
                break
            except Exception:
                failure = True
                break
        # Reserve at least two search attempts for profile enrichment; max three
        # post-author searches per run, all charged to the same global cap.
        used = self.store.one('run_log', 'run_id', self.run_id)['requests_attempted']
        if self.api is None or self.post_searches >= 3 or self.config['maxSerpApiRequestsPerRun'] - used <= 2:
            return '', [], 'post_author_deferred_quota'
        self.post_searches += 1
        query = '"' + post_url + '"'
        try:
            data = self.api.search(engine='google', q=query, google_domain='google.co.uk', gl='uk', hl='en')
            self.save_search(query, data, kind='post_author_lookup')
            evidence = []
            for item in data.get('organic_results') or []:
                name, candidate_evidence = post_authors.from_search(item, post_url)
                evidence.extend(candidate_evidence)
            name, evidence = post_authors.author_result(evidence)
            if name:
                return name, evidence, 'post_author_resolved'
            if evidence:
                return '', evidence, 'post_author_conflict'
        except QuotaStop:
            return '', [], 'post_author_deferred_quota'
        except Exception:
            failure = True
        return '', [], 'post_author_fetch_failed' if failure else 'post_author_unresolved'

    def enrich(self, creator):
        data = self.api.search(engine='instagram_profile', profile_id=creator['username'])
        profile = data.get('profile_results')
        if not isinstance(profile, dict) or not profile:
            raise ProviderError('Profile is unavailable or missing.')
        actual_name = str(profile.get('username') or creator['username']).lower()
        if actual_name != creator['username']:
            raise ProviderError('Returned username differs from requested profile; manual review required.')
        c = self.store.creator(creator['username'], profile.get('id'))
        links = []
        for item in profile.get('bio_links') or []:
            link = canonical(item.get('url') if isinstance(item, dict) else item)
            if link:
                links.append(link)
        external = canonical(profile.get('external_url'))
        if external:
            links.append(external)
        followers = profile.get('followers')
        followers = followers if type(followers) is int and followers >= 0 else None
        self.store.update('creators', 'creator_key', c['creator_key'], {'full_name': clean_text(profile.get('full_name')),
            'biography': clean_text(profile.get('biography')), 'followers': followers, 'bio_links_json': js(list(dict.fromkeys(links))),
            'enriched_at': now()})
        ids = []
        if followers is not None and self.config['minFollowers'] <= followers <= self.config['maxFollowers']:
            self.save_posts(c, profile.get('posts') or [], ids)
            self.store.update('creators', 'creator_key', c['creator_key'], {'latest_sample_json': js(ids)})
            token = (data.get('serpapi_pagination') or {}).get('next_page_token')
            if not competitor_bio_flags(profile.get('biography')) and self.config.get('requireLatest20Posts', True) and token and len(ids) < self.config['targetPostsPerCreator']:
                more = self.api.search(engine='instagram_profile', profile_id=c['username'], next_page_token=token)
                self.save_posts(c, (more.get('profile_results') or {}).get('posts') or more.get('posts') or [], ids)
        self.store.update('creators', 'creator_key', c['creator_key'], {'latest_sample_json': js(ids)})
        self.store.update('search_results', 'identifiable_username', c['username'], {'processing_status': 'enriched'})
        self.increment('creators_processed')

    def save_posts(self, creator, posts, ids):
        for post in posts:
            if len(ids) >= self.config['targetPostsPerCreator']:
                break
            if not isinstance(post, dict):
                continue
            owner = post.get('owner') or {}
            if owner.get('username') and owner['username'].lower() != creator['username']:
                continue
            post_id = str(post.get('id') or post.get('shortcode') or '')
            if not post_id or post_id in ids:
                continue
            shortcode = post.get('shortcode')
            post_url = f'https://www.instagram.com/p/{shortcode}/' if shortcode and re.fullmatch(r'[\w-]+', str(shortcode)) else canonical(post.get('link'))
            captions = post.get('media_captions')
            caption = '\n'.join(x for x in captions if isinstance(x, str)) if isinstance(captions, list) else clean_text(post.get('caption'))
            matched = topics(caption)
            self.store.put('post_evidence', {'creator_key': creator['creator_key'], 'post_id': post_id, 'post_url': post_url,
                'caption': caption, 'available_timestamp': clean_text(post.get('taken_at_timestamp') or post.get('timestamp')),
                'matched_topics': js(matched), 'assessment_evidence': js({'supporting_text': caption, 'substantive_relevance': 'needs_review'})}, 'creator_key,post_id')
            ids.append(post_id)

    def save_emails(self, creator, text, source, origin, linked=False):
        for match in EMAIL.finditer(text):
            email = match.group().lower().rstrip('.')
            domain = email.rsplit('@', 1)[1]
            if JUNK_EMAIL.search(email) or any(domain == d or domain.endswith('.'+d) for d in VENDORS):
                continue
            context = text[max(0, match.start()-140):match.end()+140]
            if not BUSINESS.search(context):
                continue
            # A linked page is attributable only if the creator is explicitly identified.
            identifies = creator['username'].lower() in text.lower() or (len(creator['full_name'].split()) >= 2 and creator['full_name'].lower() in text.lower())
            proven = origin == 'biography' or (origin == 'public_page' and linked and identifies)
            role = 'representative' if REPRESENTATIVE.search(context) else 'creator'
            attribution = role if proven else 'unconfirmed'
            existing = self.store.all('contacts', 'creator_key=? AND contact_type=? AND contact_value=?', (creator['creator_key'], 'email', email))
            old = existing[0] if existing else None
            record = {'contact_key': old['contact_key'] if old else str(uuid.uuid4()), 'creator_key': creator['creator_key'],
                'contact_type': 'email', 'contact_value': email, 'recipient_role': role, 'source_url': source,
                'supporting_text': context, 'attribution_status': attribution, 'discovered_at': now()}
            if old and (old['review_status'] != 'discovered' or (old['attribution_status'] != 'unconfirmed' and attribution == 'unconfirmed')):
                continue
            self.store.put('contacts', record, 'contact_key')
            if not old:
                self.increment('contacts_found')

    def contacts(self, creator):
        self.save_emails(creator, creator['biography'], creator['profile_url'], 'biography', True)
        def has_email():
            return any(c['attribution_status'] in ('creator', 'representative') and c['review_status'] != 'rejected'
                       for c in self.store.all('contacts', 'creator_key=?', (creator['creator_key'],)))
        # A suitable saved or biography email makes further page collection unnecessary.
        if has_email():
            self.store.update('creators', 'creator_key', creator['creator_key'],
                              {'contact_lookup_status': 'contact_found', 'contact_lookup_at': now()})
            return
        queue = [(u, True) for u in unpack(creator['bio_links_json'], [])]
        visited, attempts, failures = set(), 0, 0

        def crawl():
            nonlocal attempts, failures
            while queue and attempts < 4:
                raw, linked = queue.pop(0)
                target = canonical(raw)
                if not target or target in visited:
                    continue
                visited.add(target)
                attempts += 1
                self.increment('public_page_requests')
                try:
                    status, headers, body = self.page_get(target)
                    if status in (301, 302, 303, 307, 308):
                        dest = canonical(url.urljoin(target, headers.get('location', '')))
                        if dest:
                            queue.insert(0, (dest, linked))
                        continue
                    if status != 200 or 'text/html' not in headers.get('content-type', ''):
                        raise ProviderError('Public page unavailable or not HTML.')
                    page = Page()
                    page.feed(body.decode('utf-8', errors='replace'))
                    text = page.text
                    # Include visible mailto links with their actual surrounding page text.
                    for href, label in page.links:
                        if href.lower().startswith('mailto:'):
                            address = url.unquote(href[7:].split('?')[0])
                            text += '\n'+label+' '+address
                    self.save_emails(creator, text, target, 'public_page', linked)
                    if has_email():
                        return
                    for href, label in page.links:
                        if BUSINESS.search(label+' '+href) and not re.search(r'privacy|terms|support|login|sign.in', label+' '+href, re.I):
                            dest = canonical(url.urljoin(target, href))
                            if dest and dest not in visited:
                                queue.append((dest, linked))
                except Exception as exc:
                    failures += 1
                    self.error('public_page', exc)

        crawl()
        found = self.store.all('contacts', 'creator_key=?', (creator['creator_key'],))
        usable = [c for c in found if c['attribution_status'] in ('creator', 'representative') and c['review_status'] != 'rejected']
        deferred = False
        if not usable:
            try:
                query = f'"{creator["username"]}" ("business enquiries" OR collaborations OR management)'
                data = self.api.search(engine='google', q=query, google_domain='google.co.uk', gl='uk', hl='en', location='United Kingdom')
                self.save_search(query, data, kind='contact_lookup')
                for item in data.get('organic_results') or []:
                    source = canonical(item.get('link'))
                    if not source:
                        continue
                    text = clean_text(item.get('title'))+' '+clean_text(item.get('snippet'))
                    self.save_emails(creator, text, source, 'search_snippet')
                    if BUSINESS.search(text):
                        queue.append((source, False))
                crawl()
            except QuotaStop:
                deferred = True
            except Exception as exc:
                failures += 1
                self.error('contact_search', exc)
        contacts = self.store.all('contacts', 'creator_key=?', (creator['creator_key'],))
        usable = [c for c in contacts if c['attribution_status'] in ('creator', 'representative') and c['review_status'] != 'rejected']
        status = 'contact_found' if usable else ('lookup_deferred_quota' if deferred else ('contact_needs_review' if contacts else ('retrieval_failed' if failures else 'no_contact_found')))
        self.store.update('creators', 'creator_key', creator['creator_key'], {'contact_lookup_status': status, 'contact_lookup_at': now()})

    def drafts(self, creator_keys=None):
        for creator in self.store.all('creators'):
            if creator_keys is not None and creator['creator_key'] not in creator_keys:
                continue
            if not contact_candidate(creator, self.config):
                continue
            # Read saved biography and post evidence on every drafting pass.
            posts = self.store.all('post_evidence', 'creator_key=?', (creator['creator_key'],))
            evidence = [e for e in unpack(creator['expertise_evidence'], []) if e.get('text') and e.get('url')]
            if not evidence:
                self.store.update('creators', 'creator_key', creator['creator_key'], {'missing_information': js(unpack(creator['missing_information'], []) + ['No relevant biography/caption evidence for personalisation'])})
                continue
            verified_posts = set(unpack(creator['review_json']).get('relevant_post_ids', []))
            preferred_urls = {p['post_url'] for p in posts if p['post_id'] in verified_posts}
            evidence.sort(key=lambda e: (e['url'] not in preferred_urls, e.get('kind') != 'biography'))
            reference = evidence[0]
            contacts = usable_contacts(self.store, creator['creator_key'])
            if contacts:
                for contact in contacts:
                    previous = self.store.all('outreach_drafts', 'creator_key=? AND contact_key=? AND channel=?',
                                              (creator['creator_key'], contact['contact_key'], 'email'))
                    self.save_draft(make_draft(creator, contact, reference, self.config), previous)
            elif creator['contact_lookup_status'] in LOOKUP_OUTCOMES:
                previous = self.store.all('outreach_drafts', 'creator_key=? AND channel=?',
                                          (creator['creator_key'], 'instagram_message'))
                self.save_draft(make_message_draft(creator, reference, self.config), previous)

    def save_draft(self, record, previous):
        if any(d['manually_edited'] or d['draft_status'] in ('approved', 'rejected') for d in previous):
            return
        record['draft_key'] = previous[0]['draft_key'] if previous else str(uuid.uuid4())
        record['created_at'] = previous[0]['created_at'] if previous else now()
        self.store.put('outreach_drafts', record, 'draft_key')
        if not previous:
            self.increment('drafts_created')


def make_draft(creator, contact, reference, config):
    # Exact contiguous excerpt; no invented expertise or claim to have watched a video.
    original = reference['text']
    words = list(re.finditer(r'\S+', original))
    excerpt = original[:words[min(14, len(words))-1].end()] if words else ''
    excerpt = excerpt.strip()
    focus = topics(reference['text'])
    focus = focus[0] if focus else 'life admin'
    frames = {
        'insurance': ('insurance', 'For followers considering insurance decisions, the admin around those decisions could be a useful topic to explore.'),
        'budgeting': ('household budgeting', 'For followers interested in managing household budgets, insurance admin could be a useful adjacent topic to explore.'),
        'household bills': ('household bills', 'For followers navigating household bills and contracts, insurance admin could be a relevant addition to that conversation.'),
        'home ownership': ('home ownership', 'For followers managing a home, the associated insurance admin could be a relevant topic to explore.'),
        'subscriptions': ('household subscriptions', 'For followers reviewing recurring household commitments, insurance admin could be another relevant area to consider.'),
        'life admin': ('household organisation', 'For followers interested in organising household paperwork, insurance admin could be a natural topic to explore.'),
        'parenting mental load': ('family life admin', 'For followers thinking about the mental load of household admin, managing insurance paperwork could be a relevant conversation.'),
    }
    theme, benefit = frames[focus]
    representative = contact['attribution_status'] == 'representative'
    creator_label = creator['full_name'] or '@'+creator['username']
    greeting = f'Hello {contact["recipient_name"]},' if contact['recipient_name'].strip() else ('Hello management team,' if representative else 'Hello,')
    subject = f'Nous collaboration enquiry — @{creator["username"]}'
    whose = f'{creator_label}’s' if representative else 'your'
    kind = 'bio' if reference['kind'] == 'biography' else 'caption'
    openings = {
        'insurance': f'I’m reaching out about {whose} insurance-related content.',
        'budgeting': f'I’m getting in touch about {whose} focus on household budgeting.',
        'life admin': f'I’m contacting you about {whose} approach to household organisation.',
    }
    opening = openings.get(focus, f'I’m reaching out about {whose} content around {theme}.')
    intro = f'{opening} The {kind} says: “{excerpt}”'
    brief = config['approvedProductBrief'].strip()
    facts = [brief]
    extra = ''
    if config['approvedProductFacts']:
        extra = ' '+config['approvedProductFacts'][0].strip()
        facts.append(config['approvedProductFacts'][0])
    partner = f'a possible paid partnership with {creator_label}' if representative else 'a possible paid partnership'
    body = (f'{greeting}\n\n{intro}\n\n{brief}{extra} {benefit}\n\n'
            f'We’d like to explore {partner}, with an approach that fits the content and offers something useful to followers. '
            'This would be an initial conversation, with the scope and terms to be discussed together. '
            'We’d welcome your thoughts on a format that would feel useful and appropriate for the content. '
            'Would you be open to discussing a collaboration?\n\n'
            f'Best,\n{config["senderName"] or "[Sender name]"}\n{config["senderRole"] or "[Sender role]"}, Nous')
    missing = []
    if not config['senderName']:
        missing.append('senderName')
    if not config['senderRole']:
        missing.append('senderRole')
    if contact['review_status'] not in ('human_confirmed', 'approved'):
        missing.append('Human confirmation of email attribution; deliverability is unknown')
    if not eligible(creator):
        missing.append('Provisional draft: creator qualification is pending; review before outreach')
    if creator['qualification_status'] == 'manually_approved':
        missing.append('Creator selected by manual approval; see qualification evidence gaps')
    count = len(body.split())
    if not 100 <= count <= 150:
        missing.append(f'Length review: {count} words (target 100–150)')
    return {'creator_key': creator['creator_key'], 'contact_key': contact['contact_key'], 'channel': 'email',
        'recipient_email': contact['contact_value'], 'subject': subject, 'draft_body': body,
        'expertise_reference': js({'topic': theme, 'exact_excerpt': excerpt, 'source_url': reference['url']}),
        'audience_benefit_rationale': benefit, 'evidence_urls_json': js(list(dict.fromkeys([reference['url'], creator['profile_url'], contact['source_url']]))),
        'product_facts_used_json': js(facts), 'draft_status': 'needs_review', 'missing_information': js(missing), 'updated_at': now()}


def make_message_draft(creator, reference, config):
    """A copyable, unsent Instagram message; never invent an email address."""
    template = make_draft(creator, {'attribution_status':'creator','recipient_name':'',
        'contact_key':None,'contact_value':'','review_status':'discovered','source_url':creator['profile_url']}, reference, config)
    evidence = unpack(template['expertise_reference'])
    original = reference['text']
    words = list(re.finditer(r'\S+', original))
    excerpt = original[:words[min(8,len(words))-1].end()].strip() if words else ''
    kind = 'bio' if reference['kind'] == 'biography' else 'caption'
    benefit = f"This could be relevant to followers interested in {evidence['topic']}."
    body = (f'Hi! Your {kind} says: “{excerpt}”\n\n'
            f'{config["approvedProductBrief"].strip()} {benefit} '
            'Would you be open to discussing a paid partnership, and which business email should I use?'
            f'\n\n{config["senderName"] or "[Sender name]"} — Nous')
    if len(body.split()) < 50:
        body = body.replace('Would you be open', 'We could discuss a format that suits your content. Would you be open')
    reason = {
        'no_contact_found':'No suitable public business email found',
        'contact_needs_review':'Email evidence is unconfirmed; use the message to ask for a business email',
        'retrieval_failed':'Email lookup failed; an email may still be available',
        'lookup_deferred_quota':'Email lookup was deferred by quota; an email may still be available',
        'contact_found':'No currently usable email remains; ask for the preferred business email'
    }[creator['contact_lookup_status']]
    missing = [reason]
    if not config['senderName']:
        missing.append('senderName')
    if not eligible(creator):
        missing.append('Provisional draft: creator qualification is pending; review before outreach')
    if creator['qualification_status'] == 'manually_approved':
        missing.append('Creator selected by manual approval; see qualification evidence gaps')
    count = len(body.split())
    if not 50 <= count <= 80:
        missing.append(f'Length review: {count} words (target 50–80)')
    evidence['exact_excerpt'] = excerpt
    template.update(contact_key=None, channel='instagram_message', recipient_email='', subject='',
        draft_body=body, expertise_reference=js(evidence), audience_benefit_rationale=benefit,
        product_facts_used_json=js([config['approvedProductBrief'].strip()]), missing_information=js(missing))
    return template


def review_creator(store, key, changes, config):
    creator = store.one('creators', 'creator_key', key)
    if not creator:
        raise ValueError('Creator not found.')
    allowed = {'manual_decision', 'reviewer_notes', 'review_json'}
    if set(changes) - allowed:
        raise ValueError('Unsupported review field.')
    if 'manual_decision' in changes and changes['manual_decision'] not in ('', 'approved', 'rejected'):
        raise ValueError('Invalid manual decision.')
    if changes.get('manual_decision') == 'approved' and not str(changes.get('reviewer_notes', creator['reviewer_notes'])).strip():
        raise ValueError('Add a short reason when manually approving a creator.')
    if 'review_json' in changes:
        review = changes['review_json']
        if not isinstance(review, dict) or set(review) - {'relevant_post_ids','latest_post_ids','chronological_evidence','uk_suitability','uk_evidence'}:
            raise ValueError('Invalid evidence review.')
        for field in ('relevant_post_ids', 'latest_post_ids'):
            if not isinstance(review.get(field, []), list) or any(not isinstance(x, str) for x in review.get(field, [])):
                raise ValueError('Post evidence must be a list of post IDs.')
        if review.get('uk_suitability') not in (True, False, None):
            raise ValueError('Invalid UK suitability decision.')
        changes['review_json'] = js(review)
    store.update('creators', 'creator_key', key, changes)
    return assess(store, store.one('creators', 'creator_key', key), config)


def review_contact(store, key, changes):
    if not store.one('contacts', 'contact_key', key):
        raise ValueError('Contact not found.')
    if set(changes) - {'review_status','attribution_status','recipient_name','recipient_role'}:
        raise ValueError('Unsupported contact field.')
    if changes.get('review_status', 'discovered') not in ('discovered','human_confirmed','approved','rejected'):
        raise ValueError('Invalid contact review state.')
    if changes.get('attribution_status', 'unconfirmed') not in ('creator','representative','unconfirmed'):
        raise ValueError('Invalid attribution.')
    store.update('contacts', 'contact_key', key, changes)


def edit_draft(store, key, changes):
    if not store.one('outreach_drafts', 'draft_key', key):
        raise ValueError('Draft not found.')
    if set(changes) - {'subject', 'draft_body', 'draft_status'}:
        raise ValueError('Unsupported draft field.')
    if changes.get('draft_status', 'needs_review') not in ('needs_review','approved','rejected'):
        raise ValueError('Invalid draft state.')
    changes.update(manually_edited=1, updated_at=now())
    store.update('outreach_drafts', 'draft_key', key, changes)
