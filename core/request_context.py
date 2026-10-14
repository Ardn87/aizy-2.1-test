from contextvars import ContextVar

current_user = ContextVar('business_user', default=None)
current_reason = ContextVar('business_reason', default='')

def restaurant_ids(user):
    from .models import RestaurantAccess
    return RestaurantAccess.objects.filter(user=user).values_list('restaurant_id', flat=True)

class BusinessContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
    def __call__(self, request):
        user_token = current_user.set(request.user if request.user.is_authenticated else None)
        reason_token = current_reason.set(request.POST.get('reason', request.POST.get('validation_exception', ''))[:2000] if request.method == 'POST' else '')
        try:
            return self.get_response(request)
        finally:
            current_user.reset(user_token)
            current_reason.reset(reason_token)
