# Deploying without a credit card

Both options below were tested end to end on 23 Nov 2026, from a fresh checkout with production settings.
The tests covered the health check, the HTTPS redirect, login, every main page, the CSV export, static files,
and AI calls routed through an allowlist proxy.

| | **PythonAnywhere free** (recommended) | **Container host + Neon Postgres** |
|---|---|---|
| Card needed | No | No (SnapDeploy; Koyeb Hobby via a new organization) |
| Database | SQLite file on PythonAnywhere's persistent disk | Neon free Postgres (`DATABASE_URL`) |
| Uploaded receipts | Persistent disk (default) | `FILE_STORAGE=database`, so files are kept in Postgres and survive redeploys |
| AI (OpenAI) | `api.openai.com` is on the free-plan allowlist | Unrestricted |
| Address | `yourname.pythonanywhere.com` | Host's subdomain |
| Sleeps when idle | No, but free apps must be renewed every few months from the dashboard | Yes, with a slow first load after idle |

## Option 1: PythonAnywhere (free)

1. Sign up for a free "Beginner" account at pythonanywhere.com. Your username becomes your web address.
2. Open a **Bash console** and run:
   ```bash
   git clone https://github.com/ashandilgith/campaign_financial_management.git
   cd campaign_financial_management
   python3.12 -m venv ~/.venvs/cfm && source ~/.venvs/cfm/bin/activate
   pip install -r requirements.txt
   cp .env.example .env && nano .env      # set the values below
   python manage.py migrate && python manage.py collectstatic --noinput && python manage.py bootstrap
   ```
   Values to set in `.env`:
   ```
   DJANGO_DEBUG=0
   DJANGO_SECRET_KEY=<long random string>
   DJANGO_ALLOWED_HOSTS=YOURNAME.pythonanywhere.com
   DJANGO_CSRF_TRUSTED_ORIGINS=https://YOURNAME.pythonanywhere.com
   OPENAI_API_KEY=sk-...
   SEED_DEMO=1                # demo data for the client; set 0 for real use
   DJANGO_SUPERUSER_USERNAME=owner
   DJANGO_SUPERUSER_PASSWORD=<strong password>
   ```
3. **Web tab → Add a new web app → Manual configuration → Python 3.12.**
   - **Virtualenv:** `/home/YOURNAME/.venvs/cfm`
   - **WSGI file:** replace its contents with `deploy/pythonanywhere_wsgi.py`, putting in your username.
   - **Static files:** URL `/static/` → directory `/home/YOURNAME/campaign_financial_management/staticfiles`
   - Turn on **Force HTTPS**, then click **Reload**.
4. To update after a push: `git pull`, `python manage.py migrate`, `collectstatic`, then Reload.

## Option 2: Container host (SnapDeploy / Koyeb Hobby) + Neon

1. Create a free Neon project and copy its connection string (`postgresql://…?sslmode=require`).
2. On the host, deploy this repo with its `Dockerfile`, which listens on `$PORT`. Set these environment variables:
   ```
   DATABASE_URL=<Neon connection string>
   FILE_STORAGE=database
   DJANGO_DEBUG=0
   DJANGO_SECRET_KEY=<long random string>
   DJANGO_ALLOWED_HOSTS=<app>.<host domain>
   DJANGO_CSRF_TRUSTED_ORIGINS=https://<app>.<host domain>
   OPENAI_API_KEY=sk-...
   SEED_DEMO=1
   DJANGO_SUPERUSER_USERNAME=owner
   DJANGO_SUPERUSER_PASSWORD=<strong password>
   ```
3. The container runs migrations and bootstrap on start. Health check path: `/healthz`.

## Render (needs a card on accounts that already have services)

Use the `render.yaml` blueprint: see the README.
