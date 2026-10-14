from functools import wraps

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


ROLE_RECEPTION = "Reception"
ROLE_REVUE = "Revue"
ROLE_GESTIONNAIRE = "Gestionnaire"
ROLE_ADMIN = "Admin"


DEFAULT_ROLES = (ROLE_RECEPTION, ROLE_REVUE, ROLE_GESTIONNAIRE, ROLE_ADMIN)


def user_has_role(user, *role_names):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name__in=role_names).exists()


def user_can_access_admin(user):
    if not user.is_authenticated or not user.is_active:
        return False
    return user.is_superuser or user_has_role(user, ROLE_ADMIN)


def role_required(*role_names):
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def wrapped(request, *args, **kwargs):
            if user_has_role(request.user, *role_names):
                return view_func(request, *args, **kwargs)
            raise PermissionDenied

        return wrapped

    return decorator


class RoleRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    required_roles = ()
    raise_exception = True

    def get_required_roles(self):
        return self.required_roles

    def test_func(self):
        return user_has_role(self.request.user, *self.get_required_roles())


def restricted_section(user):
    """Les rôles métier limités restent isolés même si les deux sont attribués."""
    if not user.is_authenticated or user_has_role(user, ROLE_ADMIN, ROLE_GESTIONNAIRE):
        return None
    if user_has_role(user, ROLE_RECEPTION):
        return 'deposit'
    if user_has_role(user, ROLE_REVUE):
        return 'review'
    return None
