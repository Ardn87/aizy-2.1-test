#!/usr/bin/env python3
"""Installe les scénarios fictifs et des comptes locaux sur une base neuve."""

import importlib.util
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "facture_project.settings")

import django

django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction

from core.access import ROLE_ADMIN, ROLE_RECEPTION, ROLE_REVUE
from core.models import Restaurant, RestaurantAccess


PACKAGE = ROOT / "jeu-donnees-fictif-aizy-2.1"
ACCOUNT_NAMES = ("Superadmin", "Admin", "Reception", "Revue")


def install_scenarios():
    documents = Path(settings.MEDIA_ROOT) / "fictif-aizy"
    if Restaurant.all_objects.exists():
        if not documents.is_dir():
            raise RuntimeError("Base peuplée mais pièces fictives absentes ; réparation manuelle requise.")
        print("Scénarios fictifs déjà présents ; aucune donnée remplacée.")
        return
    if documents.exists():
        raise RuntimeError("Pièces fictives déjà présentes sans données ; aucun écrasement.")
    spec = importlib.util.spec_from_file_location("aizy_demo_importer", PACKAGE / "importer.py")
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    importer.run()


def _credentials_path():
    return Path(os.getenv("AIZY_DEMO_CREDENTIALS_FILE", "/data/demo-credentials.json"))


def _read_or_create_credentials(path):
    if path.exists():
        credentials = json.loads(path.read_text(encoding="utf-8"))
        if set(credentials) != set(ACCOUNT_NAMES) or not all(credentials.values()):
            raise RuntimeError("Fichier des accès de démonstration invalide.")
        return credentials
    credentials = {name: secrets.token_urlsafe(18) for name in ACCOUNT_NAMES}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(credentials, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return credentials


def install_accounts():
    User = get_user_model()
    if User.objects.exists():
        print("Comptes existants préservés ; aucun mot de passe modifié.")
        return
    credentials = _read_or_create_credentials(_credentials_path())
    call_command("init_roles", verbosity=0)
    from django.contrib.auth.models import Group

    with transaction.atomic():
        User.objects.create_superuser(username="Superadmin", password=credentials["Superadmin"])
        for name, group_name, is_staff in (
            ("Admin", ROLE_ADMIN, True),
            ("Reception", ROLE_RECEPTION, False),
            ("Revue", ROLE_REVUE, False),
        ):
            user = User.objects.create_user(
                username=name, password=credentials[name], is_staff=is_staff
            )
            user.groups.add(Group.objects.get(name=group_name))
            if group_name in (ROLE_RECEPTION, ROLE_REVUE):
                RestaurantAccess.objects.bulk_create(
                    RestaurantAccess(user=user, restaurant=restaurant)
                    for restaurant in Restaurant.all_objects.all()
                )
    print("Quatre comptes de démonstration créés ; accès dans ./manage.sh credentials.")


if __name__ == "__main__":
    install_scenarios()
    install_accounts()
