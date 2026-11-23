# PythonAnywhere WSGI file: paste this into the file linked from the Web tab ("WSGI configuration file").
# Replace YOURNAME with your PythonAnywhere username.
import os
import sys

path = "/home/YOURNAME/campaign_financial_management"
if path not in sys.path:
    sys.path.insert(0, path)

# Secrets and settings live in /home/YOURNAME/campaign_financial_management/.env (loaded by config/settings.py)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
