# Campaign Financial Management: MVP

A mobile-friendly web app for an advertising and activation agency. It connects **approved campaign budgets** with **payment requests**, **MD approvals**, **finance payments**, **advance settlements** and **budget-vs-actual reports**. The aim is to cut campaign financial closure from 3–4 weeks to 7–10 days.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the system design review, the three architecture options and the recommendation.

## Features

- **Campaigns and budgets:** category-wise approved budgets. Revisions need a reason. Finance proposes a revision and the MD approves it. Budgets can't be cut below what is already paid or committed.
- **Requests:** staff advances and supplier payments raised against a campaign category. A quote can be uploaded from the phone camera, and the form shows live available budget.
- **Instalments without double counting:** the first instalment records the supplier's full quote as a contract. Second and final instalments draw the contract down and can't exceed it.
- **Supervisor first, then the MD (new):** each staff member can have a supervisor (account manager / cost-centre head). Their requests go to the supervisor to endorse, clarify or reject before reaching the MD, so the MD only sees checked requests. Staff without a supervisor go straight to the MD. Set it in `/admin/` under *Staff profiles*.
- **Email notifications (new):** every step that needs someone emails them a short summary with a link: approval needed, endorsed, approved, clarify, rejected, ready to pay, paid, settlement to verify, settlement verified or returned, and budget revisions. Sends through **Outlook (Microsoft Graph)**, SMTP (e.g. Gmail), or **simulated** mode, where nothing is sent and each person's mail appears in the in-app **Inbox**. Email never carries authority: approvals happen in the app after sign-in. A failed send is logged and never blocks the workflow.
- **Client closure report as PDF (new):** one click builds the report Arun's team used to paste together by hand. It has a summary against the client PO, budget vs actual, an expense schedule with the evidence for each payment, advance settlements, an invoice summary (verified spend + agency commission %), the closure checklist and an appendix with every bill and receipt (photos placed on the page, PDF invoices merged in). It is marked DRAFT until the checklist is complete, and can be downloaded or **emailed to the client** from the report page.
- **MD approval screen:** budget, already paid, available before and after, contract position, and prior payments to the same supplier. One tap to approve, clarify or reject. Going over budget needs a comment. A snapshot of the figures is stored with each decision. Nobody can approve their own request.
- **Finance:** pays only MD-approved requests and must enter a transaction reference. Optional payment proof upload and partial payment.
- **Bills and settlements:** supplier bills are uploaded and verified. Advances are settled with receipts and cash returned, then verified by finance. Overspends show as a reimbursement due.
- **Three separate figures:** budget, cash out (net of returns) and verified spend.
- **Reports:** budget vs actual, payment history (printable to PDF), CSV export (QuickBooks-ready columns), an append-only audit trail, and a **closure checklist** that blocks closing while anything is open.
- **AI receipt & bill reading (optional):** uploaded quotes, bills and receipts (photos or PDFs) are read by **OpenAI GPT-6 Luna**, the low-cost model, or by Claude. The app then cross-checks:
  - bill total against the amount paid
  - vendor against the payee
  - quote against the contract value
  - receipts against the claimed settlement
  - possible duplicate bills
  - dates outside the campaign, and illegible or handwritten documents

  Settlements are pre-filled from the receipts. These checks are advisory only: the AI never approves, pays or changes figures.
- **Ask AI (query agent):** a LangGraph agent on GPT-6 Luna answers plain-English questions such as "How much is left for promoters on CMP-2611?" or "Which advances are still open?". It uses 9 read-only, role-scoped tools built on the same budget code as the screens. Field staff only see their own requests, and the agent can't approve or pay anything.
- **Roles:** Field Staff (sees own requests only), MD, Finance, Admin.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo          # sample users, 3 campaigns, requests in every state
python manage.py runserver
```

Open http://127.0.0.1:8000. All demo users have the password **`demo1234`**:

| User | Role | Try |
|---|---|---|
| `kasun`, `dilini` | Field staff | Submit a request (it goes to Tharindu first); answer the MD's clarification on SUP-00010; check **Inbox** |
| `tharindu` | Account manager (supervisor of kasun and dilini) | **Team** tab: endorse SUP-00016 before it reaches the MD |
| `md` | Managing Director | **Approvals** tab: 4 requests (one over budget) and 1 budget revision |
| `finance` | Finance | **Finance** tab: pay, verify bills, verify settlements; CMP-2609 → **Report** → *Download client PDF* or *Email PDF*; **Inbox** → *All emails* |
| `admin` | Admin / superuser | Everything, plus `/admin/` |

Run tests: `python manage.py test finance` (the AI is mocked in tests, so no key is needed)

### Turning on AI receipt reading

1. Copy `.env.example` to `.env` (next to `manage.py`).
2. Set `OPENAI_API_KEY=sk-...` in it. Never commit `.env`; it is git-ignored.
3. Restart the server. Bills, receipts and quotes are read automatically on upload. Each document also gets a "Read with AI" / "Re-read" button.

The defaults are `AI_PROVIDER=openai`, `AI_MODEL=gpt-6-luna` and `AI_REASONING_EFFORT=low`. One receipt is roughly 1–2k tokens, which is a fraction of a US cent on Luna. For messy handwritten receipts, set `AI_MODEL=gpt-6-sol`, which is about 20× the price. To use Claude instead, set `AI_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`.

### Sending real email through Outlook

1. In the Microsoft Entra admin centre, register an app, add the **Mail.Send** *application* permission for Microsoft Graph and grant admin consent. Create a client secret.
2. Recommended: restrict the app to the one shared mailbox with an Exchange Online application access policy (`New-ApplicationAccessPolicy`), so it cannot send as anyone else.
3. Set `EMAIL_PROVIDER=outlook`, `MS_GRAPH_TENANT_ID`, `MS_GRAPH_CLIENT_ID`, `MS_GRAPH_CLIENT_SECRET`, `MS_GRAPH_SENDER` (e.g. `finance@youragency.lk`) and `SITE_URL` (the public address used in email links).
4. Give every user an email address in `/admin/`. Users can be opted out under *Staff profiles*.

Attachments over ~3 MB are not attached but linked (Graph's single-request limit). For Gmail or Google Workspace use `EMAIL_PROVIDER=smtp` with an app password.

### Running the AI on your own server

Set `AI_PROVIDER=local` and `AI_BASE_URL` to any OpenAI-compatible server, for example llama.cpp: `llama-server -m qwen2.5-vl-7b-instruct-q4_k_m.gguf --mmproj mmproj.gguf --port 8080`. Receipt photos go to the vision model; PDFs are sent as their text layer. Ask AI uses the same server through tool calling. Accuracy on messy receipts is lower than the cloud models, so check a sample before relying on it.

## Project layout

```
config/                 settings (env-driven), urls, wsgi
finance/models.py       domain model
finance/services.py     budget math, role checks, workflow transitions, audit logging
finance/notify.py       email notifications: simulated inbox, Outlook (Microsoft Graph), SMTP
finance/reports.py      client closure report PDF (reportlab + pypdf)
finance/views.py        thin views calling services
finance/templates/      mobile-first server-rendered UI
finance/management/commands/seed_demo.py
finance/tests.py        budget math, controls, HTTP smoke tests
docs/ARCHITECTURE.md    design options and roadmap
```

## Free hosting (no card)

See [`docs/DEPLOY.md`](docs/DEPLOY.md): **PythonAnywhere free** (tested; OpenAI is on its allowlist), or a container host plus free Neon Postgres with `FILE_STORAGE=database`.

## Deploy to Render (one click)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/ashandilgith/campaign_financial_management)

The `render.yaml` blueprint creates a web service, a Postgres database and a 1 GB disk for receipts, all in the Singapore region. Render will ask for:
- `OPENAI_API_KEY`: needed for AI receipt reading and Ask AI. GitHub secrets are **not** passed to Render, so set it here as well.
- `DJANGO_SUPERUSER_USERNAME` / `DJANGO_SUPERUSER_PASSWORD`: your admin login.

`SEED_DEMO=1` loads the demo campaigns and users (password `demo1234`) so a client can try the app straight away. **Before using real data:** set `SEED_DEMO=0`, delete the demo users in `/admin/`, and create real users.

## Live AI evaluation

The `Live AI eval` GitHub workflow runs on your `OPENAI_API_KEY` secret. It reads a generated receipt photo and invoice PDF, and asks the agent 15 realistic questions as different users. The scored reports go to the `eval-results` branch.

## Deploying elsewhere

Set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` and `DATABASE_URL` (PostgreSQL). Then:

```bash
pip install -r requirements.txt
python manage.py migrate && python manage.py collectstatic --noinput
gunicorn config.wsgi --bind 0.0.0.0:8000
```

Put Caddy or Nginx in front for HTTPS and to serve `/media/` (uploaded bills and receipts), and back up the database and media nightly. A `Dockerfile` is included.

## Not in the MVP (next steps)

WhatsApp alerts, reading the client PO straight from an Outlook inbox, password reset and 2FA, S3 receipt storage, QuickBooks sync, AI-based anomaly detection across campaigns. See the roadmap in `docs/ARCHITECTURE.md`.
