# System design: three options

This review is based on the *Campaign Financial Management Platform – Initial product concept* (Nov 2026).

## What the system has to do

| Driver | Implication for design |
|---|---|
| Scale is small: 3–4 campaigns/month, LKR 4–5M spend, roughly 10–20 users | One server and one database are enough. No microservices, queues or caching layer. |
| The real cost is **closure time** (3–4 weeks now, 7–10 days wanted) | Closure-readiness checks and verification queues matter more than features. |
| Field staff and the MD use **phones** | Fast server-rendered pages, large tap targets, camera upload for receipts, and one-tap approve/reject/clarify. |
| Money must not be **double counted** across instalments | A *contract* concept with the full quote committed once, which instalments then draw down. |
| Three money concepts must stay separate | Track **budget**, **cash out** (paid/advanced, net of returns) and **verified spend** (bill/settlement checked) separately. |
| Audit and segregation of duties | Role checks in the service layer: MD approves, Finance pays, nobody approves their own request. Every change goes to an append-only audit log, along with a snapshot of the figures the MD saw at decision time. |
| QuickBooks and AI receipt checks come **later** | Keep a clean payment ledger with export, and design for an integration adapter without building one yet. |
| Budget ceiling around LKR 500k, Python preferred | Lean on a batteries-included framework and avoid a separate SPA. |

## Core domain model (common to all options)

```
Campaign ─┬─ BudgetLine (category, approved_amount) ── BudgetRevision (old→new, reason, MD-approved)
          ├─ SupplierContract (supplier, total quote)  ← instalments draw this down
          └─ PaymentRequest (advance | supplier, amount, status, instalment)
                 ├─ ApprovalDecision (MD: approve/reject/clarify + budget snapshot)
                 ├─ Payment (finance: amount, date, method, txn reference)
                 ├─ Settlement (advances: spent, cash returned, finance-verified)
                 ├─ Attachment (quote, bill, receipt, payment proof)
                 └─ AuditLog (append-only)
```

Request lifecycle:

```
submitted ──MD──► approved ──Finance──► paid ──(advance)──► settlement submitted ──Finance──► settled
    │  ▲               │                     └─(supplier)──► bill uploaded ──Finance──► bill verified
    │  └─ clarify ◄────┘ (requester responds)
    └──► rejected / cancelled
```

Budget position per category:

```
available = budget − cash_out − committed − pending
  cash_out  = payments − cash returned on verified settlements
  committed = approved-unpaid requests + (contract total − paid on contract) for contracts with an approved instalment
  pending   = requests awaiting MD (+ full contract value when its first instalment is still pending)
```

---

## Option A: Django monolith with server-rendered pages (**recommended, built in this repo**)

**Stack:** Django 5, server-rendered templates with a little vanilla JS, PostgreSQL (SQLite for dev), file storage on disk or S3-compatible storage, Django admin as the back office.

**Shape:** one deployable app, with business rules in `finance/services.py` and thin views.

| | |
|---|---|
| ✅ | Python, as the brief asks. Auth, roles (Groups), admin, forms, migrations and CSRF all come built in. |
| ✅ | Mobile-friendly without building a native app. Pages are small and work on weak 4G. |
| ✅ | Cheapest to build and run. Any Python developer in Colombo can maintain it. |
| ✅ | Easy to grow: add a QuickBooks adapter, notifications (email/WhatsApp) and receipt OCR as separate modules later. |
| ⚠️ | No offline mode, and the UI is functional rather than "app-like". |
| ⚠️ | A full page reload per action. That's fine at this scale. HTMX can be added later. |

**Effort to pilot-ready:** this MVP covers the core workflow. A pilot still needs about 3–4 weeks: hardening, the client's real categories, email/WhatsApp notifications, deployment and backups, a UAT on one live campaign, and training.

**Indicative cost:** fits comfortably within the LKR 500k ceiling. Hosting is a small VPS or PaaS plus managed Postgres, at roughly USD 10–30/month. Add a light maintenance retainer.

## Option B: API backend with a PWA front-end

**Stack:** FastAPI or Django REST Framework, a React/Vue Progressive Web App (installable, offline queue for receipts, push notifications), PostgreSQL and object storage.

| | |
|---|---|
| ✅ | Best mobile experience: installable icon, push notifications for the MD, offline receipt capture in the field. |
| ✅ | A clean API that a future mobile app, QuickBooks sync or AI receipt service can reuse. |
| ⚠️ | Two codebases (Python plus JavaScript), with roughly 2× the build effort and a narrower pool of maintainers. |
| ⚠️ | Auth, validation and permissions have to be built on both sides, and offline sync adds tricky edge cases around money. |
| ⚠️ | Likely **exceeds** the LKR 500k ceiling for the same scope, or forces cuts to reporting and audit. |

**When to choose:** after the pilot, if field staff actually work offline or the MD needs push approvals. Option A's service layer can be exposed through DRF, so moving from A to B is incremental rather than a rewrite.

## Option C: Low-code / configure existing tools

**Variants:** AppSheet or Power Apps on top of the current Google Sheets; or a Frappe/ERPNext setup (Python) with custom doctypes and approval workflows; or QuickBooks add-ons for approvals.

| | |
|---|---|
| ✅ | Fastest to a first demo (days), and data stays where the team already works. |
| ✅ | ERPNext comes with accounting, workflows, roles and a mobile UI. |
| ⚠️ | Sheets-based apps struggle with the core problem: transactional integrity, double counting across instalments, and a reliable audit trail. |
| ⚠️ | Per-user licence fees grow with users. Custom budget math gets fragile inside low-code formulas. |
| ⚠️ | ERPNext is a large system to host and upgrade, and too much for 3–4 campaigns a month. Customisation needs specialist Frappe skills. |
| ⚠️ | Vendor lock-in, and exporting history later is harder. |

**When to choose:** as a 2–3 week stop-gap to prove the workflow if no budget is released, or if the company also wants to replace QuickBooks with a full ERP.

---

## Comparison

| Criterion | A. Django monolith | B. API + PWA | C. Low-code / ERPNext |
|---|---|---|---|
| Fit to brief (Python, simple mobile) | ●●● | ●●○ | ●○○ / ●●○ |
| Budget-math correctness & audit | ●●● | ●●● | ●○○ (Sheets) / ●●○ (ERPNext) |
| Build cost | Low | Medium–High | Very low / Medium |
| Running cost | Low | Low–Medium | Licences / Medium |
| Time to live pilot | ~4 weeks | ~8–10 weeks | ~2 weeks / ~6 weeks |
| Future QuickBooks / AI | Adapter module | Natural | Plug-ins / limited |
| Maintainability by a small team | ●●● | ●●○ | ●○○ |

**Recommendation:** use **Option A** now. It's the smallest thing that fixes closure time while keeping money math and audit trustworthy. Keep the service layer API-ready so that a PWA (Option B) or a QuickBooks sync can be added once the pilot proves the workflow.

## Phased roadmap

1. **MVP (this repo):** campaigns and budgets, controlled revisions, advance and supplier requests, instalment contracts, MD approve/reject/clarify with a budget check, finance payments with references, bill verification, advance settlement, budget-vs-actual reports, CSV export, audit trail, closure checklist.
2. **Pilot hardening (about 3–4 weeks):** email/WhatsApp notifications, Postgres plus nightly backups, S3-compatible receipt storage, password reset/2FA for the MD and Finance, the client's real category list, UAT on one live campaign.
3. **Integrations:** a QuickBooks Online sync of paid payments as bills/expenses (start with the CSV import that already exists), and supplier master sync.
4. **Assistive (✅ first part done):** AI document reading with GPT-6 Luna (default) or Claude. It extracts structured fields from photos and PDFs, and deterministic rules in `services.ai_checks` cross-check those fields. Still to do: cross-campaign anomaly checks such as duplicate bills or the same supplier paid twice.

## Production deployment (Option A)

```
[Phone / laptop] ──HTTPS──► [Caddy or Nginx] ──► [gunicorn + Django] ──► [PostgreSQL]
                                                      └──► media/ (or S3-compatible bucket) for receipts
                                         nightly: pg_dump + media sync → off-site backup
```

AI design: the model only *reads* documents into a strict JSON schema. All judgements ("does the bill match the payment?") are plain Python rules, so they're explainable and auditable, and the AI can't move money. When no API key is set, the feature switches off cleanly.

Environment variables: `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY` with `AI_PROVIDER=anthropic`), `AI_MODEL`, `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `DATABASE_URL`, `MEDIA_ROOT`.


## Release 2 (Nov 2026): supervisor layer, email, client PDF

Changes after the 25 Nov review with Arun:

- **Approval routing.** `StaffProfile.supervisor` adds a first approver. `submit_request` puts the request in `endorse` ("Awaiting supervisor") when the requester has one; `endorse_request` moves it to `submitted` (the MD's queue), or to `clarify` / `rejected`. Each `ApprovalDecision` records its `stage` (supervisor or MD), and a clarification returns to whichever stage asked. Requests awaiting a supervisor count as *pending* in the budget, so availability stays right.
- **Notifications** (`finance/notify.py`). Every notifiable event writes an `OutboundEmail` row, then delivers it after the transaction commits. Providers: `simulated` (the Inbox page), `outlook` (Graph client-credentials token + `/users/{sender}/sendMail`, no SDK) and `smtp`. Delivery errors are stored on the row and never roll back the business action.
- **Client closure report** (`finance/reports.py`). Built from `campaign_position`, the same numbers as the screens. Images are placed on the page; PDF bills are merged after their cover page with pypdf. Campaigns gained `client_po_number`, `client_email` and `commission_pct`.
- **Local AI option.** `AI_PROVIDER=local` sends document reading and Ask AI to an OpenAI-compatible server (llama.cpp, Ollama, vLLM), for clients who want everything on their own hardware.

Next: read the client's approved PO from the Outlook inbox to pre-fill the campaign budget (Graph `Mail.Read` on one mailbox), and WhatsApp alerts.
