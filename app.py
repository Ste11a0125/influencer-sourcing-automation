#!/usr/bin/env python3
"""Local review app and CLI. No third-party dependencies."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from workflow import (ROOT, TABLES, Store, Workflow, validate_config, review_creator,
                      review_contact, edit_draft, ProviderError, unpack, contact_candidate, eligible, current_draft, assess)


def load_config(path):
    return validate_config(json.loads(Path(path).read_text()))


def serve(db_path, config_path, port, open_browser):
    token = secrets.token_urlsafe(32)
    memory = {'key': os.environ.get('SERPAPI_KEY', ''), 'running': False, 'last_run': None}
    state_lock = threading.Lock()
    origin = f'http://127.0.0.1:{port}'
    Store(db_path).close()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Request paths may contain the one-time browser session token.

        def reply(self, value, status=200, content_type='application/json', cookie=False):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type+'; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if cookie:
                self.send_header('Set-Cookie', f'nous_session={token}; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            self.wfile.write(data)

        def authorised(self):
            if self.headers.get('Host') != f'127.0.0.1:{port}':
                return False
            cookies = dict(part.strip().split('=',1) for part in self.headers.get('Cookie','').split(';') if '=' in part)
            return secrets.compare_digest(cookies.get('nous_session', ''), token)

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{port}':
                return self.reply({'error':'Invalid host'},403)
            # Token is consumed into an HttpOnly cookie, then removed from browser URL.
            if self.path == '/start/'+token:
                self.send_response(302)
                self.send_header('Set-Cookie', f'nous_session={token}; HttpOnly; SameSite=Strict; Path=/')
                self.send_header('Location', '/')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                return
            if not self.authorised():
                return self.reply({'error':'Open the session link printed by the launcher.'},403)
            path = urlsplit(self.path).path
            if path == '/':
                return self.reply((ROOT/'index.html').read_bytes(), content_type='text/html')
            if path == '/api/state':
                store = Store(db_path)
                try:
                    tables = {name: store.all(name) for name in TABLES}
                    config = load_config(config_path)
                    for creator in tables['creators']:
                        creator['contact_candidate'] = contact_candidate(creator, config)
                        creator['qualification_pending'] = creator['contact_candidate'] and not eligible(creator)
                    for draft in tables['outreach_drafts']:
                        draft['is_current'] = current_draft(store, draft, config)
                    tables['run_log'].sort(key=lambda r:r['started_at'], reverse=True)
                    return self.reply({'tables':tables, 'config':load_config(config_path), 'key_present':bool(memory['key']),
                                       'running':memory['running'], 'last_run':memory['last_run']})
                finally:
                    store.close()
            if path == '/api/export':
                store = Store(db_path)
                try:
                    output = io.StringIO()
                    writer = csv.writer(output)
                    writer.writerow(['username','qualification','qualification_pending','followers','channel','instagram_url','email','email_review','subject','draft_body','draft_status','evidence_urls','missing_information'])
                    for draft in store.all('outreach_drafts'):
                        c = store.one('creators','creator_key',draft['creator_key'])
                        contact = store.one('contacts','contact_key',draft['contact_key'])
                        # Include provisional candidates, with explicit qualification flags.
                        if not current_draft(store,draft,load_config(config_path)):
                            continue
                        row = [c['username'],c['qualification_status'],not eligible(c),c['followers'],draft['channel'],c['profile_url'],draft['recipient_email'],contact['review_status'] if contact else '',draft['subject'],draft['draft_body'],draft['draft_status'],draft['evidence_urls_json'],draft['missing_information']]
                        writer.writerow([("'"+str(x) if str(x).startswith(('=','+','-','@')) else x) for x in row])
                    return self.reply(output.getvalue().encode('utf-8-sig'),content_type='text/csv')
                finally:
                    store.close()
            self.reply({'error':'Not found'},404)

        def do_POST(self):
            if not self.authorised() or self.headers.get('Origin') != origin or self.headers.get('X-Nous-Request') != 'local-review':
                return self.reply({'error':'Invalid local session'},403)
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 100000:
                    raise ValueError('Request is too large or empty.')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('Expected a JSON object.')
                path = urlsplit(self.path).path
                with state_lock:
                    if memory['running']:
                        return self.reply({'error':'A run is active. Wait before editing or starting another run.'},409)
                    if path == '/api/key':
                        memory['key'] = str(data.get('key','')).strip()
                        return self.reply({'ok':True})
                    if path == '/api/config':
                        c = validate_config(data)
                        temporary = Path(str(config_path)+'.tmp')
                        temporary.write_text(json.dumps(c,indent=2)+'\n')
                        os.replace(temporary,config_path)
                        # Apply saved screening/exclusion rules locally; no collection or draft rewriting.
                        s = Store(db_path)
                        try:
                            for creator in s.all('creators'):
                                assess(s, creator, c)
                        finally:
                            s.close()
                        return self.reply({'ok':True})
                    if path == '/api/run':
                        mode = data.get('mode')
                        if mode not in ('discovery','replay','contact_lookup','draft_generation'):
                            raise ValueError('Unknown run mode.')
                        config = load_config(config_path)
                        memory['running'] = True
                        key = memory['key']
                        def work():
                            s = Store(db_path)
                            try:
                                result = Workflow(s,config,key).run(mode)
                                memory['last_run'] = result
                            except Exception as exc:
                                memory['last_run'] = {'status':'failed','errors':str(exc) if isinstance(exc,ProviderError) else type(exc).__name__}
                            finally:
                                s.close()
                                memory['running'] = False
                        threading.Thread(target=work,daemon=True).start()
                        return self.reply({'ok':True})
                    store = Store(db_path)
                    try:
                        if path == '/api/creator':
                            review_creator(store,data['key'],data['changes'],load_config(config_path))
                        elif path == '/api/contact':
                            review_contact(store,data['key'],data['changes'])
                        elif path == '/api/draft':
                            edit_draft(store,data['key'],data['changes'])
                        else:
                            return self.reply({'error':'Not found'},404)
                    finally:
                        store.close()
                self.reply({'ok':True})
            except (ValueError,KeyError,TypeError) as exc:
                self.reply({'error':str(exc) if isinstance(exc,ValueError) else 'Invalid request fields.'},400)
            except Exception:
                self.reply({'error':'Operation failed; no credentials have been logged.'},500)

    server = ThreadingHTTPServer(('127.0.0.1',port),Handler)
    entry = origin+'/start/'+token
    print('Nous local workflow is ready. This app only saves drafts.',flush=True)
    print('Open this local session: '+entry,flush=True)
    print('Keep this terminal open. Press Ctrl+C to stop.',flush=True)
    if open_browser:
        webbrowser.open(entry)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped. Saved records remain in the local database.')
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description='Nous local influencer discovery and collaboration drafts')
    parser.add_argument('--db',default=str(ROOT/'data'/'nous.sqlite3'))
    parser.add_argument('--config',default=str(ROOT/'config.json'))
    sub = parser.add_subparsers(dest='command',required=True)
    ui = sub.add_parser('serve')
    ui.add_argument('--port',type=int,default=8765)
    ui.add_argument('--no-open',action='store_true')
    run = sub.add_parser('run')
    run.add_argument('mode',choices=['discovery','replay','contact_lookup','draft_generation'])
    sub.add_parser('status')
    args = parser.parse_args()
    if args.command == 'serve':
        serve(args.db,args.config,args.port,not args.no_open)
    else:
        s = Store(args.db)
        try:
            if args.command == 'run':
                result = Workflow(s,load_config(args.config),os.environ.get('SERPAPI_KEY','')).run(args.mode)
                print(json.dumps(result,indent=2))
                return 0 if result['status']=='complete' else 1
            print(json.dumps({name:len(s.all(name)) for name in TABLES},indent=2))
        finally:
            s.close()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
