# Restaurant Chatwoot Agent Bot

A FastAPI service that answers restaurant customers in Chatwoot, hands off to humans safely, and gives staff two control surfaces:

| Surface | Path | Runs where | Auth |
|---|---|---|---|
| **Bot** | `POST /webhooks/chatwoot` | Called by Chatwoot | HMAC signature |
| **Dashboard App** | `/app-panel/` | iframe tab inside a Chatwoot conversation | Bearer token from a one-time link (no cookies) |
| **Admin site** | `/admin/` | Its own browser tab | Cookie session + CSRF + roles |
| Copilot API | `POST /copilot/translate` | Used by the panel and admin | Panel bearer or admin session, plus CSRF |
| Health | `GET /health` | Public | none; returns only `{"status":"ok"}` |

## How the bot behaves

1. Chatwoot posts `message_created`. The signature is checked against the raw body. Only `incoming` messages from a **contact** are processed; agent, bot and private-note messages are ignored, so the bot can't reply to itself. The endpoint returns 200 straight away and does the work in a background task.
2. The bot replies only while the Chatwoot conversation status is **`pending`**. Once a human owns it (`open`, `snoozed` or `resolved`), the bot stays silent.
3. **Before the model runs**, these hand off straight away:
   - refund, complaint, allergy, food-poisoning or legal keywords;
   - "I want a human";
   - more than 6 bot turns;
   - the spend cap has been reached.
4. **Answering**: BM25 search finds the top 4 knowledge chunks. The model gets those chunks, the last 10 messages and the contact profile, and may call four tools:
   - `menu_lookup`
   - `order_status`
   - `delivery_check`
   - `opening_status`

   It must return JSON with a confidence score.
5. **After the answer**: the bot hands off on low confidence or "needs a human". It also hands off if the reply contains a ₦/NGN amount that no tool returned in that turn, which catches invented prices. Amounts written in knowledge documents are deliberately not trusted, because documents such as an old PDF menu go out of date.
6. **Handoff**:
   - the customer gets a holding message;
   - the status is set to `open`;
   - a private note is posted with a 3-line summary, the customer's question and the escalation reason;
   - a second private note carries a **suggested reply** drafted from the knowledge base.
7. When a human sets the conversation back to **pending**, the bot resumes with its turn count reset.

**Global switch** (Overview page):
- **ON**: normal.
- **PAUSED**: the bot does nothing, and conversations stay pending.
- **OFF**: no answers and no LLM calls. Each new message's conversation is set to `open` so humans get it.

This reconciles two lines of the spec: "do nothing" in the webhook section and "OFF sends all new conversations to humans" on the Overview page.

## Roles and permissions (RBAC)

Every user has one **role**, and a role is a list of permissions. Permissions are checked on the server for every page and action, and changes take effect on the user's next click.

**Permissions** come in the form `<area>.<action>`:
- **Areas:** Overview, Bot switch, Knowledge, Menu, Delivery zones, Hours, Customer data, Playground, Analytics, Settings, Users, Roles, Audit log, Chatwoot panel.
- **Actions:** *View*, *Create & edit*, *Delete*; *Use* for the Playground; *Act on conversations* for the Chatwoot panel (take over, send, draft, edit memory). Edit and Delete include View.

**Roles you start with:**

| Role | Can do |
|---|---|
| `admin` | Everything, including permissions added in future versions. Built-in: can't be edited or deleted. |
| `staff` | Knowledge, Menu and Hours (view, edit, delete), Analytics, Overview, and the Chatwoot panel |
| `agent` | Overview and the Chatwoot panel only, for Chatwoot agents |

To add a role, open **Roles**, tick the boxes and save, then assign it on **Users**. A role can't be deleted while anyone still has it.

**Safeguards against giving yourself more access:**
- You can only put permissions you hold yourself into a role.
- You can only assign roles whose permissions you hold.
- You can only manage users (reset password, disable, delete, change role) whose access doesn't exceed yours. Otherwise, for example, someone could issue a password reset for an admin and take over that account.
- You can't change, disable or delete your own account from **Users**.
- The last active admin can't be demoted, disabled or deleted.
- Every role and user change is in the audit log.

## Deleting things

| What | Where | What happens |
|---|---|---|
| Knowledge file | Knowledge → **Delete** | The bot stops using it immediately. Its versions are kept: **Restore** brings it back; **Delete forever** removes the history. |
| Menu item | Menu → **Delete** | Removed; an uploaded photo file is deleted too. Replaced photos are also cleaned up. |
| Delivery zone / date override | Zones / Hours → **Delete** / **Remove** | Removed immediately. |
| User's password | Users → **Set password** (type one or generate one) or **Reset link** | Signs them out everywhere; optionally makes them choose a new one at next login. |
| User | Users → **Delete** | Access is removed at once, along with their sessions. The audit log keeps their history. |
| Role | Roles → **Delete** | Only when no user has it. |
| Customer data | Customer data → customer → **Delete memory only** or **Delete all stored data** | Memory only: name, phone, preferences, notes, orders and summaries. All: also the bot's stored transcripts, bot decisions and conversation records. The audit log records counts only, not the erased data. Chatwoot keeps its own copy, so delete it there too for a full erasure request. |

Every delete asks for confirmation and is written to the audit log. The audit log itself can't be edited or deleted.

## Testing the copilot without Chatwoot: Simulate handoff

On **Playground → Simulate handoff** (admin only):

1. Write a short conversation, one message per line, starting with `Customer:` or `Bot:` (`C:` / `B:` also work). The last line must be from the customer.

   ```
   Customer: Hi, do you deliver to Lekki?
   Bot: Yes, we deliver to Lekki Phase 1.
   Customer: My order came cold and I want a refund
   ```
2. Optionally add a customer name and phone; the phone is used for order lookups.
3. Choose **Use the bot's real rules**, or **Always show a handoff** to see the copilot output even when the bot would have answered by itself.

The result shows exactly what would happen in Chatwoot:
- the message to the customer;
- the status change to open;
- the private summary note (3 lines, the customer's question and the escalation reason, with 🚩 for refund, allergy or complaint);
- the suggested-reply note.

A **Why** section shows the knowledge sections and tool calls used.

It runs the same code as the live bot, so the same keywords, turn limit (counted from the `Bot:` lines), confidence threshold, price check and spend cap apply. Nothing is sent to Chatwoot. The AI calls are real and count toward the daily cap.

## AI provider: OpenAI or Gemini

The bot works with **OpenAI** or **Google Gemini**. Pick one with `LLM_PROVIDER` (`openai` or `gemini`) and set that provider's key: `OPENAI_API_KEY`, or `GEMINI_API_KEY` from https://aistudio.google.com/apikey.

You can switch later on **Settings → AI provider** without redeploying, as long as both keys are set in Coolify. The page shows which keys are present. Leave **Model** empty to use the provider default:
- OpenAI: `gpt-4o-mini`
- Gemini: `gemini-3.5-flash-lite`, Google's recommended fast, low-cost model for new projects. Avoid `gemini-2.5-flash` and `gemini-2.5-pro`: Google shuts them down on 1 December 2026.

Both providers use the same tools, the same rules (prices only from the menu table), the same price check and the same spend cap. Gemini's thinking tokens are counted as output in the cost.

## Knowledge documents and suggested replies

The bot and Copilot answer from the Markdown files on **Knowledge** (`/admin/knowledge`). You can write them there, or **import a document**:

1. On the Knowledge page, choose a PDF, Word (`.docx`), `.txt` or `.md` file (max 10 MB). Optionally set "Save as" (e.g. `faq.md`); by default the name comes from the uploaded file.
2. The file is converted to Markdown and opened in the editor with a live preview. **Nothing is live yet.**
3. Check the text and fix any headings, lists or tables the conversion got wrong, then click **Save version**. The file is versioned like any other, can be rolled back, and the search index is rebuilt.

How conversion works:
- **Word:** headings become Markdown headings, bullet lists become lists, and tables become Markdown tables.
- **PDF:** text is extracted and re-flowed into paragraphs. Column layouts and tables can't be fully recovered, so the editor shows a warning.
- **FAQ questions:** any short line or paragraph ending in "?" becomes its own heading. Each question and its answer is then a separate search chunk, which makes it easier for the bot to find the right answer.
- **Not supported:** scanned PDFs (images only, no OCR), old `.doc` files (save as `.docx`), password-protected PDFs, and other Office formats.
- **Safety:** the file type is checked from its contents, not its name. Word files are checked for zip bombs, PDFs are limited to 300 pages, and the uploaded file itself is never stored; only the converted text is kept after you save.
- **Prices:** amounts in documents are ignored for quoting. Keep prices in **Menu** and fees in **Delivery zones**.

Suggested replies for agents come from the same knowledge and tools:
- a private "💡 Suggested reply" note on every handoff;
- **Generate draft** in the Dashboard App, which the agent can edit, translate, then send under their own name or copy.

No suggestion is offered when the knowledge doesn't cover the question, or when the draft contains a price that no tool returned.

## Project tree

```
app/
  main.py               app factory (uvicorn app.main:create_app --factory), bootstrap, error handlers
  config.py             pydantic-settings
  db.py, models.py      SQLAlchemy 2 (Postgres in prod, SQLite in dev/tests)
  deps.py               auth dependencies (permission checks per route), CSRF checks
  templating.py         Jinja2 (autoescape on)
  logging_setup.py      JSON logs; secret-looking fields masked
  security/
    signature.py        webhook HMAC + timestamp check
    permissions.py      RBAC permission catalogue and starter roles
    sessions.py         server-side sessions (web cookie / panel bearer), 30 min idle, 12 h absolute
    passwords.py        argon2id
    crypto.py           Fernet encryption of agents' Chatwoot tokens
    ratelimit.py        login lockout (DB) + in-process sliding window
    middleware.py       CSP / X-Frame-Options / HSTS, admin IP allow-list, request log
  services/
    bot.py              webhook ingest, filtering, reply pipeline
    answer.py           retrieval + tools + LLM + invented-price guard
    handoff.py          handoff rules and the handoff action
    copilot.py          summaries, suggested replies, translation
    memory.py           contact memory + summaries at close
    tools.py            menu / order / delivery / opening tools (+ ToolCallLog)
    retriever.py        Retriever interface + BM25
    knowledge.py        knowledge files, versions, sanitised Markdown preview
    importer.py         PDF / DOCX / TXT / MD upload -> Markdown (with safety checks)
    simulate.py         Playground "Simulate handoff" (same code path as the live bot)
    hours.py            opening status with overnight spans and date overrides
    chatwoot.py         Chatwoot API client
    spend.py            LLM usage, cost, daily cap
    settings_store.py   runtime settings (DB) with env defaults
    audit.py            audit log writer
    rbac.py             roles, permission lookup, anti-escalation rules
    llm/                LLMProvider interface, OpenAI + Gemini adapters, fake provider, pricing
  routers/              webhooks, panel, admin_auth, admin_pages, admin_contacts, copilot, health
  templates/            admin/*, panel/*
  static/               panel.js (postMessage + auth), admin.js, app.css
knowledge/*.md          placeholder knowledge (copied into the volume on first boot)
tests/                  pytest suite (132 tests)
Dockerfile, docker-entrypoint.sh, docker-compose.yml, .env.example
```

## Local setup

```powershell
python -m venv .venv
```
```powershell
.venv\Scripts\pip install -r requirements-dev.txt
```
```powershell
Copy-Item .env.example .env
```

Edit `.env` and set:
- `APP_ENV=development`
- `COOKIE_SECURE=false`
- `SESSION_SECRET`
- `DASHBOARD_ADMIN_EMAIL` and `DASHBOARD_ADMIN_PASSWORD`
- `LLM_PROVIDER=openai` with `OPENAI_API_KEY`, or `LLM_PROVIDER=gemini` with `GEMINI_API_KEY`, or `LLM_PROVIDER=fake` to work offline
- optionally `SEED_DEMO_DATA=true` for a sample menu, zones and hours

Then run:

```powershell
.venv\Scripts\uvicorn app.main:create_app --factory --reload
```

Open http://localhost:8000/admin/ and log in. You'll be asked to change the password. The **Playground** page tests the bot without Chatwoot.

To exercise the webhook locally, expose the port with a tunnel and point a test agent bot at it, or post a signed payload. `tests/conftest.py` has `signed_headers()`.

## Tests

```powershell
.venv\Scripts\python -m pytest
```

| File | Covers |
|---|---|
| `test_signature.py` | HMAC, timestamp window (±5 min), raw body vs re-serialised JSON, endpoint 401s |
| `test_handoff.py` | Keyword and human-request rules, word boundaries, turn limit, low confidence, invented price, spend cap, OFF/PAUSED, silence when not pending, loop prevention, duplicate deliveries, resume after pending, note order (status change before note) |
| `test_retriever.py` | Heading-aware chunking, BM25 ranking, empty results, swappable retriever, reindex, filename/path safety |
| `test_dashboard_auth.py` | Login, cookie flags, forced password change, roles, lockout, idle/absolute timeouts, forged cookies, open-redirect guard, frame headers, the panel link flow, single-use nonce, bearer-only panel, agent token used for sends, Chatwoot access check |
| `test_import.py` | DOCX headings/lists/tables/FAQ questions, PDF text extraction, scanned-PDF rejection, type detection by content, size limit, zip-bomb and non-Word zip rejection, review-before-save, CSRF and role checks, prices in documents not trusted |
| `test_gemini.py` | Gemini message mapping (system, merged turns, grouped tool results), thought signatures resent, tool schemas, thinking tokens billed, errors, provider/model switching, a full tool-call round trip, Settings validation |
| `test_rbac.py` | Starter roles, view-only roles can't write, permission changes apply without re-login, creating/renaming/deleting roles, locked admin role, no privilege escalation (roles, invites, password resets, self-changes), last-admin protection, panel view vs act |
| `test_deletes.py` | Knowledge delete/restore/purge and permission checks, user delete, customer data pages and search, memory-only vs full erasure (audit keeps counts only), menu photo cleanup, no reflected text in messages |
| `test_user_create.py` | Creating logins with typed or generated passwords, forced first-login change, weak/duplicate rejection, role limits, setting passwords (signs out, can't target higher access or yourself), passwords never in the audit log, invite links still work |
| `test_simulate.py` | Transcript parsing, a refund handoff showing summary and suggestion with no Chatwoot calls, the greeting on a direct reply, forced handoff, turn limit from bot lines, spend-cap fallback, escaping, CSRF and admin-only access |
| `test_csrf.py` | Missing, wrong and cross-session tokens, Origin check, HTMX header token, login-form CSRF, panel CSRF header, `/copilot/translate` CSRF |

## Deploying on Coolify

1. **Push this folder to a Git repo** (GitHub/GitLab/Gitea). Don't commit `.env`.
2. In Coolify, go to **+ New Resource → Docker Compose** (from your Git repository). Pick the repo and branch, with compose file `docker-compose.yml`. This brings up the app and Postgres with three persistent volumes: `knowledge`, `media` (menu photos) and `pgdata`.
3. **Environment variables**: set every variable from `.env.example` in the resource's Environment tab, using production values:
   - `APP_ENV=production`
   - `COOKIE_SECURE=true`
   - a long random `POSTGRES_PASSWORD`
   - `SESSION_SECRET` and `TOKEN_ENCRYPTION_KEY` (generation commands are in `.env.example`)
   - the Chatwoot token and secret

   The app **refuses to start in production** if a secret is missing or weak, or if Chatwoot's browser URL isn't https.
4. **Chatwoot URL**:
   - Chatwoot on the same Coolify server: set `CHATWOOT_BASE_URL` to the internal Docker network URL, e.g. `http://<chatwoot-service-name>:3000`. Both stacks must share a network; in Coolify, enable "Connect to Predefined Network" or put them in the same project network. Set `CHATWOOT_PUBLIC_URL=https://chat.example.com`.
   - Otherwise: set both to the public URL.
5. **Domain + HTTPS**: in the app service's domain field, enter `https://bot.example.com:8000`. The `:8000` tells Coolify which container port to route to; Coolify's proxy issues the Let's Encrypt certificate. Don't publish port 8000 on the host. The Dashboard App and admin must be served over HTTPS.
6. **Make the webhook public**: `https://bot.example.com/webhooks/chatwoot` must be reachable from Chatwoot. If you use `DASHBOARD_ALLOWED_IPS`, it applies only to `/admin/`; the webhook stays open, protected by its signature.
7. **Deploy**, then check `https://bot.example.com/health` → `{"status":"ok"}`. Coolify also uses the container's `HEALTHCHECK`.
8. After the first successful login, **remove `DASHBOARD_ADMIN_PASSWORD`** from Coolify. It's only read when the users table is empty.

Volume note: if Coolify uses bind mounts instead of named volumes, make the host folders writable by UID 10001, the container's non-root user.

## Chatwoot setup

1. **Settings → Integrations → Agent Bots → Add Bot.** Set the webhook (outgoing) URL to `https://bot.example.com/webhooks/chatwoot`. Copy the bot's **access token** to `CHATWOOT_BOT_TOKEN` and its **webhook secret** to `CHATWOOT_WEBHOOK_SECRET` in Coolify, then redeploy.
2. Open the target inbox → **Settings → Collaborators → Bot Configuration**, and select the bot. New conversations in that inbox now start as `pending`.
3. **Settings → Integrations → Dashboard apps → Configure → Add**. Name: "Restaurant Bot". URL: `https://bot.example.com/app-panel/`.
4. Send a test message from the website widget or WhatsApp and confirm the bot replies. Then send "I want a refund" and confirm the conversation moves to Open with two private notes.
5. Open `https://bot.example.com/admin/` and change the initial password. Check **Roles** (adjust `staff` and `agent` or add your own), then add people under **Users → Add user**: enter their email and role, and either type a password or leave it empty to generate one. Send the login details privately. Invite links are still available if you'd rather they set their own password.
6. **Each agent**:
   - logs in to `/admin/`;
   - opens **My profile** and pastes their Chatwoot **Access Token** (Chatwoot avatar → Profile settings);
   - in a Chatwoot conversation, opens the Restaurant Bot tab, clicks **Log in**, confirms the code in the new tab, then clicks **I've logged in**.

   Messages they send from the panel appear under their own name.

## Security checklist

- [ ] `APP_ENV=production`, `COOKIE_SECURE=true`, HTTPS on the bot domain (HSTS is sent).
- [ ] `SESSION_SECRET` (≥32 random chars) and `TOKEN_ENCRYPTION_KEY` (Fernet) are set and stored only in Coolify. Rotating `SESSION_SECRET` logs everyone out; rotating `TOKEN_ENCRYPTION_KEY` means agents must reconnect their Chatwoot tokens.
- [ ] `DASHBOARD_ADMIN_PASSWORD` removed after first login; initial password changed (this is enforced).
- [ ] Port 8000 is not published on the host, so `FORWARDED_ALLOW_IPS=*` is only reachable through Coolify's proxy. If you publish it, set `FORWARDED_ALLOW_IPS` to the proxy's IP.
- [ ] Consider `DASHBOARD_ALLOWED_IPS` for `/admin/`.
- [ ] Webhook: signature over `{timestamp}.{raw_body}`, a ±5-minute window, constant-time compare, 1 MB body limit, per-conversation rate limit and message-ID de-duplication.
- [ ] Admin pages send `X-Frame-Options: DENY` and `frame-ancestors 'none'`. Only `/app-panel` (and `/static`) allow framing, by `'self'` and the Chatwoot origin.
- [ ] CSRF tokens on every admin form and HTMX request, plus an `Origin` check. The panel uses a bearer token and a CSRF header (no cookies).
- [ ] Review **Roles**: permissions are enforced on the server for every route (`app/security/permissions.py`). Give `users.*` and `roles.*` only to people you trust; the anti-escalation rules stop them exceeding their own access, not misusing it.
- [ ] Logins: argon2id, lockout after 5 failures per account or 20 per IP in 15 minutes, generic error messages.
- [ ] Customer text is escaped everywhere (Jinja autoescape). The Markdown preview is sanitised with `nh3`. CSV exports neutralise formula injection.
- [ ] Logs are JSON with IDs and event types only. No message bodies, no query strings (the uvicorn access log is off), and secret-looking fields are masked.
- [ ] Audit log covers every write: who, what, when, before and after, with secrets masked.
- [ ] Order lookups by phone only work for the phone number on the conversation. Order contents are only shown when that phone matches.
- [ ] The Chatwoot bot token is used only for bot replies, status changes and handoff notes. Agent actions use the agent's own encrypted token, and the panel re-checks conversation access with that token.
- [ ] Back up the `pgdata`, `knowledge` and `media` volumes.
- [ ] Pin the CDN scripts (Tailwind Play CDN, htmx 2.0.4) with SRI hashes, or self-host them. Tailwind's Play CDN is not meant for production; replace it with a built CSS file when convenient.

## Not verified against Chatwoot's docs: check before going live

I read the Chatwoot webhook and agent-bot guides, the Dashboard Apps guide (dated Apr 2024), and the API reference for messages, toggle_status, conversation details, profile and contacts. These points were not confirmed:

1. **Dashboard App context**:
   - Docs show `event.data` as a JSON *string*: `{"event":"appContext","data":{"conversation":…,"contact":…,"currentAgent":{id,name,email}}}`.
   - The request message is `'chatwoot-dashboard-app:fetch-info'`.
   - **The docs don't state the message origin.** `panel.js` accepts only messages whose `event.origin` equals `CHATWOOT_PUBLIC_URL`'s origin, and sends `fetch-info` with that origin as `targetOrigin` (the docs use `'*'`). If the panel never loads context, check the origin first.
   - **Not documented: whether `conversation.id` is the display ID** used in API URLs. The code assumes it is.
2. **No verified agent identity reaches the iframe.** `currentAgent` has only id, name and email, with no signature, so the panel never trusts it. Identity comes from the panel login, and access is re-checked with the agent's own Chatwoot token. `conversation.meta.hmac_verified` relates to contact identity, not agents.
3. **Iframe sandbox and permissions are undocumented.** If Chatwoot's iframe blocks popups, the "Log in" link can't open a tab; the code is also shown, so agents can open `/admin/panel-link` (linked from My profile) and type it in. If clipboard access is blocked, the "Copy" button falls back to `execCommand`.
4. **Agent-bot payload details**:
   - Assumed: `sender.type == "contact"`, `message_type` is the string `"incoming"` (integer `0` is also accepted), `private`, `conversation.display_id`, and `conversation.status`.
   - The docs' sample shows `display_id` but not `status`. If `status` is missing, the bot fetches the conversation with the bot token.
   - Docs say agent bots get the same signature headers as webhooks, but that is an inference from the agent-bot page.
5. **Status changes for bots**: `toggle_status` lists agent-bot auth in the API spec, but the docs say bot tokens reach only "bot-permitted APIs" without listing them. **Test the handoff once on your instance.**
6. **Close events**: the docs say `conversation_status_changed` is **not delivered to agent bots**. Summaries therefore run when any later event shows `status: resolved`. The code also accepts `conversation_status_changed`, but an account webhook would need its own secret (see "Open decisions").
7. **Message history**: `GET …/messages` doesn't list bot auth, so history comes from the local transcript. Conversations that started before the bot was deployed will lack earlier context.
8. **Connection test**: uses `GET /api` for reachability and expects 404 vs 401 on a non-existent conversation to check the bot token. Both are reasonable guesses, not documented contracts.
9. **Coolify**: the `https://domain:8000` port syntax and the predefined-network option follow Coolify's usual behaviour. Confirm them in your Coolify version.
10. **LLM prices** in `app/services/llm/pricing.py` are defaults; check current OpenAI and Gemini pricing or override them in Settings. Gemini prices were read from Google's pricing page on 2026-11-25; where Google announced a rise, the higher price is used.
11. **Gemini function calling with a JSON response format** isn't documented as supported together, so with tools the bot asks for JSON in the prompt and parses it from the text. If a model wraps JSON oddly, replies hand off rather than send.

## Open decisions (choices I made without changing your security model or Chatwoot setup)

- **Panel login mechanism**: the spec says no cookies in the iframe and a "Log in" tab plus "I've logged in". This is implemented as a one-time pairing:
  1. The panel creates a nonce and a short code.
  2. The agent confirms the code in the logged-in admin tab.
  3. The panel exchanges the nonce for a bearer token, kept in `sessionStorage` (partitioned per site) with the same 30-minute idle and 12-hour absolute timeouts.

  Like any device-code flow, a user tricked into confirming someone else's code would link that person's panel. The confirmation page warns about this, and codes expire after 10 minutes.
- **`CHATWOOT_PUBLIC_URL`** was added because an internal Docker URL can't serve as the iframe's `frame-ancestors` or postMessage origin.
- **Conversation summaries at close** depend on Chatwoot telling the bot about `resolved`. Reliable summaries would need an account-level webhook (Settings → Integrations → Webhooks → `conversation_status_changed`) with its own secret. That changes the Chatwoot setup, so it isn't wired up.
- Orders are read from the `orders` table, which is meant to be filled by your POS or ordering system. There is no admin page for orders.
- Rate limiting for the webhook and panel-link creation is in-process. That suits one container; use Redis before scaling out.
- Schema is created with `create_all` on boot. Add Alembic before your first schema change in production.
