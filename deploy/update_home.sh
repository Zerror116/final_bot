#!/usr/bin/env bash
# Обновление final_bot на домашнем сервере из GitHub.
# Запускать НА СЕРВЕРЕ под root:
#     bash update_home.sh            # ветка main
#     bash update_home.sh <ветка>    # другая ветка
#
# Что делает:
#   1. Скачивает свежий код с GitHub (репозиторий публичный, git не нужен).
#   2. Проверяет, что новый код компилируется.
#   3. Делает бэкап базы (pg_dump из Docker) и бэкап текущего кода.
#   4. Кладёт новый код поверх /opt/final_bot (ключи, venv и настройки не трогает).
#   5. Обновляет зависимости Python и перезапускает бота.
#   6. Если бот не поднялся — сам откатывает код и перезапускает старую версию.
#   7. Хранит только 5 последних бэкапов кода и базы перед обновлением.
set -euo pipefail

REPO="${REPO:-Zerror116/final_bot}"
BRANCH="${1:-main}"
APP="${APP:-/opt/final_bot}"
SERVICE="${SERVICE:-final_bot}"
ENVF="${ENVF:-/etc/final_bot.env}"
PGC="${PGC:-final_bot_db}"
BK="${BK:-/opt/final_bot_backups}"
WAIT_SECONDS="${WAIT_SECONDS:-25}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"          # сколько последних бэкапов каждого вида хранить
MIN_FREE_MB="${MIN_FREE_MB:-500}"          # меньше — обновление не начинаем
TS="$(date +%Y%m%d_%H%M%S)"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m   %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || [ -n "${ALLOW_NON_ROOT:-}" ] || die "запускай под root"
[ -f "$APP/main.py" ] || die "не найден $APP/main.py"
PY="$APP/venv/bin/python"
[ -x "$PY" ] || die "не найден $PY"
mkdir -p "$BK"

free_mb() { df -Pm "$1" | awk 'NR==2 {print $4}'; }
FREE_MB="$(free_mb "$BK")"
echo "   свободно на диске: ${FREE_MB} МБ, бэкапы обновлений занимают: $(du -sh "$BK" 2>/dev/null | cut -f1)"
[ "${FREE_MB:-0}" -ge "$MIN_FREE_MB" ] || die "на диске меньше ${MIN_FREE_MB} МБ свободного места, освободи место в $BK"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fetch() {  # fetch URL OUTFILE
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --connect-timeout 20 "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  else
    "$PY" -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$1" "$2"
  fi
}

env_value() {  # значение переменной из env-файла без кавычек
  grep -E "^$1=" "$ENVF" 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

# ---------- 1. скачать код
say "Скачиваю $REPO, ветка $BRANCH"
fetch "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" "$WORK/src.tgz" \
  || die "не удалось скачать код с GitHub"
mkdir -p "$WORK/src"
tar -xzf "$WORK/src.tgz" -C "$WORK/src" --strip-components=1
[ -f "$WORK/src/main.py" ] || die "в архиве нет main.py"
rm -rf "$WORK/src/android_post_stats"

COMMIT=""
if fetch "https://api.github.com/repos/$REPO/commits/$BRANCH" "$WORK/commit.json" 2>/dev/null; then
  COMMIT="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha"][:7])' "$WORK/commit.json" 2>/dev/null || true)"
fi
echo "   коммит: ${COMMIT:-неизвестен}"
if [ -n "$COMMIT" ] && [ -f "$APP/DEPLOYED_VERSION" ] && grep -q "commit=$COMMIT" "$APP/DEPLOYED_VERSION"; then
  warn "эта версия уже установлена; обновляю повторно"
fi

# ---------- 2. проверка нового кода
say "Проверяю, что новый код компилируется"
"$PY" -m compileall -q "$WORK/src" >/dev/null || die "новый код не компилируется, обновление отменено"
echo "   ок"

# ---------- 3. бэкапы
say "Бэкап базы перед обновлением"
DB_DUMP=""
if command -v docker >/dev/null 2>&1 && [ "$(docker inspect -f '{{.State.Running}}' "$PGC" 2>/dev/null || true)" = "true" ]; then
  DBUSER="$(env_value DB_USER)"; DBNAME="$(env_value DB_NAME)"
  [ -n "$DBUSER" ] && [ -n "$DBNAME" ] || die "в $ENVF нет DB_USER или DB_NAME"
  DB_DUMP="$BK/before_update_$TS.dump"
  docker exec "$PGC" pg_dump -U "$DBUSER" -d "$DBNAME" -Fc > "$DB_DUMP" || die "pg_dump не удался, обновление отменено"
  [ -s "$DB_DUMP" ] || die "дамп базы пустой, обновление отменено"
  echo "   $DB_DUMP ($(du -h "$DB_DUMP" | cut -f1))"
else
  warn "контейнер $PGC не запущен или нет docker — бэкап базы пропущен"
fi

say "Бэкап текущего кода"
CODE_BK="$BK/code_before_update_$TS.tgz"
# Только сам бот: без venv, git, старых бэкапов и дампов (иначе ~80 МБ на каждое обновление).
APP_NAME="$(basename "$APP")"
tar -czf "$CODE_BK" -C "$(dirname "$APP")" \
  --exclude="$APP_NAME/venv" --exclude="$APP_NAME/.git" \
  --exclude="$APP_NAME/backups" --exclude="$APP_NAME/deploy_backups" --exclude="$APP_NAME/.deploy_backups" \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='*.bak_*' --exclude='*.dump' --exclude='*.tgz' \
  "$APP_NAME"
echo "   $CODE_BK ($(du -h "$CODE_BK" | cut -f1))"

rollback() {
  warn "Откатываю код к версии до обновления"
  tar -xzf "$CODE_BK" -C "$(dirname "$APP")"
  systemctl restart "$SERVICE" || true
  sleep 5
  warn "состояние после отката: $(systemctl is-active "$SERVICE" || true)"
}

# ---------- 4. установка кода
say "Кладу новый код в $APP"
# Ключи (*.json), venv, config.json и прочие файлы, которых нет в репозитории, остаются на месте.
(cd "$WORK/src" && tar -cf - .) | (cd "$APP" && tar -xf -)
find "$APP" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chmod 600 "$APP"/*.json 2>/dev/null || true
printf 'commit=%s\nbranch=%s\ninstalled_at=%s\n' "${COMMIT:-unknown}" "$BRANCH" "$(date '+%F %T')" > "$APP/DEPLOYED_VERSION"
echo "   готово"

# ---------- 5. зависимости
say "Обновляю зависимости Python"
if ! "$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"; then
  rollback
  die "pip install не удался, код откатан"
fi
"$PY" -c 'import telebot, sqlalchemy, psycopg2, googleapiclient, openpyxl' || { rollback; die "библиотеки не импортируются, код откатан"; }
echo "   ок"

# ---------- 6. перезапуск и проверка
say "Перезапускаю бота"
systemctl restart "$SERVICE"
RESTARTS_BEFORE="$(systemctl show "$SERVICE" -p NRestarts --value 2>/dev/null || echo 0)"
echo "   жду ${WAIT_SECONDS} сек..."
sleep "$WAIT_SECONDS"
STATE="$(systemctl is-active "$SERVICE" || true)"
RESTARTS_AFTER="$(systemctl show "$SERVICE" -p NRestarts --value 2>/dev/null || echo 0)"
journalctl -u "$SERVICE" -n 20 --no-pager -o cat 2>/dev/null || true

if [ "$STATE" != "active" ] || [ "$RESTARTS_AFTER" != "$RESTARTS_BEFORE" ]; then
  warn "бот не запустился стабильно (состояние: $STATE, перезапусков: $RESTARTS_BEFORE -> $RESTARTS_AFTER)"
  rollback
  die "обновление откатано. Логи: journalctl -u $SERVICE -n 100 --no-pager"
fi

for extra in final_bot_statistic_api final_bot_button_control; do
  if systemctl list-unit-files "$extra.service" >/dev/null 2>&1 && systemctl is-enabled "$extra" >/dev/null 2>&1; then
    systemctl restart "$extra" && echo "   перезапущен $extra"
  fi
done

# храним только KEEP_BACKUPS последних бэкапов каждого вида (ежедневные дампы cron не трогаем)
for pattern in 'before_update_*.dump' 'code_before_update_*.tgz'; do
  ls -1t "$BK"/$pattern 2>/dev/null | tail -n +"$((KEEP_BACKUPS + 1))" | while read -r old_backup; do
    rm -f -- "$old_backup"
  done || true
done

say "Готово: бот обновлён (коммит ${COMMIT:-неизвестен}) и работает"
echo "   бэкап базы: ${DB_DUMP:-не делался}"
echo "   бэкап кода: $CODE_BK"
echo "   бэкапы обновлений: последние $KEEP_BACKUPS, занимают $(du -sh "$BK" 2>/dev/null | cut -f1), свободно $(free_mb "$BK") МБ"
echo "   проверить бэкап в Telegram: отправь боту /backup_now"
