"""
URL configuration for facture_project.
"""

from django.contrib import admin
from django.urls import include, path

from core.admin_access import configure_admin_site


configure_admin_site(admin.site)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("core.urls")),
]
