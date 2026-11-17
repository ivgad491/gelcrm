"""Authenticated, encrypted database backups. Archives never leave BACKUP_DIR.

The scheduler measures twelve hours from the last *successful* snapshot. Both
manual and scheduled work go through the same lock and Celery task.
"""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import uuid
import zipfile

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.management.color import no_style
from django.db import connection, connections, transaction
from django.utils import timezone

from .models import AuditEvent, BackupRecord, Notification, User

INTERVAL = timedelta(hours=12)
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
LOCK_ID = 714026711


def _cipher():
    key = settings.BACKUP_ENCRYPTION_KEY
    if not key:
        raise ValueError('Резервные копии отключены: задайте BACKUP_ENCRYPTION_KEY.')
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (TypeError, ValueError) as exc:
        raise ValueError('BACKUP_ENCRYPTION_KEY должен быть ключом Fernet.') from exc


def _directory():
    path = Path(settings.BACKUP_DIR).resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@contextmanager
def _operation_lock():
    """Cross-process lock; PostgreSQL additionally protects distributed workers."""
    path = _directory() / '.operation.lock'
    lock_file = path.open('a+b')
    postgres_locked = False
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                lock_file.seek(0)
                if lock_file.read(1) == b'':
                    lock_file.write(b'0')
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise ValueError('Другая операция резервного копирования уже выполняется.') from exc
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s)', [LOCK_ID])
                postgres_locked = cursor.fetchone()[0]
            if not postgres_locked:
                raise ValueError('Другая операция резервного копирования уже выполняется.')
        yield
    finally:
        if postgres_locked and connection.connection is not None:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [LOCK_ID])
        lock_file.close()


def _engine():
    engine = settings.DATABASES['default']['ENGINE']
    if engine.endswith('sqlite3'):
        return 'sqlite'
    if engine.endswith('postgresql'):
        return 'postgresql'
    raise ValueError('Резервные копии поддерживают PostgreSQL и SQLite.')


def _pg_args(program):
    db = settings.DATABASES['default']
    args = [program]
    for setting, option in [('HOST', '--host'), ('PORT', '--port'), ('USER', '--username')]:
        if db.get(setting):
            args.extend([option, str(db[setting])])
    return args


def _pg_env():
    env = os.environ.copy()
    env['PGPASSWORD'] = str(settings.DATABASES['default'].get('PASSWORD', ''))
    env['PGCONNECT_TIMEOUT'] = '15'
    return env


def _run(args):
    try:
        return subprocess.run(args, check=True, capture_output=True, env=_pg_env(), timeout=600)
    except FileNotFoundError as exc:
        raise ValueError(f'Не найдена программа {args[0]}. Установите PostgreSQL client tools.') from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f'{args[0]} превысил время ожидания (10 минут).') from exc
    except subprocess.CalledProcessError as exc:
        # Do not leak connection details or archive content into an API response.
        raise ValueError(f'{args[0]} завершился с ошибкой; проверьте доступ к базе и версию PostgreSQL.') from exc


def _notify_failure(message):
    Notification.objects.bulk_create([
        Notification(user=user, title='Ошибка резервного копирования', text=message[:1000])
        for user in User.objects.filter(is_active=True, role__in=['superadmin', 'admin'])
    ])


def _dump_database(path):
    if _engine() == 'sqlite':
        source = str(settings.DATABASES['default']['NAME'])
        if source == ':memory:' or source.startswith('file:memory'):
            # Django's test SQLite database is a shared in-memory connection.
            connection.ensure_connection()
            with sqlite3.connect(path) as destination:
                connection.connection.backup(destination)
        else:
            with sqlite3.connect(source) as origin, sqlite3.connect(path) as destination:
                origin.backup(destination)
    else:
        args = _pg_args('pg_dump')
        args.extend(['--format=custom', '--no-owner', '--no-privileges', '--file', str(path),
                     '--dbname', str(settings.DATABASES['default']['NAME'])])
        _run(args)


def _write_snapshot(record):
    """Caller owns the operation lock. No transaction spans the database dump."""
    cipher = _cipher()
    directory = _directory()
    BackupRecord.objects.filter(pk=record.pk).update(status='running', error='')
    try:
        with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=directory) as temp:
            dump_path = Path(temp) / 'database.dump'
            _dump_database(dump_path)
            if dump_path.stat().st_size > MAX_ARCHIVE_BYTES:
                raise ValueError('База превышает допустимый размер архива (512 МиБ).')
            payload = dump_path.read_bytes()
            manifest = {'version': 1, 'engine': _engine(), 'backup_id': record.pk,
                        'created_at': timezone.now().isoformat(),
                        'sha256': hashlib.sha256(payload).hexdigest()}
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', json.dumps(manifest))
                archive.writestr('database.dump', payload)
            encrypted = cipher.encrypt(buffer.getvalue())
            filename = f'backup-{record.pk}-{uuid.uuid4().hex}.gelbackup'
            temporary = directory / (filename + '.tmp')
            with temporary.open('xb') as target:
                os.chmod(temporary, 0o600)
                target.write(encrypted)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, directory / filename)
        completed = timezone.now()
        BackupRecord.objects.filter(pk=record.pk).update(
            status='success', filename=filename, sha256=hashlib.sha256(encrypted).hexdigest(),
            completed_at=completed, error='')
        AuditEvent.objects.create(actor=record.requested_by, action='backup.created', entity='backup',
                                  entity_id=str(record.pk), details={'filename': filename})
        record.refresh_from_db()
        return record
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'Не удалось создать резервную копию. Проверьте журнал сервера.'
        BackupRecord.objects.filter(pk=record.pk).update(status='failed', error=message, completed_at=timezone.now())
        _notify_failure(message)
        raise


def _prune(protected=()):
    retention = max(2, int(settings.BACKUP_RETENTION))
    records = list(BackupRecord.objects.filter(status='success').order_by('-completed_at', '-pk'))
    for record in records[retention:]:
        if record.pk in protected:
            continue
        path = _archive_path(record)
        path.unlink(missing_ok=True)
        # Keep an auditable inventory without representing a deleted file as restorable.
        record.status = 'failed'
        record.error = 'Архив удалён по политике хранения.'
        record.save(update_fields=['status', 'error'])


def create_backup(record_id):
    with _operation_lock():
        record = BackupRecord.objects.select_related('requested_by').get(pk=record_id)
        if record.status != 'pending':
            return record
        record = _write_snapshot(record)
        _prune()
        return record


def queue_backup(user=None):
    _cipher()
    with _operation_lock():
        if BackupRecord.objects.filter(status__in=['pending', 'running']).exists():
            raise ValueError('Резервная копия уже ожидает выполнения или создаётся.')
        record = BackupRecord.objects.create(requested_by=user, status='pending')
    try:
        from .tasks import create_backup_task
        create_backup_task.delay(record.pk)
    except Exception as exc:
        message = 'Очередь недоступна. Запустите Redis и Celery worker.'
        BackupRecord.objects.filter(pk=record.pk).update(status='failed', error=message, completed_at=timezone.now())
        _notify_failure(message)
        raise ValueError(message) from exc
    record.refresh_from_db()
    return record


def backup_status():
    try:
        _cipher()
        enabled = True
    except ValueError:
        enabled = False
    last = BackupRecord.objects.filter(status='success').order_by('-completed_at').first()
    return {
        'items': list(BackupRecord.objects.order_by('-created_at').values(
            'id', 'status', 'filename', 'error', 'created_at', 'completed_at')[:100]),
        'last_success': last.completed_at if last else None,
        'next_due': last.completed_at + INTERVAL if last else timezone.now(),
        'enabled': enabled,
    }


def check_backup_due():
    try:
        _cipher()
    except ValueError:
        return None
    # A worker killed between accepting a job and finishing it must not stop
    # the scheduler forever. Never invalidate an operation holding the lock.
    stale = BackupRecord.objects.filter(status__in=['pending', 'running'],
                                       created_at__lt=timezone.now() - timedelta(hours=1))
    if stale.exists():
        try:
            with _operation_lock():
                count = stale.update(status='failed', completed_at=timezone.now(),
                                     error='Задача не завершилась за час. Проверьте Celery worker.')
                if count:
                    _notify_failure('Зависшая задача резервного копирования сброшена; будет выполнена новая попытка.')
        except ValueError:
            return None
    last = BackupRecord.objects.filter(status='success').order_by('-completed_at').first()
    if last and last.completed_at + INTERVAL > timezone.now():
        return None
    if BackupRecord.objects.filter(status__in=['pending', 'running']).exists():
        return None
    try:
        return queue_backup()
    except ValueError:
        return None


def _archive_path(record):
    filename = record.filename
    if not filename or Path(filename).name != filename or not filename.endswith('.gelbackup'):
        raise ValueError('Недопустимое имя архива.')
    return _directory() / filename


def _validated_dump(record, destination):
    if record.status != 'success':
        raise ValueError('Восстановление возможно только из успешной копии.')
    path = _archive_path(record)
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES * 2:
        raise ValueError('Архив отсутствует или превышает допустимый размер.')
    encrypted = path.read_bytes()
    if hashlib.sha256(encrypted).hexdigest() != record.sha256:
        raise ValueError('Контрольная сумма архива не совпадает.')
    try:
        raw = _cipher().decrypt(encrypted)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if set(archive.namelist()) != {'manifest.json', 'database.dump'}:
                raise ValueError('Неожиданная структура архива.')
            if any(item.file_size > MAX_ARCHIVE_BYTES for item in archive.infolist()):
                raise ValueError('Распакованный архив превышает допустимый размер.')
            if archive.getinfo('manifest.json').file_size > 8192:
                raise ValueError('Недопустимый манифест архива.')
            manifest = json.loads(archive.read('manifest.json'))
            payload = archive.read('database.dump')
    except (InvalidToken, zipfile.BadZipFile, json.JSONDecodeError, KeyError, UnicodeDecodeError) as exc:
        raise ValueError('Архив повреждён или зашифрован другим ключом.') from exc
    if (manifest.get('version') != 1 or manifest.get('engine') != _engine()
            or manifest.get('backup_id') != record.pk):
        raise ValueError('Архив не соответствует записи или типу базы данных.')
    if hashlib.sha256(payload).hexdigest() != manifest.get('sha256'):
        raise ValueError('Контрольная сумма базы данных не совпадает.')
    destination.write_bytes(payload)
    if _engine() == 'sqlite':
        try:
            with sqlite3.connect(f'file:{destination}?mode=ro', uri=True) as db:
                if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                    raise ValueError('Проверка целостности SQLite не пройдена.')
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'crm_user', 'crm_backuprecord', 'django_migrations'} <= tables:
                    raise ValueError('В архиве нет обязательных таблиц CRM.')
        except sqlite3.DatabaseError as exc:
            raise ValueError('Архив содержит некорректную базу SQLite.') from exc
    else:
        listing = _run(['pg_restore', '--list', str(destination)]).stdout.decode('utf-8', errors='replace')
        if not all(name in listing for name in ('crm_user', 'crm_backuprecord', 'django_migrations')):
            raise ValueError('В архиве нет обязательных таблиц CRM.')
    return manifest


def restore_backup(record, user):
    """Authorization/password confirmation belongs to the API; verify role too."""
    if not user.is_active or user.role != 'superadmin':
        raise ValueError('Восстанавливать копии может только главный администратор.')
    _cipher()
    with _operation_lock():
        if BackupRecord.objects.filter(status__in=['pending', 'running']).exists():
            raise ValueError('Дождитесь завершения текущего резервного копирования.')
        directory = _directory()
        flag = directory / '.restore-in-progress'
        with tempfile.TemporaryDirectory(prefix='.restore-', dir=directory) as temp:
            dump_path = Path(temp) / 'database.dump'
            manifest = _validated_dump(record, dump_path)
            # An invalid archive cannot trigger a safety snapshot or database writes.
            flag.write_text(timezone.now().isoformat(), encoding='utf-8')
            try:
                safety = BackupRecord.objects.create(requested_by=user, status='pending')
                _write_snapshot(safety)
                inventory = list(BackupRecord.objects.values(
                    'id', 'status', 'filename', 'sha256', 'error', 'created_at', 'completed_at'))
                username = user.username
                if _engine() == 'sqlite':
                    target = str(settings.DATABASES['default']['NAME'])
                    if target == ':memory:' or target.startswith('file:memory'):
                        raise ValueError('Восстановление общей in-memory SQLite не поддерживается.')
                    connections.close_all()
                    with sqlite3.connect(dump_path) as origin, sqlite3.connect(target) as destination:
                        origin.backup(destination)
                else:
                    # Keep the advisory-lock connection open. pg_restore performs one
                    # atomic transaction, so a restore error leaves the current DB intact.
                    args = _pg_args('pg_restore')
                    args.extend(['--clean', '--if-exists', '--no-owner', '--no-privileges',
                                 '--single-transaction', '--exit-on-error', '--dbname',
                                 str(settings.DATABASES['default']['NAME']), str(dump_path)])
                    _run(args)
                    connections.close_all()
                # Restore inventory is outside the snapshot's data timeline. Keep
                # retained archives accessible, including the safety copy just made.
                with transaction.atomic():
                    for item in inventory:
                        identifier = item.pop('id')
                        BackupRecord.objects.update_or_create(pk=identifier, defaults=item)
                    # The restored dump's sequence predates inventory records
                    # inserted above. Explicit IDs do not advance PostgreSQL
                    # sequences; reset before the next scheduled backup insert.
                    with connection.cursor() as cursor:
                        for sql in connection.ops.sequence_reset_sql(no_style(), [BackupRecord]):
                            cursor.execute(sql)
                    actor = User.objects.filter(username=username).first()
                    AuditEvent.objects.create(actor=actor, action='backup.restored', entity='backup',
                                              entity_id=str(record.pk), details={
                                                  'safety_backup_id': safety.pk,
                                                  'snapshot_created_at': manifest['created_at']})
                    Session.objects.all().delete()
                _prune(protected=(record.pk, safety.pk))
                return {'restored': True, 'safety_backup_id': safety.pk,
                        'detail': 'База восстановлена. Войдите повторно.'}
            except Exception:
                _notify_failure('Не удалось восстановить архив. Исходная база или защитная копия сохранена.')
                raise
            finally:
                flag.unlink(missing_ok=True)
