from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from .access import user_can_access_admin


class RoleAdminAuthenticationForm(AdminAuthenticationForm):
    def confirm_login_allowed(self, user):
        AuthenticationForm.confirm_login_allowed(self, user)
        if not user_can_access_admin(user):
            raise ValidationError(
                "Veuillez utiliser un compte avec le rôle Admin.",
                code="invalid_login",
            )


def configure_admin_site(admin_site):
    admin_site.login_form = RoleAdminAuthenticationForm
    def login(request, extra_context=None):
        from .auth_views import SafeLoginView
        return SafeLoginView.as_view(authentication_form=RoleAdminAuthenticationForm,
            redirect_authenticated_user=False)(request)
    admin_site.login = login
    admin_site.site_header = "Administration Factures"
    admin_site.site_title = "Administration Factures"
    admin_site.index_title = "Gestion de l'application"

    def has_permission(request):
        return user_can_access_admin(request.user)

    admin_site.has_permission = has_permission
