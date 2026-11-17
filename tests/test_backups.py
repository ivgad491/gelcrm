from datetime import timedelta
import hashlib
from pathlib import Path
import os
import subprocess
import sys
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import override_settings
from django.utils import timezone
import pytest

from crm.backups import (
    INTERVAL, _archive_path, _validated_dump, backup_status, check_backup_due,
    create_backup, queue_backup, restore_backup,
)
from crm.models import BackupRecord, Customer, User


@pytest.fixture
def backup_settings(settings, tmp_path):
    settings.BACKUP_ENCRYPTION_KEY = Fernet.generate_key().decode()
    settings.BACKUP_DIR = tmp_path / 'backups'
    settings.BACKUP_RETENTION = 2
    return settings


@pytest.fixture
def owner(db):
    return User.objects.create_user(username='backup-owner', password='BackupPass2026!', role='superadmin')


@pytest.mark.django_db(transaction=True)
def test_encrypted_snapshot_is_validated_and_not_plaintext(backup_settings, owner, tmp_path):
    Customer.objects.create(name='Секретный клиент', phone='+79991111111')
    record = BackupRecord.objects.create(requested_by=owner)
    create_backup(record.pk)
    record.refresh_from_db()
    assert record.status == 'success'
    encrypted = _archive_path(record).read_bytes()
    assert not encrypted.startswith(b'SQLite format')
    assert 'Секретный клиент'.encode() not in encrypted
    assert hashlib.sha256(encrypted).hexdigest() == record.sha256
    manifest = _validated_dump(record, tmp_path / 'database.dump')
    assert manifest['engine'] == 'sqlite'
    assert manifest['backup_id'] == record.pk
    assert (tmp_path / 'database.dump').read_bytes().startswith(b'SQLite format 3')


@pytest.mark.django_db(transaction=True)
def test_tampered_archive_is_rejected_before_any_restore(backup_settings, owner, tmp_path):
    record = BackupRecord.objects.create(requested_by=owner)
    create_backup(record.pk)
    record.refresh_from_db()
    path = _archive_path(record)
    path.write_bytes(path.read_bytes() + b'tamper')
    before = BackupRecord.objects.count()
    with pytest.raises(ValueError, match='Контрольная сумма'):
        restore_backup(record, owner)
    assert BackupRecord.objects.count() == before
    assert not (Path(backup_settings.BACKUP_DIR) / '.restore-in-progress').exists()


@pytest.mark.django_db(transaction=True)
def test_valid_archive_wrong_key_is_rejected(backup_settings, owner, tmp_path):
    record = BackupRecord.objects.create(requested_by=owner)
    create_backup(record.pk)
    record.refresh_from_db()
    with override_settings(BACKUP_ENCRYPTION_KEY=Fernet.generate_key().decode()):
        with pytest.raises(ValueError, match='другим ключом'):
            _validated_dump(record, tmp_path / 'out.dump')


@pytest.mark.django_db(transaction=True)
def test_retention_keeps_previous_copies_and_marks_pruned_records(backup_settings, owner):
    records = []
    for _ in range(3):
        record = BackupRecord.objects.create(requested_by=owner)
        create_backup(record.pk)
        record.refresh_from_db()
        records.append(record)
    records[0].refresh_from_db()
    assert records[0].status == 'failed'
    assert 'политике хранения' in records[0].error
    assert not _archive_path(records[0]).exists()
    assert _archive_path(records[1]).exists()
    assert _archive_path(records[2]).exists()


@pytest.mark.django_db
def test_scheduler_uses_last_success_not_last_attempt_or_manual_request(backup_settings):
    completed = timezone.now() - timedelta(hours=11)
    BackupRecord.objects.create(status='success', completed_at=completed)
    BackupRecord.objects.create(status='failed', completed_at=timezone.now())
    with patch('crm.backups.queue_backup') as queued:
        assert check_backup_due() is None
        queued.assert_not_called()
    assert backup_status()['next_due'] == completed + INTERVAL
    BackupRecord.objects.filter(status='success').update(completed_at=timezone.now() - timedelta(hours=13))
    with patch('crm.backups.queue_backup', return_value='queued') as queued:
        assert check_backup_due() == 'queued'
        queued.assert_called_once_with()


@pytest.mark.django_db
def test_missing_key_disables_backups_and_never_claims_success(settings):
    settings.BACKUP_ENCRYPTION_KEY = ''
    assert backup_status()['enabled'] is False
    assert check_backup_due() is None
    with pytest.raises(ValueError, match='отключены'):
        queue_backup()
    assert not BackupRecord.objects.exists()


@pytest.mark.django_db
def test_queue_failure_notifies_administrators_and_records_failure(backup_settings, owner):
    from crm.models import Notification
    with patch('crm.tasks.create_backup_task.delay', side_effect=OSError('offline')):
        with pytest.raises(ValueError, match='Очередь недоступна'):
            queue_backup(owner)
    record = BackupRecord.objects.get()
    assert record.status == 'failed'
    assert record.completed_at is not None
    assert Notification.objects.filter(user=owner, title='Ошибка резервного копирования').exists()


@pytest.mark.django_db
def test_only_superadmin_may_restore(backup_settings):
    admin = User.objects.create_user(username='limited', role='admin')
    record = BackupRecord.objects.create(status='success')
    with pytest.raises(ValueError, match='главный администратор'):
        restore_backup(record, admin)


@pytest.mark.django_db
def test_archive_path_traversal_is_rejected(backup_settings):
    record = BackupRecord(filename='../stolen.gelbackup', status='success')
    with pytest.raises(ValueError, match='Недопустимое имя'):
        _archive_path(record)


def test_complete_restore_on_isolated_disk_database(tmp_path):
    """Use a separate process so a restore cannot overwrite pytest's own DB."""
    script = r'''
import os
from pathlib import Path
from cryptography.fernet import Fernet
from django.conf import settings
root = Path(os.environ['RESTORE_TEST_DIRECTORY'])
settings.DATABASES['default'] = {'ENGINE': 'django.db.backends.sqlite3', 'NAME': root / 'test.sqlite3'}
settings.BACKUP_DIR = root / 'backups'
settings.BACKUP_ENCRYPTION_KEY = Fernet.generate_key().decode()
settings.BACKUP_RETENTION = 5
import django
django.setup()
from django.core.management import call_command
call_command('migrate', verbosity=0)
from crm.models import User, Customer, BackupRecord, AuditEvent
from django.contrib.sessions.models import Session
from crm.backups import create_backup
owner = User.objects.create_user(username='owner', password='TestBackup2026!', role='superadmin')
Customer.objects.create(name='before snapshot')
record = BackupRecord.objects.create(requested_by=owner)
create_backup(record.pk)
record.refresh_from_db()
Customer.objects.create(name='after snapshot')
Session.objects.create(session_key='obsolete-session', session_data='', expire_date=__import__('django.utils.timezone', fromlist=['now']).now())
restore_user = User.objects.create_user(username='created-after-snapshot', password='RestorePassword2026!', role='superadmin')
from rest_framework.test import APIClient
client = APIClient()
client.force_login(restore_user)
response = client.post('/api/backups/restore/', {
    'backup_id': record.pk, 'password': 'RestorePassword2026!', 'confirmation': 'ВОССТАНОВИТЬ',
}, format='json')
assert response.status_code == 200, response.content
result = response.json()
assert result['restored'] is True
assert Customer.objects.filter(name='before snapshot').exists()
assert not Customer.objects.filter(name='after snapshot').exists()
assert not User.objects.filter(username='created-after-snapshot').exists()
assert not Session.objects.exists()
assert BackupRecord.objects.get(pk=result['safety_backup_id']).status == 'success'
assert AuditEvent.objects.filter(action='backup.restored').exists()
assert AuditEvent.objects.filter(action='backup.restored').get().actor_id is None
assert client.get('/api/me/').json()['user'] is None
assert not (settings.BACKUP_DIR / '.restore-in-progress').exists()
'''
    env = dict(os.environ, DEBUG='true', DJANGO_SETTINGS_MODULE='config.settings',
               RESTORE_TEST_DIRECTORY=str(tmp_path))
    result = subprocess.run([sys.executable, '-c', script], env=env,
                            cwd=Path(__file__).resolve().parent.parent,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
