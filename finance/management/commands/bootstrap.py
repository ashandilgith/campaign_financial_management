"""Idempotent first-run setup used by build.sh: role groups, admin user, optional demo data."""
import os

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import BaseCommand

from finance import services as svc
from finance.models import Campaign


class Command(BaseCommand):
    help = "Create role groups and the admin user from env; seed demo data if SEED_DEMO=1 and DB is empty."

    def handle(self, *args, **opts):
        for name in svc.ALL_ROLES:
            Group.objects.get_or_create(name=name)
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME")
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
        if username and password and not User.objects.filter(username=username).exists():
            u = User.objects.create_superuser(username=username, password=password)
            u.groups.add(Group.objects.get(name=svc.ROLE_ADMIN))
            self.stdout.write(f"Created admin user '{username}'.")
        if os.environ.get("SEED_DEMO") == "1":
            if not Campaign.objects.exists():
                call_command("seed_demo")
            elif Campaign.objects.filter(code="CMP-2611").exists() and not User.objects.filter(username="tharindu").exists():
                # Demo database from before the supervisor / email / PDF release: reload the demo story.
                self.stdout.write("Upgrading demo data (supervisor layer, emails, closure report).")
                call_command("seed_demo", reset=True)
        self.stdout.write("Bootstrap complete.")
