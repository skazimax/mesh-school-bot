# Постоянная работа на Linux

## systemd

Шаблон `systemd/mesh-school-bot.service` использует установку в
`/opt/mesh-school-bot` и отдельного системного пользователя `meshbot`.
Это рекомендуемые значения; при другом пути исправьте WorkingDirectory,
EnvironmentFile, ExecStart и ReadWritePaths в unit-файле.

Установите репозиторий в выбранный каталог, создайте пользователя и окружение:

```bash
sudo useradd --system --home-dir /opt/mesh-school-bot --shell /usr/sbin/nologin meshbot
sudo install -d -m 0700 -o meshbot -g meshbot /opt/mesh-school-bot/data /opt/mesh-school-bot/.secrets
cd /opt/mesh-school-bot
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

Создайте `.env` из `.env.example`, настройте его по README и передайте файлу
владельца `meshbot` с правами `0600`. Для входа непосредственно на сервере:

```bash
sudo -u meshbot .venv/bin/python -m tools.mesh prepare-login
sudo -u meshbot .venv/bin/python -m tools.mesh login
```

Если авторизация выполнялась на другой машине, перенесите только приватный
`auth.json` и необходимые настройки защищённым способом; копирование всего
виртуального окружения не требуется. Установите права:

```bash
sudo chown meshbot:meshbot .env .secrets/auth.json
sudo chmod 600 .env .secrets/auth.json
sudo install -m 0644 systemd/mesh-school-bot.service /etc/systemd/system/mesh-school-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now mesh-school-bot.service
sudo systemctl status mesh-school-bot.service --no-pager
```

Пользователь `meshbot` должен иметь доступ на чтение к коду и `.venv`, запись —
к `data/` и `.secrets/`. Сервис использует hardening systemd и сам обрабатывает SIGTERM.
Если ваш прокси запускается отдельным сервисом, при необходимости добавьте
его в After/Wants unit-файла. Конкретный прокси-сервис не является зависимостью проекта.

## Проверка и обновление

```bash
cd /opt/mesh-school-bot
sudo -u meshbot .venv/bin/python -m tools.runtime status
sudo -u meshbot .venv/bin/python -m tools.runtime reports
sudo journalctl -u mesh-school-bot.service -n 30 --no-pager
```

Диагностика не отправляет сообщения и не печатает содержимое дневника.
Для ручной полной синхронизации остановите сервис, выполните
`sudo -u meshbot .venv/bin/python -m tools.runtime once`, затем запустите его снова.
Не запускайте несколько экземпляров с одной SQLite/авторизацией.

Перед обновлением создайте backup. Обновление выполняйте от владельца каталога кода:

```bash
sudo -u meshbot .venv/bin/python -m tools.runtime backup
sudo systemctl stop mesh-school-bot.service
git pull --ff-only
.venv/bin/python -m pip install -e .
sudo systemctl start mesh-school-bot.service
sudo systemctl status mesh-school-bot.service --no-pager
```

`.env`, `.secrets/` и `data/` сохраняются между обновлениями. При замене unit-файла
повторно установите его и выполните `systemctl daemon-reload`.

## Резервные копии и восстановление

Scheduler делает согласованные копии SQLite через backup API и хранит последние
14 ежедневных файлов `data/backups/YYYY-MM-DD.db`. Ручной backup:

```bash
sudo -u meshbot .venv/bin/python -m tools.runtime backup
```

Для восстановления:

1. Остановите сервис.
2. Сохраните текущие `bot.db`, `bot.db-wal` и `bot.db-shm` в отдельный приватный каталог.
3. Уберите текущие файлы базы и WAL из рабочего каталога, установите выбранную
   резервную копию как `data/bot.db`, владелец `meshbot`, права `0600`.
4. Запустите сервис и проверьте `/status`.

Не копируйте только основной `bot.db` работающей WAL-базы вместо backup API.
SQLite backup не содержит `.env` и авторизацию: их резервируйте отдельно
в защищённом хранилище. Копии базы содержат персональные данные дневника.
