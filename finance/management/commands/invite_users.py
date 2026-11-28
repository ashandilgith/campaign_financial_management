"""Create individual logins from a CSV and email each person a link to set their password.

    python manage.py invite_users users.csv

CSV columns: username, first_name, last_name, email, role, supervisor (optional username)
role is one of: Field Staff, MD, Finance, Admin. Existing usernames are updated, not duplicated.
"""
import csv

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from finance import services as svc
from finance.models import StaffProfile


class Command(BaseCommand):
    help = "Create users from a CSV and send set-password invitations."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--no-email", action="store_true", help="Create users without sending invitations")

    def handle(self, csv_path, no_email=False, **opts):
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            raise CommandError("The CSV has no rows.")
        problems = []
        for n, r in enumerate(rows, start=2):
            if not r.get("username") or not r.get("email"):
                problems.append(f"row {n}: username and email are required")
            if r.get("role") not in svc.ALL_ROLES:
                problems.append(f"row {n}: role must be one of {', '.join(svc.ALL_ROLES)}")
        if problems:
            raise CommandError("\n".join(problems))
        created = []
        with transaction.atomic():
            for r in rows:
                u, new = User.objects.get_or_create(username=r["username"].strip())
                u.first_name, u.last_name, u.email = r.get("first_name", ""), r.get("last_name", ""), r["email"].strip()
                if new:
                    u.set_unusable_password()
                u.save()
                u.groups.set([Group.objects.get_or_create(name=r["role"])[0]])
                if r["role"] == svc.ROLE_ADMIN:
                    u.is_staff = True
                    u.save(update_fields=["is_staff"])
                if new:
                    created.append(u)
            for r in rows:
                if r.get("supervisor"):
                    sup = User.objects.filter(username=r["supervisor"].strip()).first()
                    if sup is None:
                        raise CommandError(f"supervisor {r['supervisor']!r} not found")
                    StaffProfile.objects.update_or_create(user=User.objects.get(username=r["username"].strip()),
                                                          defaults={"supervisor": sup})
            if not no_email:
                for u in created:
                    svc.invite_user(None, u)
        self.stdout.write(self.style.SUCCESS(
            f"{len(rows)} users processed, {len(created)} new" + ("" if no_email else f", {len(created)} invited") + "."))
