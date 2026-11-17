class MutationAuditMiddleware:
    """Record successful writes without storing secrets or uploaded file bodies."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .signals import audit_actor
        token = audit_actor.set(request.user if request.user.is_authenticated else None)
        try:
            response = self.get_response(request)
        finally:
            audit_actor.reset(token)
        if request.path.startswith('/api/'):
            response['Cache-Control'] = 'no-store, private'
            response['Pragma'] = 'no-cache'
        response.setdefault('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https: http: data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
        if (request.path.startswith('/api/') and request.method in ('POST', 'PUT', 'PATCH', 'DELETE')
                and 200 <= response.status_code < 300 and request.user.is_authenticated):
            from .models import AuditEvent
            AuditEvent.objects.create(actor=request.user, action=f'{request.method} {request.path}',
                                      entity='api', details={'status': response.status_code})
        return response


class MaintenanceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings
        from django.http import JsonResponse
        if request.path.startswith('/api/') and not request.path.startswith('/api/backups/'):
            if (settings.BACKUP_DIR / '.restore-in-progress').exists():
                return JsonResponse({'detail': 'Восстановление данных. Повторите запрос позже.'}, status=503)
        return self.get_response(request)
