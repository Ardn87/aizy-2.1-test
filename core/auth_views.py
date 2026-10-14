import hashlib
from datetime import timedelta
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth import get_user_model
from django import forms
from django.contrib import messages
from django.views.generic import UpdateView
from django.utils import timezone
from django.db import transaction
from django.urls import reverse_lazy
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from .models import LoginAttempt
from .forms import apply_bootstrap_classes

class SafeLoginView(LoginView):
    template_name = 'registration/login.html'
    redirect_authenticated_user = True
    def get_success_url(self):
        from .access import restricted_section
        from django.urls import reverse
        section = restricted_section(self.request.user)
        if section:
            return reverse('document_deposit' if section == 'deposit' else 'price_review')
        return super().get_success_url()

    def post(self, request, *args, **kwargs):
        address = request.META.get('REMOTE_ADDR', '')
        keys = [hashlib.sha256(v.encode()).hexdigest() for v in ('ip:' + address, 'account:' + request.POST.get('username', '').casefold())]
        now = timezone.now()
        with transaction.atomic():
            attempts = []
            for key in keys:
                item, _ = LoginAttempt.objects.get_or_create(key=key, defaults={'window_start':now})
                item = LoginAttempt.objects.select_for_update().get(pk=item.pk)
                if now - item.window_start >= timedelta(minutes=10):
                    item.window_start = now; item.failures = 0; item.save()
                attempts.append(item)
            if any(a.failures >= 8 for a in attempts):
                form = self.get_form(); form.add_error(None, 'Trop de tentatives. Réessayez dans quelques minutes.')
                form.rate_limited=True
                return self.render_to_response(self.get_context_data(form=form), status=429)
            response = super().post(request, *args, **kwargs)
            if response.status_code in (301,302):
                # Preserve the IP counter so success cannot reset attacks against other users.
                LoginAttempt.objects.filter(key=keys[1]).delete()
            else:
                for a in attempts:
                    a.failures += 1; a.save(update_fields=['failures'])
            return response

class OwnPasswordForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs); apply_bootstrap_classes(self.fields)

@method_decorator(login_required, name='dispatch')
class OwnPasswordView(PasswordChangeView):
    template_name = 'core/own_password.html'
    form_class = OwnPasswordForm
    success_url = reverse_lazy('home')


class OwnProfileForm(forms.ModelForm):
    class Meta:
        model = get_user_model()
        fields = ('username', 'first_name', 'last_name', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_bootstrap_classes(self.fields)


@method_decorator(login_required, name='dispatch')
class OwnProfileView(UpdateView):
    template_name = 'core/own_profile.html'
    form_class = OwnProfileForm
    success_url = reverse_lazy('own_profile')

    def get_object(self, queryset=None):
        return self.request.user

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, 'Votre profil a été mis à jour.')
        return response
