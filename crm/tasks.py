from datetime import timedelta

from celery import shared_task
from django.utils import timezone


@shared_task(name='crm.tasks.create_backup_task', ignore_result=True)
def create_backup_task(record_id):
    from .backups import create_backup
    create_backup(record_id)


@shared_task(name='crm.tasks.periodic_maintenance', ignore_result=True)
def periodic_maintenance():
    from .backups import check_backup_due
    from .models import Notification, ProductionTask, User
    check_backup_due()
    late = ProductionTask.objects.filter(
        status__in=['pending', 'accepted', 'rejected'],
        updated_at__lt=timezone.now() - timedelta(hours=24))
    for task in late:
        title = f'Задержка производства: задание №{task.pk}'
        for user in User.objects.filter(is_active=True, role__in=['superadmin', 'admin']):
            if not Notification.objects.filter(user=user, title=title, created_at__gte=timezone.now() - timedelta(hours=12)).exists():
                Notification.objects.create(user=user, title=title,
                                            text=f'Заказ №{task.order_id}, этап {task.get_stage_display()}: более 24 часов без движения.')
