from rest_framework.permissions import BasePermission

ADMIN_ROLES = {'superadmin', 'admin'}
PRODUCTION_ROLES = {'colorist', 'filler', 'labeler', 'picker', 'dispatcher'}
STAGE_ROLE = {'color': 'colorist', 'fill': 'filler', 'label': 'labeler', 'pick': 'picker', 'ship': 'dispatcher'}
ROLE_SECTIONS = {
    'superadmin': ['dashboard', 'customers', 'orders', 'production', 'stock', 'catalog', 'recipes', 'finance', 'employees', 'audit', 'backups', 'integrations'],
    'admin': ['dashboard', 'customers', 'orders', 'production', 'stock', 'catalog', 'recipes', 'finance', 'integrations'],
    'manager': ['dashboard', 'customers', 'orders', 'catalog'],
    'colorist': ['dashboard', 'production', 'recipes', 'stock'],
    'filler': ['dashboard', 'production', 'stock'],
    'labeler': ['dashboard', 'production', 'stock'],
    'picker': ['dashboard', 'production', 'stock', 'catalog'],
    'dispatcher': ['dashboard', 'production'],
    'accountant': ['dashboard', 'orders', 'finance'],
    'analyst': ['dashboard', 'orders', 'stock', 'catalog', 'finance'],
}

class IsSuperadmin(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == 'superadmin'
