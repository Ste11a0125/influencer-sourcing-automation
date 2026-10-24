"""Resolve explicit public Instagram post authors, never shortcodes or mentions."""
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin

RESERVED = set('p reel reels tv stories explore accounts popular about legal privacy terms direct share'.split())

def handle(value):
    value = str(value or '').strip().lstrip('@').lower()
    return value if re.fullmatch(r'[a-z0-9_](?:[a-z0-9_.]{0,28}[a-z0-9_])?',value) and value not in RESERVED else ''

def profile_handle(value):
    try:
        p=urlsplit(str(value or ''))
        if p.scheme not in ('http','https') or p.hostname not in ('instagram.com','www.instagram.com','m.instagram.com') or p.username or p.password:
            return ''
        parts=p.path.strip('/').split('/')
        return handle(parts[0]) if len(parts)==1 else ''
    except ValueError:
        return ''

def shortcode(value):
    try:
        p=urlsplit(str(value or ''))
        if p.scheme not in ('http','https') or p.hostname not in ('instagram.com','www.instagram.com','m.instagram.com') or p.username or p.password:
            return ''
        parts=p.path.strip('/').split('/')
        return parts[1] if len(parts)==2 and parts[0] in ('p','reel','tv') and re.fullmatch(r'[A-Za-z0-9_-]+',parts[1]) else ''
    except ValueError:
        return ''

def same_post(first,second):
    return bool(shortcode(first)) and shortcode(first)==shortcode(second)

class Metadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta={};self.authors=[];self.scripts=[];self.current=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='meta':
            self.meta[attrs.get('property') or attrs.get('name')]=attrs.get('content','')
        if tag in ('a','link') and 'author' in attrs.get('rel','').split():
            self.authors.append(attrs.get('href',''))
        if tag=='script' and attrs.get('type') in ('application/ld+json','application/json'):
            self.current=[]
    def handle_data(self,data):
        if self.current is not None:self.current.append(data)
    def handle_endtag(self,tag):
        if tag=='script' and self.current is not None:
            try:self.scripts.append(json.loads(''.join(self.current)))
            except (ValueError,RecursionError):pass
            self.current=None

def walk(value,depth=0):
    if depth>40:return
    if isinstance(value,dict):
        yield value
        for item in value.values():yield from walk(item,depth+1)
    elif isinstance(value,list):
        for item in value:yield from walk(item,depth+1)

def author_result(evidence):
    handles={e['username'] for e in evidence if e['username']}
    if len(handles)!=1:return '',evidence
    return next(iter(handles)),evidence

def from_html(html,post_url):
    """Only metadata/links explicitly tied to the requested post can establish ownership."""
    parser=Metadata();parser.feed(html)
    code=shortcode(post_url)
    if not code:return '',[]
    evidence=[]
    def add(name,method,text):
        name=handle(name)
        if name:evidence.append({'username':name,'method':method,'source_url':post_url,'supporting_text':text})
    for script in parser.scripts:
        for node in walk(script):
            # Instagram media objects identify the exact shortcode and its owner.
            if node.get('shortcode')==code or node.get('code')==code:
                for field in ('owner','user'):
                    owner=node.get(field)
                    if isinstance(owner,dict):add(owner.get('username'),'post_owner_metadata',f'{field}.username={owner.get("username")}; shortcode={code}')
            urls=[node.get('url'),node.get('@id'),node.get('mainEntityOfPage'),node.get('contentUrl')]
            urls=[u.get('@id') or u.get('url') if isinstance(u,dict) else u for u in urls]
            if any(same_post(u,post_url) for u in urls):
                authors=node.get('author',[])
                if not isinstance(authors,list):authors=[authors]
                for author in authors:
                    values=[author] if isinstance(author,str) else [author.get('url'),author.get('@id')] if isinstance(author,dict) else []
                    for value in values:
                        name=profile_handle(value)
                        if name:add(name,'linked_data_author',str(value))
    if same_post(parser.meta.get('og:url'),post_url):
        for href in parser.authors:
            absolute=urljoin(post_url,href)
            name=profile_handle(absolute)
            if name:add(name,'explicit_author_link',absolute)
        for key in ('og:title','og:description'):
            text=parser.meta.get(key,'')
            # Recognised author headers only; caption @mentions cannot become owners.
            match=re.match(r'^.*?\(@([A-Za-z0-9_.]+)\) on Instagram\s*:',text)
            if not match:match=re.match(r'^(?:[\d,.KM]+ likes,\s*[\d,.KM]+ comments\s*-\s*)?([A-Za-z0-9_.]+) on (?:Instagram\s*:|[A-Z][a-z]+ \d{1,2}, \d{4}\s*:)',text)
            if match:add(match.group(1),'post_author_header',text[:300])
    return author_result(evidence)

def from_search(item,post_url):
    """Only the exact post's structured Google author/source attribution is accepted."""
    if not isinstance(item,dict) or not same_post(item.get('link'),post_url):return '',[]
    evidence=[]
    author=item.get('author')
    values=[]
    if isinstance(author,dict):values=[author.get('url'),author.get('link')]
    elif isinstance(author,str):values=[author]
    for value in values:
        name=profile_handle(value)
        if not name and isinstance(value,str) and value.startswith('@'):name=handle(value)
        if name:evidence.append({'username':name,'method':'indexed_author','source_url':post_url,'supporting_text':str(value)})
    source=str(item.get('source') or '')
    match=re.fullmatch(r'Instagram\s*[·•]\s*@?([A-Za-z0-9_.]+)',source,re.I)
    if match and handle(match.group(1)):
        evidence.append({'username':handle(match.group(1)),'method':'indexed_source','source_url':post_url,'supporting_text':source})
    return author_result(evidence)
