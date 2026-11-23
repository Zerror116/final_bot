import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


# Systemd-сервис бота запускается с PATH только из venv, поэтому системные пути добавляем сами.
EXTRA_BINARY_PATHS = (
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/local/sbin",
    "/usr/sbin",
    "/opt/homebrew/bin",
)
DEFAULT_DOCKER_CONTAINER = "final_bot_db"
DEFAULT_TIMEOUT_SECONDS = 600


class DatabaseBackupError(Exception):
    pass


def find_executable(name, explicit_path=None):
    if explicit_path:
        return explicit_path if os.path.isfile(explicit_path) and os.access(explicit_path, os.X_OK) else None
    search_path = os.pathsep.join([os.environ.get("PATH", ""), *EXTRA_BINARY_PATHS])
    found = shutil.which(name, path=search_path)
    if found:
        return found
    for candidate in sorted(Path("/usr/lib/postgresql").glob(f"*/bin/{name}"), reverse=True):
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def docker_container_running(docker_path, container):
    try:
        completed = subprocess.run(
            [docker_path, "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def build_backup_command(db_url):
    """Возвращает (команда, env). Дамп всегда пишется в stdout в формате pg_dump custom (-Fc)."""
    if not db_url.drivername.startswith("postgresql"):
        raise DatabaseBackupError(f"Бэкап поддерживается только для PostgreSQL, сейчас: {db_url.drivername}")

    database = db_url.database
    username = db_url.username
    if not database or not username:
        raise DatabaseBackupError("Не указаны имя базы или пользователь для бэкапа.")

    container = (os.environ.get("DB_BACKUP_DOCKER_CONTAINER") or DEFAULT_DOCKER_CONTAINER).strip()
    docker_path = find_executable("docker")
    if container and docker_path and docker_container_running(docker_path, container):
        # Внутри контейнера подключение идёт через локальный сокет, пароль не нужен.
        return [docker_path, "exec", container, "pg_dump", "-U", username, "-d", database, "-Fc"], None

    pg_dump_path = find_executable("pg_dump", os.environ.get("PG_DUMP_PATH", "").strip() or None)
    if not pg_dump_path:
        raise DatabaseBackupError(
            "Не найден pg_dump и не запущен Docker-контейнер базы "
            f"«{container or DEFAULT_DOCKER_CONTAINER}». Укажите PG_DUMP_PATH или DB_BACKUP_DOCKER_CONTAINER."
        )

    command = [pg_dump_path, "-Fc", "-U", username, "-d", database]
    if db_url.host:
        command.extend(["-h", db_url.host])
    if db_url.port:
        command.extend(["-p", str(db_url.port)])
    env = os.environ.copy()
    if db_url.password:
        # Пароль передаём через окружение, чтобы он не был виден в списке процессов.
        env["PGPASSWORD"] = str(db_url.password)
    return command, env


def backup_file_name(database, current=None):
    current = current or datetime.now()
    safe_database = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(database or "db"))
    return f"{safe_database}_{current.strftime('%Y-%m-%d_%H%M')}.dump"


def create_database_backup(db_url, output_dir, current=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    command, env = build_backup_command(db_url)
    output_path = Path(output_dir) / backup_file_name(db_url.database, current)
    try:
        with open(output_path, "wb") as output_file:
            completed = subprocess.run(
                command,
                stdout=output_file,
                stderr=subprocess.PIPE,
                env=env,
                timeout=timeout,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise DatabaseBackupError(f"pg_dump не уложился в {timeout} сек.") from exc
    except OSError as exc:
        raise DatabaseBackupError(f"Не удалось запустить pg_dump: {exc}") from exc

    if completed.returncode != 0:
        error_text = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise DatabaseBackupError(f"pg_dump завершился с ошибкой {completed.returncode}: {error_text[:500]}")

    if output_path.stat().st_size == 0:
        raise DatabaseBackupError("pg_dump создал пустой файл.")
    return output_path
