"""Transactional domain operations. API clients cannot bypass these state checks."""
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from .models import (User, Order, Material, Product, ProductionTask, StockMovement,
                     Notification, FinanceEntry, AuditEvent, Recipe)
from .permissions import ADMIN_ROLES, STAGE_ROLE


def notify_roles(roles, title, text=''):
    for user in User.objects.filter(role__in=roles, is_active=True):
        Notification.objects.create(user=user, title=title, text=text)


def decimal_quantity(value):
    try:
        value = Decimal(str(value))
        if not value.is_finite() or value == 0 or abs(value) >= Decimal('100000000000') or value.as_tuple().exponent < -3:
            raise ValueError
        return value
    except (ValueError, InvalidOperation, TypeError):
        raise ValidationError('Укажите ненулевое количество: не более 3 знаков после запятой.')


def _material_movement(material, delta, user, reason, order=None):
    if material.quantity + delta < 0:
        raise ValidationError(f'Недостаточно материала «{material.name}»: {material.quantity} {material.unit}.')
    if material.quantity + delta >= Decimal('100000000000'):
        raise ValidationError('Остаток превышает максимально допустимое значение.')
    material.quantity += delta
    material.save(update_fields=['quantity'])
    StockMovement.objects.create(material=material, quantity=delta, reason=reason, actor=user, order=order)
    if material.quantity <= material.min_quantity:
        notify_roles(ADMIN_ROLES, 'Низкий остаток на складе', f'{material.name}: {material.quantity} {material.unit}')


@transaction.atomic
def adjust_material(material_id, quantity, reason, user):
    delta = decimal_quantity(quantity)
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError('Укажите основание складской операции.')
    material = Material.objects.select_for_update().get(pk=material_id)
    _material_movement(material, delta, user, str(reason)[:500])
    return material


@transaction.atomic
def start_order(order_id, user):
    order = Order.objects.select_for_update().get(pk=order_id)
    if user.role not in ADMIN_ROLES and (user.role != 'manager' or order.manager_id != user.pk):
        raise PermissionDenied('Недостаточно прав для запуска этого заказа.')
    if order.status != 'draft' or order.tasks.exists():
        raise ValidationError('Заказ уже запущен или закрыт.')
    if order.kind == 'standard':
        product = Product.objects.select_for_update().get(pk=order.product_id)
        if product.stock < order.quantity:
            raise ValidationError('Недостаточно готового товара. Выпустите партию как индивидуальный заказ по рецептуре.')
        product.stock -= order.quantity
        product.save(update_fields=['stock', 'updated_at'])
        StockMovement.objects.create(product=product, order=order, quantity=-order.quantity,
                                     reason='Списание готового товара в заказ', actor=user)
        stages = ['pick', 'ship']
    else:
        if not order.recipe_id:
            raise ValidationError('Для колеровки нужна рецептура.')
        # Freeze the recipe reference for the lifetime of this order; recipes are immutable through API.
        Recipe.objects.select_for_update().get(pk=order.recipe_id)
        components = list(order.recipe.components.order_by('material_id'))
        if not components:
            raise ValidationError('В рецептуре нет материалов.')
        for component in components:
            material = Material.objects.select_for_update().get(pk=component.material_id)
            _material_movement(material, -component.quantity * order.quantity, user,
                               'Выдача материалов по рецептуре в производство', order)
        stages = ['color', 'fill', 'label', 'pick', 'ship']
    for i, stage in enumerate(stages):
        ProductionTask.objects.create(order=order, stage=stage, status='pending' if i == 0 else 'waiting')
    order.status = 'production'
    order.save(update_fields=['status', 'updated_at'])
    notify_roles([STAGE_ROLE[stages[0]]], 'Новое задание', f'Заказ #{order.pk}, количество {order.quantity}')
    return order


@transaction.atomic
def advance_task(task_id, user, action, note=''):
    # Always lock order first, then tasks. A consistent order prevents cross-stage deadlocks.
    if not isinstance(note, str):
        raise ValidationError('Комментарий контроля качества должен быть текстом.')
    order_id = ProductionTask.objects.values_list('order_id', flat=True).get(pk=task_id)
    order = Order.objects.select_for_update().get(pk=order_id)
    task = ProductionTask.objects.select_for_update().get(pk=task_id)
    if user.role not in ADMIN_ROLES and user.role != STAGE_ROLE[task.stage]:
        raise PermissionDenied('Задание относится к другому отделу.')
    if order.status not in ('production', 'ready'):
        raise ValidationError('Заказ не находится в производстве.')
    if action == 'accept':
        if task.status not in ('pending', 'rejected'):
            raise ValidationError('Задание ещё не передано или уже принято.')
        task.status = 'accepted'
        task.assigned_to = user
    elif action in ('complete', 'reject'):
        if task.status != 'accepted':
            raise ValidationError('Сначала примите задание в работу.')
        if task.assigned_to_id != user.pk and user.role not in ADMIN_ROLES:
            raise PermissionDenied('Задание принято другим сотрудником.')
        if not note.strip():
            raise ValidationError('Укажите результат контроля качества или причину возврата.')
        if action == 'reject':
            task.status = 'rejected'
            task.assigned_to = None
            notify_roles(ADMIN_ROLES | {STAGE_ROLE[task.stage]}, 'Возврат на доработку', f'Заказ #{order.pk}: {note}')
        else:
            task.status = 'completed'
            stages = ['color', 'fill', 'label', 'pick', 'ship']
            next_task = (ProductionTask.objects.filter(order=order, status='waiting', stage__in=stages[stages.index(task.stage)+1:])
                         .order_by('pk').first())
            if next_task:
                next_task.status = 'pending'
                next_task.save(update_fields=['status', 'updated_at'])
                notify_roles([STAGE_ROLE[next_task.stage]], 'Задание передано в отдел', f'Заказ #{order.pk}; приёмка: {note}')
            if task.stage == 'pick':
                order.status = 'ready'
            if task.stage == 'ship':
                order.status = 'shipped'
                if order.manager_id:
                    Notification.objects.create(user_id=order.manager_id, title='Заказ отправлен', text=f'Заказ #{order.pk}')
            order.save(update_fields=['status', 'updated_at'])
    else:
        raise ValidationError('Неизвестное действие задания.')
    task.note = str(note)[:5000]
    task.save()
    return task


@transaction.atomic
def record_finance(data, user):
    order = data.get('order')
    if order:
        order = Order.objects.select_for_update().get(pk=order.pk)
        ledger = list(order.finance_entries.all())
        net = sum((entry.amount if entry.kind == 'payment' else -entry.amount if entry.kind == 'refund' else Decimal('0')
                   for entry in ledger), Decimal('0'))
        if data['kind'] == 'refund' and data['amount'] > net:
            raise ValidationError('Возврат превышает сумму полученных оплат с учётом предыдущих возвратов.')
        if data['kind'] == 'payment' and data['amount'] + net > order.total:
            raise ValidationError('Оплата превышает остаток долга по заказу.')
    return FinanceEntry.objects.create(actor=user, **data)
