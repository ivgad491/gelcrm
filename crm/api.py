import json
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.utils.decorators import method_decorator
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView, exception_handler
from .models import (User, Customer, Message, Material, Product, Recipe, Order, ProductionTask,
                     FinanceEntry, Notification, AuditEvent, StockMovement, ImportBatch, BackupRecord)
from .permissions import ADMIN_ROLES, STAGE_ROLE, ROLE_SECTIONS, IsSuperadmin
from .serializers import (UserSerializer, CustomerSerializer, MessageSerializer, MaterialSerializer, ProductSerializer,
                          RecipeSerializer, OrderSerializer, TaskSerializer, FinanceSerializer, NotificationSerializer,
                          AuditSerializer, MovementSerializer, AssignmentInput, TaskActionInput, AdjustmentInput, RestoreInput)
from .services import start_order, advance_task, adjust_material, record_finance, notify_roles


class RequestShapeMixin:
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.method in ('POST', 'PUT', 'PATCH') and not isinstance(request.data, Mapping):
            raise ValidationError('Тело запроса должно быть объектом с именованными полями.')


class SafeAPIView(RequestShapeMixin, APIView):
    pass


class SafeGenericViewSet(RequestShapeMixin, viewsets.GenericViewSet):
    pass


def api_exception_handler(exc, context):
    if isinstance(exc, IntegrityError):
        return Response({'detail': 'Нарушено правило целостности: проверьте дубли и значения полей.'}, status=400)
    if isinstance(exc, ValueError):
        return Response({'detail': str(exc)}, status=400)
    return exception_handler(exc, context)


def require_role(user, roles):
    if user.role not in roles:
        raise PermissionDenied('Действие недоступно для вашей роли.')


class LoginThrottle(AnonRateThrottle):
    scope = 'login'
    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


@method_decorator(csrf_protect, name='dispatch')
class LoginView(SafeAPIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginThrottle]
    def post(self, request):
        username = request.data.get('username', '')
        password = request.data.get('password', '')
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValidationError('Укажите логин и пароль.')
        user = authenticate(request, username=username, password=password)
        if not user:
            AuditEvent.objects.create(action='login.failed', entity='auth', details={})
            return Response({'detail': 'Неверный логин или пароль.'}, status=400)
        login(request, user)
        AuditEvent.objects.create(actor=user, action='login.success', entity='auth')
        return Response({'user': UserSerializer(user).data, 'permissions': ROLE_SECTIONS[user.role]})


class LogoutView(SafeAPIView):
    def post(self, request):
        AuditEvent.objects.create(actor=request.user, action='logout', entity='auth')
        logout(request)
        return Response({'detail': 'Вы вышли из системы.'})


@method_decorator(ensure_csrf_cookie, name='dispatch')
class MeView(SafeAPIView):
    permission_classes = [AllowAny]
    def get(self, request):
        if not request.user.is_authenticated:
            return Response({'user': None, 'permissions': []})
        return Response({'user': UserSerializer(request.user).data, 'permissions': ROLE_SECTIONS[request.user.role]})


class HealthView(SafeAPIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    def get(self, request):
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        return Response({'status': 'ok'})


class ReadCreateViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, SafeGenericViewSet):
    pass


class CustomerViewSet(ReadCreateViewSet, mixins.UpdateModelMixin):
    serializer_class = CustomerSerializer
    def get_queryset(self):
        require_role(self.request.user, ADMIN_ROLES | {'manager'})
        qs = Customer.objects.select_related('manager')
        if self.request.user.role == 'manager':
            # Unassigned customers are visible as a queue, but cannot be edited or messaged by managers.
            qs = qs.filter(Q(manager=self.request.user) | Q(manager__isnull=True))
        query = self.request.query_params.get('search')
        if query:
            qs = qs.filter(Q(name__icontains=query) | Q(phone__icontains=query) | Q(email__icontains=query))
        return qs
    def perform_create(self, serializer):
        require_role(self.request.user, ADMIN_ROLES | {'manager'})
        serializer.save()  # New contacts always enter the common queue.
        notify_roles(ADMIN_ROLES, 'Новое обращение в очереди', serializer.instance.name)
    def perform_update(self, serializer):
        if self.request.user.role == 'manager' and serializer.instance.manager_id != self.request.user.pk:
            raise PermissionDenied('Клиент ещё не назначен вам.')
        serializer.save()
    @action(detail=True, methods=['post'])
    def assign(self, request, pk=None):
        require_role(request.user, ADMIN_ROLES)
        inputs = AssignmentInput(data=request.data)
        inputs.is_valid(raise_exception=True)
        with transaction.atomic():
            customer = Customer.objects.select_for_update().get(pk=self.get_object().pk)
            manager_id = inputs.validated_data['manager_id']
            manager = None
            if manager_id is not None:
                manager = get_object_or_404(User, pk=manager_id, role='manager', is_active=True)
            customer.manager = manager
            customer.save()
            # Ownership follows the customer, including historical orders.
            for order in customer.orders.select_for_update():
                order.manager = manager
                order.save(update_fields=['manager', 'updated_at'])
            if manager:
                Notification.objects.create(user=manager, title='Вам назначен клиент', text=customer.name)
        return Response(self.get_serializer(customer).data)
    @action(detail=True, methods=['get', 'post'])
    def messages(self, request, pk=None):
        customer = self.get_object()
        if request.user.role == 'manager' and customer.manager_id != request.user.pk:
            raise PermissionDenied('Переписка доступна после назначения клиента.')
        if request.method == 'GET':
            return Response(MessageSerializer(customer.messages.select_related('author').all(), many=True).data)
        serializer = MessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if customer.channel != 'manual':
            raise ValidationError('Отправка во внешний канал пока не подключена. Добавьте запись как внутренний комментарий через клиента с каналом manual.')
        serializer.save(customer=customer, author=request.user, channel='manual')
        if customer.manager_id and customer.manager_id != request.user.pk:
            Notification.objects.create(user_id=customer.manager_id, title='Новое сообщение в карточке клиента', text=customer.name)
        return Response(serializer.data, status=201)


class OrderViewSet(ReadCreateViewSet):
    serializer_class = OrderSerializer
    def get_queryset(self):
        require_role(self.request.user, ADMIN_ROLES | {'manager', 'accountant', 'analyst'})
        qs = Order.objects.select_related('customer', 'manager', 'product', 'recipe').prefetch_related('finance_entries')
        if self.request.user.role == 'manager':
            qs = qs.filter(manager=self.request.user)
        return qs
    @transaction.atomic
    def perform_create(self, serializer):
        require_role(self.request.user, ADMIN_ROLES | {'manager'})
        customer = Customer.objects.select_for_update().get(pk=serializer.validated_data['customer'].pk)
        if not customer.manager_id:
            raise ValidationError('Сначала администратор должен назначить менеджера клиенту.')
        if self.request.user.role == 'manager' and customer.manager_id != self.request.user.pk:
            raise PermissionDenied('Клиент передан другому менеджеру.')
        serializer.save(manager=customer.manager)
    @action(detail=True, methods=['post'])
    def start(self, request, pk=None):
        order = start_order(self.get_object().pk, request.user)
        return Response(self.get_serializer(order).data)


class TaskViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, SafeGenericViewSet):
    serializer_class = TaskSerializer
    def get_queryset(self):
        require_role(self.request.user, ADMIN_ROLES | set(STAGE_ROLE.values()))
        qs = ProductionTask.objects.select_related('order__customer', 'assigned_to')
        if self.request.user.role not in ADMIN_ROLES:
            qs = qs.filter(stage=next(stage for stage, role in STAGE_ROLE.items() if role == self.request.user.role))
        return qs
    @action(detail=True, methods=['post'])
    def advance(self, request, pk=None):
        inputs = TaskActionInput(data=request.data)
        inputs.is_valid(raise_exception=True)
        task = advance_task(self.get_object().pk, request.user, **inputs.validated_data)
        return Response(self.get_serializer(task).data)


class MaterialViewSet(ReadCreateViewSet):
    serializer_class = MaterialSerializer
    def get_queryset(self):
        require_role(self.request.user, ADMIN_ROLES | {'colorist', 'filler', 'labeler', 'picker', 'analyst'})
        return Material.objects.all()
    def perform_create(self, serializer):
        require_role(self.request.user, ADMIN_ROLES)
        serializer.save(quantity=0)
    @action(detail=True, methods=['post'])
    def adjust(self, request, pk=None):
        require_role(request.user, ADMIN_ROLES)
        inputs = AdjustmentInput(data=request.data)
        inputs.is_valid(raise_exception=True)
        material = adjust_material(self.get_object().pk, user=request.user, **inputs.validated_data)
        return Response(self.get_serializer(material).data)


class ProductViewSet(ReadCreateViewSet):
    serializer_class = ProductSerializer
    def get_queryset(self):
        require_role(self.request.user, ADMIN_ROLES | {'manager', 'picker', 'analyst'})
        return Product.objects.all()
    def perform_create(self, serializer):
        require_role(self.request.user, ADMIN_ROLES)
        serializer.save(stock=0)
    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        require_role(request.user, ADMIN_ROLES | {'analyst'})
        return Response(list(self.get_object().catalog_history.values()))


class RecipeViewSet(ReadCreateViewSet):
    serializer_class = RecipeSerializer
    def get_queryset(self):
        # Managers need a recipe selector when creating a custom order.
        require_role(self.request.user, ADMIN_ROLES | {'colorist', 'manager'})
        return Recipe.objects.prefetch_related('components__material')
    @transaction.atomic
    def perform_create(self, serializer):
        require_role(self.request.user, ADMIN_ROLES | {'colorist'})
        serializer.save()


class FinanceViewSet(ReadCreateViewSet):
    serializer_class = FinanceSerializer
    def get_queryset(self):
        require_role(self.request.user, ADMIN_ROLES | {'accountant', 'analyst'})
        return FinanceEntry.objects.select_related('order', 'actor')
    def perform_create(self, serializer):
        require_role(self.request.user, ADMIN_ROLES | {'accountant'})
        serializer.instance = record_finance(serializer.validated_data, self.request.user)


class EmployeeViewSet(ReadCreateViewSet, mixins.UpdateModelMixin):
    permission_classes = [IsSuperadmin]
    serializer_class = UserSerializer
    queryset = User.objects.order_by('id')


class AuditViewSet(mixins.ListModelMixin, SafeGenericViewSet):
    permission_classes = [IsSuperadmin]
    serializer_class = AuditSerializer
    queryset = AuditEvent.objects.select_related('actor').all()[:1000]


class MovementViewSet(mixins.ListModelMixin, SafeGenericViewSet):
    serializer_class = MovementSerializer
    def get_queryset(self):
        require_role(self.request.user, ADMIN_ROLES | {'analyst'})
        return StockMovement.objects.all()[:1000]


class NotificationViewSet(mixins.ListModelMixin, SafeGenericViewSet):
    serializer_class = NotificationSerializer
    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user)[:200]
    @action(detail=True, methods=['post'])
    def read(self, request, pk=None):
        notification = get_object_or_404(Notification, pk=pk, user=request.user)
        notification.read = True
        notification.save(update_fields=['read'])
        return Response(self.get_serializer(notification).data)


class DashboardView(SafeAPIView):
    def get(self, request):
        orders = Order.objects.select_related('customer', 'product', 'manager').prefetch_related('finance_entries')
        customers = Customer.objects.all()
        if request.user.role == 'manager':
            orders = orders.filter(manager=request.user)
            customers = customers.filter(manager=request.user)
        tasks = ProductionTask.objects.all()
        if request.user.role == 'manager':
            tasks = tasks.filter(order__manager=request.user)
        if request.user.role in STAGE_ROLE.values():
            stage = next(k for k, v in STAGE_ROLE.items() if v == request.user.role)
            tasks = tasks.filter(stage=stage)
        overdue = tasks.filter(status__in=['pending', 'accepted', 'rejected'], updated_at__lt=timezone.now()-timedelta(hours=24)).count()
        finance_access = request.user.role in ADMIN_ROLES | {'accountant', 'analyst'}
        ledger = FinanceEntry.objects.all() if finance_access else FinanceEntry.objects.filter(order__in=orders) if request.user.role == 'manager' else FinanceEntry.objects.none()
        revenue = sum((x.amount if x.kind == 'payment' else -x.amount if x.kind in ('refund', 'expense') else Decimal('0') for x in ledger), Decimal('0'))
        low_stock = Material.objects.filter(quantity__lte=F('min_quantity'))
        stock_access = request.user.role in ADMIN_ROLES | {'colorist', 'filler', 'labeler', 'picker', 'analyst'}
        recent = orders[:6] if request.user.role in ADMIN_ROLES | {'manager', 'accountant', 'analyst'} else []
        return Response({'stats': {'active_orders': orders.exclude(status__in=['shipped', 'cancelled']).count(),
                                  'customers': customers.count() if request.user.role in ADMIN_ROLES | {'manager'} else 0,
                                  'revenue': str(revenue), 'overdue_tasks': overdue,
                                  'stock_alerts': low_stock.count() if stock_access else 0},
                         'stage_counts': list(tasks.exclude(status='completed').values('stage').annotate(count=Count('id'))),
                         'recent_orders': OrderSerializer(recent, many=True).data,
                         'low_stock': MaterialSerializer(low_stock, many=True).data if stock_access else []})


class ManagersView(SafeAPIView):
    def get(self, request):
        require_role(request.user, ADMIN_ROLES)
        return Response(list(User.objects.filter(role='manager', is_active=True).values('id', 'first_name', 'username', 'role')))


class IntegrationsView(SafeAPIView):
    def get(self, request):
        require_role(request.user, ADMIN_ROLES)
        from .integrations import integration_status
        return Response(integration_status())


class ImportPreviewView(SafeAPIView):
    def post(self, request):
        require_role(request.user, ADMIN_ROLES)
        from .importing import preview_import
        upload = request.FILES.get('file')
        if not upload:
            raise ValidationError('Выберите файл CSV, XLSX или XML.')
        mapping = request.data.get('mapping')
        if isinstance(mapping, str):
            try:
                mapping = json.loads(mapping)
            except json.JSONDecodeError:
                raise ValidationError('Сопоставление полей должно быть JSON-объектом.')
        batch = preview_import(upload, request.user, mapping=mapping)
        return Response({'id': batch.pk, 'filename': batch.filename, 'columns': batch.columns,
                         'rows': batch.rows[:100], 'errors': batch.errors, 'mapping': batch.mapping,
                         'row_count': len(batch.rows)})


class ImportApplyView(SafeAPIView):
    def post(self, request, pk):
        require_role(request.user, ADMIN_ROLES)
        from .importing import apply_import
        batch = get_object_or_404(ImportBatch, pk=pk)
        return Response(apply_import(batch, request.user, mapping=request.data.get('mapping')))


class BackupsView(SafeAPIView):
    permission_classes = [IsSuperadmin]
    def get(self, request):
        from .backups import backup_status
        return Response(backup_status())


class BackupCreateView(SafeAPIView):
    permission_classes = [IsSuperadmin]
    def post(self, request):
        from .backups import queue_backup
        record = queue_backup(request.user)
        return Response({'id': record.pk, 'status': record.status}, status=202)


class BackupRestoreView(SafeAPIView):
    permission_classes = [IsSuperadmin]
    def post(self, request):
        inputs = RestoreInput(data=request.data)
        inputs.is_valid(raise_exception=True)
        data = inputs.validated_data
        if data['confirmation'] != 'ВОССТАНОВИТЬ':
            raise ValidationError('Для подтверждения введите ВОССТАНОВИТЬ.')
        password = data['password']
        if not isinstance(password, str) or not request.user.check_password(password):
            raise ValidationError('Неверный текущий пароль.')
        record = get_object_or_404(BackupRecord, pk=data['backup_id'])
        from .backups import restore_backup
        from .signals import audit_actor
        token = audit_actor.set(None)
        try:
            result = restore_backup(record, request.user)
        finally:
            audit_actor.reset(token)
        # All old sessions are invalid after restore; no stale user audit writes.
        request.user = AnonymousUser()
        request.session.flush()
        return Response(result)
