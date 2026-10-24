# Nous UK creator discovery and outreach drafts

An independent, local fallback for the streamlined n8n workflow. Its outputs are selected creators, public business emails, and personalised email or Instagram-message drafts. No n8n service, paid AI API, Google Sheets, or external database is required. This implementation uses Python's built-in SQLite database on your Mac.

## Start

1. Double-click **Launch.command** in Finder, or run `python3 app.py serve` from this folder.
2. In **Settings**, enter your SerpApi key. It stays in process memory and disappears when the app stops. Do not paste it into chat or `config.json`.
3. Optionally supply your sender name and role and confirmed product facts. Without them, drafts keep sender placeholders and review flags.
4. Choose **Run full workflow**. One run checks up to the configured profile limit, assesses suitability, finds emails and creates outreach drafts automatically, using at most 15 search attempts. No intermediate approval is required to continue.
5. For existing leads, choose **Process saved creators**. It assesses saved evidence, looks up emails and drafts without repeating discovery. A usable email produces an email draft; otherwise the app creates a 50–80-word Instagram message asking for the preferred business email.
6. Review candidates in **Qualified creators**, including its Awaiting qualification section. Confirm post relevance, latest-20 coverage and UK suitability, or manually approve with a reason. Pending creators can have emails and provisional drafts; they remain clearly unqualified until reviewed. Review the saved email evidence, then read and edit the collaboration emails under **Emails** or **Instagram messages**. Export results with qualification flags as CSV when useful.

Python 3.10 or newer is required; no package installation is needed. Tested with Python 3.12 on this Mac. Keep the terminal open while using the app. Stop with Ctrl+C. Launch it again to reopen saved records. If port 8765 is busy, use `python3 app.py serve --port 8767`.

## What was cut

The revised scope removes weighted ranking, engagement and demographic analysis, contact-form harvesting and scheduled/background monitoring. It keeps evidence needed for selection and outreach drafting. Instagram-message fallback has been restored at your request. Once a suitable email exists in saved data or the biography, no additional contact search or website crawl is performed. A public-page crawl stops as soon as it finds an attributable business email.

The original six data categories remain in the local database to retain sources, human decisions and run history. Some compatibility fields are intentionally left unknown. These are bookkeeping, not additional collection operations.

## How qualification works

**Current setting: latest-20-post qualification is temporarily skipped.** With `requireLatest20Posts: false`, available relevant biography/post evidence, follower range and supported UK suitability are used. The latest-20 coverage check and five-post minimum below apply only when the Settings switch is on.

The criteria are an inclusive 10,000–100,000 follower count, at least five substantively relevant posts among the latest 20, and evidence of suitability for UK households. Keyword hits only identify material to review. The API does not establish latest-20 coverage, so this app does not automatically label a keyword match as a qualified creator.

In a creator's review panel, mark genuinely relevant captions, verify the latest-20 sample and enter its evidence, and record UK household suitability evidence. When the criteria are supported, the status becomes `qualified`. A manual approval instead produces `manually_approved`; gaps remain visible. Rejected creators are excluded from new drafting. Do not use creator location alone as evidence of audience geography.

Unknown engagement, audience demographics, credentials and partnerships stay unknown. No final weighted score is calculated. Candidates are ordered by topic evidence and completeness, purely to help review.

## Emails and drafts

Emails must be publicly listed alongside business/contact context. Biography emails can be attributed to the creator or management. Emails on public pages need a link from the creator's profile and identifying text; search-only contacts stay unconfirmed until reviewed. Support, privacy and vendor addresses are excluded. Email syntax does not establish deliverability.

The app follows at most four public-page requests per creator per contact run, including redirects. It follows relevant contact/management links only. A single targeted SerpApi search is allowed if an attributable email is still missing. JavaScript-only pages, blocked websites, images of email addresses and obfuscated addresses may require manual research; no login or access-control bypass is attempted.

Drafts use an exact excerpt from saved biography or caption evidence, topic-specific introductory and benefit wording, the approved product brief and optionally the first approved product fact. Drafts are approximately 100–150 words; unusually long names or custom claims are flagged if they exceed the target. They suggest a possible paid collaboration, without promising fees or terms. All generated drafts start as `needs_review`.

Unknown first names are not guessed. Management emails are addressed to the representative when their name is supplied, otherwise to the management team, and name the creator. Pending creators receive provisional drafts marked `needs_review`, with qualification gaps retained. CSV exports include a `qualification_pending` column. When lookup finds no usable email, fails, reaches quota, or leaves attribution unconfirmed, the app creates an Instagram-message draft instead. The reason is recorded honestly: a failed lookup does not prove an email is unavailable. Unattempted lookups do not create premature fallbacks. Messages use saved evidence and ask about collaboration and the preferred business email. If an attributable email appears later, its email draft becomes current; earlier message drafts remain preserved but leave the current export. Edited, approved or rejected drafts are retained during reruns. If their creator/contact later becomes ineligible, they remain visible as historical drafts and are excluded from export.

## Run modes

- **discovery**: Google search → save results → resolve post/reel authors → enrich profiles → reassess → contact lookup for promising in-range or approved creators → drafts.
- **contact_lookup**: use saved promising in-range or approved creators, check their biographies/public links and one fallback search if needed, then draft. No discovery or profile requests.
- **draft_generation**: reassess stored evidence and generate drafts from saved contacts. No network requests.
- **replay**: reassess stored evidence and regenerate unprotected drafts. No network requests of any kind.

The UI exposes these as Run full workflow, Process saved creators, Generate from saved data, and Replay saved evidence. The full workflow performs all stages automatically after one click. Live progress reports the current stage. Drafts are saved after each creator so they survive later failures. A search-cap stop during discovery still allows processing of already-saved candidates and fallback drafting. In a terminal, `python3 app.py run replay` runs the offline path and `python3 app.py status` shows stored counts. Collection commands can read an existing `SERPAPI_KEY` environment variable, but the in-app memory-only key field is simpler and avoids putting a key in shell history.

## Cost and independence

Before collection and before each search attempt, the Account API must confirm an active zero-price plan and positive free monthly/hourly allowance. Missing or unfamiliar fields stop collection. Extra purchased credits are not used as a substitute for free allowance. Retries and pagination share the 15-search-attempt cap. Transient network/5xx failures receive at most one retry; rate-limit responses stop the affected collection. Account checks are free and tracked separately; they do not consume search allowance. A local request ledger conservatively limits repeated runs.

If n8n uses the same SerpApi account, both consume the same free allowance. Do not run their collection stages concurrently: the two platforms cannot atomically reserve a shared account balance. The fallback checks current account usage, but cannot coordinate n8n's in-flight requests. It has an independent database and does not automatically import or synchronise n8n Data Tables. It also shares SerpApi as a provider, so it is a fallback for n8n failure, not a replacement for an unavailable SerpApi service.

The app runs only while you start it on your Mac. It is not a cloud-hosted or scheduled service. Email and Instagram-message text drafts are saved locally. No emails or Instagram messages are sent, no contact forms are submitted, and no Gmail drafts are created. Use Copy message and Open Instagram profile to send manually.

## Files and recovery

- `app.py`: local browser UI server and command-line entry point.
- `workflow.py`: collection, qualification, evidence storage, contact extraction and drafting.
- `config.json`: editable non-secret campaign settings.
- `data/nous.sqlite3`: persistent local records. Back up the entire `data` directory while the app is stopped.
- `index.html`: browser review interface; no external scripts or analytics.
- `tests/`: synthetic workflow and local HTTP integration checks.

SQLite keys prevent duplicate search results, creator IDs, posts and contacts. Username records retain stable internal creator keys when Instagram IDs arrive. If two known records resolve to one ID, evidence and human decisions are reconciled; conflicting decisions are noted and require review. Manually edited drafts are preserved.

The server binds only to `127.0.0.1`, requires a session cookie, rejects cross-origin write requests, and does not log keys or request URLs. Public-page fetches reject private addresses and validate each redirect. It cannot send outreach because it has no sending integration.

## Validation completed

On 19 October 2026, **51 automated tests passed** after the full-pipeline update: 44 workflow tests and seven real localhost HTTP tests. The new checks cover a complete discovery-to-email/message run without approvals, Instagram fallback length and evidence, preserved message edits, message deduplication, email supersession, CSV channel selection, quota continuation, pending-candidate research, rejection/range exclusions, offline outcomes and promotion without duplicate drafts. Tests use invented fixtures in temporary databases, never live creators. They cover save-before-enrichment commits; duplicate-free reruns; reserved Instagram URL paths; missing/empty provider results; follower boundaries; reviewed qualification; identity reconciliation; preserved reviewer decisions and edited drafts; biography and management email attribution; exclusion of unrelated addresses; unconfirmed search snippets; four-page caps; early stopping on email discovery; distinct evidence-based templates; no-address behavior; offline replay; bounded retries; free-plan/monthly/hourly guards; shared cap enforcement; credential handling; private-address blocking; authenticated local UI access; cross-origin write rejection; configuration validation; and CSV export.

The browser interface was also opened and visually inspected. Clicking Run full workflow without a key correctly opens Settings before collection.

**Live evidence and limits:** The saved live runs contain six candidate records, five enriched profiles, and one attributed business email with an email draft. Two candidate email lookups failed. The updated fallback was run against this saved evidence offline and created two Instagram-message drafts, with zero external requests. The browser now shows one email draft and two Instagram-message drafts, each with its evidence and review flags. The full updated discovery-to-drafting path is tested with synthetic responses; a fresh live end-to-end run, email deliverability, real final creator qualification and concurrent n8n usage remain unverified. Re-enter the session key after the app restarts to run new collection.

Run the tests with `python3 -m unittest discover -s tests -v`. The HTTP tests need permission to bind a temporary localhost port.

## Provider documentation checked

Implementation field mapping was checked against SerpApi's [Instagram Profile API](https://serpapi.com/instagram-profile-api), [Account API](https://serpapi.com/account-api), and [Google Search API](https://serpapi.com/search-api). In particular, the Instagram adapter reads `profile_results.posts[].media_captions` and uses `serpapi_pagination.next_page_token` for a single additional page.

## Contact-research update

Public-email research now runs for creators with numeric in-range followers and relevant saved biography/caption evidence, even when latest-20 coverage or UK suitability remains pending. Explicit rejection still blocks research; existing manual approvals continue to act as an override. Final qualification criteria are unchanged. No-candidate runs now say `no_candidates` and explain skipped reasons instead of implying email searches succeeded. Previously saved results and human edits are preserved.

## Post and reel discovery update

Discovery now follows post and reel hits to their creator using explicit public author metadata, author links or exact-post indexed author/source attribution. Caption mentions and shortcodes never become guessed handles. Conflicting attribution stays unresolved. Topic pages (such as /popular/) are not individual posts or profiles. Author evidence is retained in the saved search-results database.

Each run resolves at most ten unique posts (also bounded by the profile setting), with at most two public GET attempts per post including redirects. Login/unrelated redirects are not followed. If needed, one exact-post Google lookup is allowed per attempted post, at most three per run and within the shared search cap, reserving at least two attempts before starting a fallback for profile collection. Profile checks, retries, pagination and email research still share the configured total cap.

A full workflow run processes creators from its current discovery results. Older creators stay in the shortlist; Process saved creators continues those leads. Saving queries applies to the next full run and does not initiate a search. Run history records query snapshots, discovery counts, resolved authors and post-page request counts.

Validation on 19 October: all 63 tests passed (44 existing workflow tests, 12 post-author and discovery-scope tests, seven localhost HTTP tests). New tests cover post-only discovery through email drafting, cached attribution, author-versus-tagged-user discrimination, conflicting metadata, exact-post fallback, login redirects, request limits and isolation from historical query results. Live Instagram access remains dependent on the public response; this does not guarantee every post can be resolved.

A live public-page check of the saved post `DblTBifMwxk` returned HTTP 200 and resolved its author as `britishembassymorocco`. This confirms public author extraction on one real result, not final qualification or a fresh end-to-end collection run. The live check did not modify the saved result. Re-enter the memory-only SerpApi key and run the workflow to process current search results with the new resolver.

The Qualified creators view sorts by most recent discovery-result timestamp, with profile-update time as a fallback and tie-breaker. Each card shows Last found (or Profile updated where no discovery timestamp is available).

## Related keyword suggestions

Settings now shows related keywords immediately below Discovery queries and updates them while typing. Local topic rules cover pet, travel, car, home, life and health insurance, budgeting, energy, broadband and household admin. Other topics receive simple broader/review/tips variations. These are suggestions, not measured search-volume or popularity data; generating them uses no API calls. Click a term to append its query, retaining the source site's filter and UK restriction when present. Duplicate queries and additions beyond five are blocked. Save settings, then Run full workflow to search. Existing queries are not automatically replaced.

Verified query expansion, context preservation, empty input, duplicates, the five-query cap and UI syntax with `node tests/test_keywords.js`. Browser checks confirmed adding a suggestion and live switching between pet and travel topics; original saved settings were preserved.

## Three-page results interface

The current interface has exactly three result tabs: Qualified creators, Emails, and Instagram messages. Settings is a fourth full-page tab alongside the three result tabs, with the existing queries and related-keyword suggestions. The former Search results, Run history and combined Outreach drafts tabs are removed; saved evidence and historical records remain in the database. A compact Latest run breakdown explains raw hits, checked profiles, exclusions and quota limits.

Qualified creators lists only fully qualified or manually approved profiles, newest discovery first. A separate Awaiting qualification section on that same page keeps promising preliminary candidates reviewable and explicitly excludes them from the qualified total. Emails places each usable public address next to its editable current collaboration draft; unconfirmed addresses have a collapsed attribution-review section. Instagram messages contains only current fallback drafts for creators without usable emails; superseded, rejected and inactive drafts are excluded from those current views. Existing review controls and manual edit protection remain.

The reported run with 48 discovery hits resolved 14 distinct profile candidates. Eight profiles were checked: seven outside the follower range and one eligible for preliminary contact research. Six had no follower data when the shared 15-attempt cap was reached. The seven research candidates shown previously were the accumulated shortlist across runs, not seven fully qualified accounts. At the time of this UI update there were 36 stored profiles, zero fully qualified/approved, seven preliminary research candidates, one usable email and six current Instagram messages.

Verified with `node tests/test_pages.js` and `node tests/test_keywords.js`, plus live browser checks of all three result tabs and Settings. No collection requests or saved review/data mutations were needed for this update.

## Settings page guide

Settings is restored as a full navigation page. Each section and input explains what it affects: Connect SerpApi handles the memory-only collection key; Sender & approved claims controls signatures and permitted draft wording; Collection limits explains the inclusive follower range, profile limit and shared request cap; Discovery queries & related keywords controls future full searches and locally generated suggestions; Qualification & outreach explains reviewed qualification versus provisional research and email/message routing; Save settings & choose what runs distinguishes saving, full discovery, stored-creator research and offline drafting. Saving settings alone does not start collection or regenerate drafts. The page retains the existing configuration and active key.

## Temporarily skip latest-20 qualification

The Settings → Qualification & outreach switch “Require review of the latest 20 posts” is now off. This waives chronological coverage and the minimum five relevant posts; available biography/post relevance, configured follower limits and supported UK suitability remain. Manual decisions remain authoritative. The profile response's existing posts are retained for evidence and drafting, but an extra profile page is not requested merely to fill the 20-post sample. Older configs without this switch retain strict behavior. Previous chronology reviews remain saved so the requirement can be restored later. Save the switch and run offline Generate from saved data or a workflow to reassess.

All 69 Python tests and both UI checks passed. Saved profiles were reassessed offline with zero external requests, and all latest-20/five-post blocker messages were removed. No saved profiles became fully qualified because remaining criteria, including UK suitability evidence, were still incomplete.

## Competitor and account exclusions

Settings → Competitors & excluded accounts contains exact Instagram handles or profile URLs, one per line (up to 200). Aviva UK (`avivauk`) and Utility Warehouse (`welcometouw`) are excluded as requested. Handles are normalised and deduplicated. Matching excludes the account, not other creators discussing that brand; different brand handles must be added individually.

Once an excluded author is identified, the full workflow saves discovery evidence but skips its profile request, email research and drafts. Exclusions override previous manual approvals without erasing them. Existing emails and drafts leave active pages and export while saved evidence/manual edits remain preserved. Removing an entry allows normal screening again. Saving Settings now reapplies screening and exclusion rules to stored profiles immediately without network calls or draft rewriting.

76 Python tests pass, including exclusion normalisation, exact-account matching, precedence over approvals, request skipping, preserved drafts, re-inclusion and immediate HTTP settings behavior. Both frontend checks passed. The live saved Aviva and Utility Warehouse records were marked excluded; current Instagram messages reduced from six to four, with their earlier drafts preserved.

## Automatic competitor-bio qualification check

Bios that present competing provider services receive `flagged_competitor` and cannot qualify, receive email research, or generate current outreach drafts, even with a previous manual approval. Explicit account exclusions retain their separate status and can also carry bio flags. The local rule screen looks for insurance brokers/agencies/providers, utility suppliers, services managing customers' bills/admin, bundled household services, direct insurance offerings and corporate financial-service catalogues. Advice/review/personal-experience wording alone is not enough; wording rules are conservative and cannot identify every competitor or guarantee no false positives.

Exact supporting bio text, rule reason and source profile URL are saved under screening_results.competitor_bio_flags. The Qualified creators page contains a separate red-flag section showing this evidence. No generic approval button overrides a red flag. New profile collection checks the biography before requesting extra post pages; subsequent email/draft stages and exports apply the same exclusion. Previously saved drafts and decisions remain stored. Flags are recalculated from the saved bio and can clear after a refreshed bio no longer matches. No paid AI or extra API requests are used for classification.

All 82 Python tests and both frontend checks passed, including provider/creator discrimination, evidence attribution, approval precedence, absence of extra pagination/email requests, preserved drafts, refresh behavior and escaped red-flag rendering. Existing profiles were reassessed offline: Aviva UK, Utility Warehouse, Insurd and FJP Insurance Agency show bio red flags. Latest-20 qualification remains skipped.
