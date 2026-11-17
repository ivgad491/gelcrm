from django.urls import path, include
from django.views.generic import TemplateView
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.static import serve
from django.conf import settings

urlpatterns = [
    path('api/', include('crm.urls')),
    path('sw.js', serve, {'path': 'sw.js', 'document_root': settings.BASE_DIR / 'static'}),
    path('', ensure_csrf_cookie(TemplateView.as_view(template_name='index.html'))),
]
