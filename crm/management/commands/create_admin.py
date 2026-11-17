"""Create the first privileged account without loading demonstration data."""
import getpass
import os
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from crm.models import User

class Command(BaseCommand):
    help = 'Создать главного администратора без email и демонстрационных данных.'
    def add_arguments(self, parser):
        parser.add_argument('--username', default='owner')
    def handle(self, *args, **options):
        username = options['username']
        if User.objects.filter(username=username).exists():
            raise CommandError('Логин уже существует; пароль и роль не изменены.')
        password = os.getenv('CRM_ADMIN_PASSWORD')
        if not password:
            password = getpass.getpass('Пароль: ')
            if password != getpass.getpass('Повторите пароль: '):
                raise CommandError('Пароли не совпадают.')
        try:
            validate_password(password, User(username=username))
        except ValidationError as exc:
            raise CommandError(' '.join(exc.messages)) from exc
        User.objects.create_user(username=username, password=password, role='superadmin', first_name='Владелец')
        self.stdout.write(self.style.SUCCESS(f'Главный администратор {username} создан.'))
