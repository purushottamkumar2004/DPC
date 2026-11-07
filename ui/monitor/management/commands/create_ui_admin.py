"""Create (or reset) a UI engineer account: `python3 manage.py create_ui_admin --username <name>`.

UI-1 and UI-2 need no login (proposed D-095 amendment); accounts are prepared for UI-3/UI-5.
The password is asked for on the terminal and never printed or stored in a file.
"""
import getpass

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or reset the initial UI administrator (Engineer role)."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)

    def handle(self, *args, **options):
        username = options["username"].strip()
        if not username:
            raise CommandError("username must not be empty")
        password = getpass.getpass("New password: ")
        if len(password) < 8:
            raise CommandError("the password needs at least 8 characters")
        if password != getpass.getpass("Repeat password: "):
            raise CommandError("the passwords differ")
        user, created = User.objects.get_or_create(username=username)
        user.is_staff = user.is_superuser = True
        user.set_password(password)
        user.save()
        engineer, _ = Group.objects.get_or_create(name="Engineer")
        user.groups.add(engineer)
        self.stdout.write(f"{'Created' if created else 'Updated'} UI administrator '{username}' (Engineer).")
