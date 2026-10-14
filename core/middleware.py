from django.core.exceptions import PermissionDenied

from .access import user_can_access_admin


class AdminRoleRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        is_admin_path = path == "/admin" or path.startswith("/admin/")
        if is_admin_path and request.user.is_authenticated and not user_can_access_admin(request.user):
            raise PermissionDenied
        return self.get_response(request)


class SectionAccessMiddleware:
    """Vérification des liens directs et des POST, indépendante du menu."""
    COMMON = {'home', 'login', 'logout', 'own_password', 'own_profile'}
    SECTIONS = {
        'deposit': {'document_deposit', 'delivery_feedback'},
        'review': {'price_review', 'review_action', 'review_undo', 'review_invoice_file'},
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        from .access import restricted_section
        section = restricted_section(request.user)
        if section and request.resolver_match.url_name not in self.COMMON | self.SECTIONS[section]:
            raise PermissionDenied
