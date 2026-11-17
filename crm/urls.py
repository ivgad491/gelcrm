from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import api

router = DefaultRouter()
for prefix, view in [('customers', api.CustomerViewSet), ('orders', api.OrderViewSet), ('tasks', api.TaskViewSet),
                     ('materials', api.MaterialViewSet), ('products', api.ProductViewSet), ('recipes', api.RecipeViewSet),
                     ('finance', api.FinanceViewSet), ('employees', api.EmployeeViewSet), ('audit', api.AuditViewSet),
                     ('notifications', api.NotificationViewSet), ('movements', api.MovementViewSet)]:
    router.register(prefix, view, basename=prefix)

urlpatterns = [path('', include(router.urls)), path('me/', api.MeView.as_view()),
               path('login/', api.LoginView.as_view()), path('logout/', api.LogoutView.as_view()),
               path('health/', api.HealthView.as_view()), path('dashboard/', api.DashboardView.as_view()),
               path('managers/', api.ManagersView.as_view()),
               path('integrations/', api.IntegrationsView.as_view()),
               path('imports/preview/', api.ImportPreviewView.as_view()), path('imports/<int:pk>/apply/', api.ImportApplyView.as_view()),
               path('backups/', api.BackupsView.as_view()), path('backups/create/', api.BackupCreateView.as_view()),
               path('backups/restore/', api.BackupRestoreView.as_view())]
