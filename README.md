# Campaign Financial Management: MVP

A mobile-friendly web app for an advertising and activation agency. It connects **approved campaign budgets** with **payment requests**, **MD approvals**, **finance payments**, **advance settlements** and **budget-vs-actual reports**. The aim is to cut campaign financial closure from 3–4 weeks to 7–10 days.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the system design review, the three architecture options and the recommendation.

## Features

- **Campaigns and budgets:** category-wise approved budgets. Revisions need a reason. Finance proposes a revision and the MD approves it. Budgets can't be cut below what is already paid or committed.
- **Requests:** staff advances and supplier payments raised against a campaign category. A quote can be uploaded from the phone camera, and the form shows live available budget.
- **Instalments without double counting:** the first instalment records the supplier's full quote as a contract. Second and final instalments draw the contract down and can't exceed it.
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
| `kasun`, `dilini` | Field staff | Submit an advance or supplier request; answer the MD's clarification on SUP-00010 |
| `md` | Managing Director | **Approvals** tab: 4 requests (one over budget) and 1 budget revision |
| `finance` | Finance | **Finance** tab: pay, verify bills, verify settlements; close CMP-2609 |
| `admin` | Admin / superuser | Everything, plus `/admin/` |

Run tests: `python manage.py test finance` (the AI is mocked in tests, so no key is needed)

### Turning on AI receipt reading

1. Copy `.env.example` to `.env` (next to `manage.py`).
2. Set `OPENAI_API_KEY=sk-...` in it. Never commit `.env`; it is git-ignored.
3. Restart the server. Bills, receipts and quotes are read automatically on upload. Each document also gets a "Read with AI" / "Re-read" button.

The defaults are `AI_PROVIDER=openai`, `AI_MODEL=gpt-6-luna` and `AI_REASONING_EFFORT=low`. One receipt is roughly 1–2k tokens, which is a fraction of a US cent on Luna. For messy handwritten receipts, set `AI_MODEL=gpt-6-sol`, which is about 20× the price. To use Claude instead, set `AI_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`.

## Project layout

```
config/                 settings (env-driven), urls, wsgi
finance/models.py       domain model
finance/services.py     budget math, role checks, workflow transitions, audit logging
finance/views.py        thin views calling services
finance/templates/      mobile-first server-rendered UI
finance/management/commands/seed_demo.py
finance/tests.py        budget math, controls, HTTP smoke tests
docs/ARCHITECTURE.md    design options and roadmap
```

## Deploying

Set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` and `DATABASE_URL` (PostgreSQL). Then:

```bash
pip install -r requirements.txt
python manage.py migrate && python manage.py collectstatic --noinput
gunicorn config.wsgi --bind 0.0.0.0:8000
```

Put Caddy or Nginx in front for HTTPS and to serve `/media/` (uploaded bills and receipts), and back up the database and media nightly. A `Dockerfile` is included.

## Not in the MVP (next steps)

Notifications (email/WhatsApp), password reset and 2FA, S3 receipt storage, QuickBooks sync, AI-based anomaly detection across campaigns. See the roadmap in `docs/ARCHITECTURE.md`.
