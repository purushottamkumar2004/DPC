"""The two roles of D-095 (Operator, Engineer), prepared for UI-3/UI-5 (no login in UI-1/UI-2)."""
from django.db import migrations

ROLES = ("Operator", "Engineer")


def create_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in ROLES:
        Group.objects.get_or_create(name=name)


def remove_roles(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name__in=ROLES).delete()


class Migration(migrations.Migration):
    dependencies = [("monitor", "0001_initial"), ("auth", "0012_alter_user_first_name_max_length")]
    operations = [migrations.RunPython(create_roles, remove_roles)]
