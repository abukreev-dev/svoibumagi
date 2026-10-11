# Развёртывание и эксплуатация

Эта инструкция рассчитана на самостоятельный экземпляр «Своих бумаг». Нужны Docker Engine с Compose, Python 3.12+ для создания хеша пароля, свободный локальный порт и место для постоянного Docker-тома. Для доступа из интернета нужен домен и HTTPS-прокси на том же хосте. Один экземпляр обслуживает одного владельца.

## 1. Получить код и создать пароль

```bash
git clone https://github.com/abukreev-dev/svoibumagi.git
cd svoibumagi
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/python -m svoibumagi hash-password
```

Команда запрашивает пароль без отображения ввода и выдаёт `scrypt:` хеш. Не записывайте открытый пароль в команду, историю shell или `.env`. Создайте `.env` в корне проекта:

```dotenv
SVOIBUMAGI_PASSWORD_HASH=scrypt:...
SVOIBUMAGI_HOST_PORT=18080
SVOIBUMAGI_MOEX_ISS_ENABLED=0
```

Замените многоточие настоящим хешем и ограничьте доступ к файлу:

```bash
chmod 600 .env
```

`.env` исключён из Git. Отдельный серверный экземпляр должен иметь собственный пароль и хранилище.

## 2. Запустить Docker Compose

```bash
docker compose --env-file .env up -d --build
docker compose --env-file .env ps
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18080/login
```

Ожидается работающий контейнер и HTTP `200` для страницы входа. Если изменили `SVOIBUMAGI_HOST_PORT`, подставьте этот порт в проверку и конфигурацию прокси. Compose публикует порт только на `127.0.0.1`; база и исходные файлы лежат в именованном томе `svoibumagi_data`. Команда `docker compose down` останавливает контейнеры и сохраняет том; **`down -v` удаляет данные**.

Обычный контейнер требует HTTPS со стороны браузера: cookie входа имеет флаг `Secure`. Ответ `/login` по локальному HTTP подтверждает только работу процесса, вход проверяйте через HTTPS.

### HTTPS через Nginx

Выпустите сертификат для своего домена штатным способом. Пример блока для уже полученных сертификата и ключа; замените домен и пути:

```nginx
server {
    listen 443 ssl;
    server_name portfolio.example.org;

    ssl_certificate     /etc/letsencrypt/live/portfolio.example.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/portfolio.example.org/privkey.pem;
    client_max_body_size 21m;

    location / {
        proxy_pass http://127.0.0.1:18080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

Перед применением проверьте `nginx -t`, затем перезагрузите Nginx способом, принятым на вашем сервере. Если там размещены другие сайты, проверьте их после перезагрузки. Не публикуйте порт приложения наружу в обход HTTPS. Лимит Nginx `21m` оставляет запас для формы вокруг файла, который приложение ограничивает 20 МБ.

Через публичный адрес проверьте `/login`, вход, закрытость `/operations` без сессии и наличие `Secure`, `HttpOnly`, `SameSite=Strict` у cookie. Приложение не поддерживает регистрацию других пользователей.

### Локальная проба без HTTPS

Для разработки **только на том же компьютере** добавьте отдельный Compose-файл:

```bash
docker compose -f compose.yaml -f compose.local-http.yaml --env-file .env up -d --build
```

Откройте `http://127.0.0.1:18080/`. Этот режим убирает `Secure` у cookie и допустим только при сохранении привязки порта к loopback. Перед переходом на основной режим запускайте Compose без `compose.local-http.yaml` и пересоздайте контейнер. Оба режима используют один и тот же том, если не указано другое имя проекта Compose.

Для запуска прямо из Python без Docker:

```bash
SVOIBUMAGI_PASSWORD_HASH='<ваш scrypt-хеш>' .venv/bin/python -m svoibumagi serve --data-dir ./data --insecure-http
```

Этот вариант слушает `127.0.0.1:8000`; каталог `data/` исключён из Git. Он предназначен только для локальной разработки.

## 3. Загрузить отчёты

После входа создайте локальный счёт Сбера или Альфа-Инвестиций и загрузите отчёт. Сверьте номер счёта из файла с выбранным счётом в сервисе, период, сделки, денежные строки, конечные позиции и остатки. Предпросмотр показывает неизвестные строки, неполные проверки, повторы и конфликты. Подтверждение проводит операции атомарно; отклонение оставляет журнал без этих операций.

Загружайте отчёты от начала истории счёта. Расчёт FIFO и XIRR требует достаточной истории и оценки активов; пока данных не хватает, интерфейс сообщает причину недоступности. Отсутствующий месячный отчёт сам по себе не доказывает отсутствия операций. Экспорт доступен после входа: операции, позиции, оценка, показатели и пробелы истории в CSV.

## 4. Резервная копия

В интерфейсе владельца есть кнопка **«Скачать копию базы и файлов»**. Архив содержит финансовые данные и исходные отчёты; храните его в закрытом месте, желательно вне сервера приложения. Проверка копии должна включать пробное восстановление.

Для Docker можно создать и забрать ZIP с хоста:

```bash
mkdir -p backups
chmod 700 backups
docker compose --env-file .env exec -T app python -m svoibumagi backup --data-dir /data --output /tmp/svoibumagi-backup.zip
container_id=$(docker compose --env-file .env ps -q app)
docker cp "$container_id:/tmp/svoibumagi-backup.zip" "backups/svoibumagi-$(date -u +%Y%m%dT%H%M%SZ).zip"
docker compose --env-file .env exec -T app rm -f /tmp/svoibumagi-backup.zip
```

Команда `backup` проверяет целостность SQLite и соответствие сохранённых исходных файлов их хешам. Папка `backups/` находится в каталоге проекта, поэтому добавлена в `.gitignore`. Для запуска по расписанию используйте планировщик своего сервера и регулярно проверяйте наличие свежего архива вне Docker-тома.

Для установки без Docker:

```bash
.venv/bin/python -m svoibumagi backup --data-dir ./data --output /закрытый/каталог/svoibumagi-backup.zip
```

## 5. Восстановление

Восстановление всегда создаёт **новый** каталог данных; в существующий каталог команда писать отказывается. Она проверяет ZIP-манифест, хеши исходных файлов, SQLite и внешние ключи. Сначала выполните пробу на копии архива:

```bash
.venv/bin/python -m svoibumagi restore --archive /закрытый/каталог/svoibumagi-backup.zip --data-dir ./data-restored
```

Для Docker сохраняйте одинаковый абсолютный путь к данным при восстановлении и последующем запуске: база хранит пути к приватным исходным файлам. Рабочий том не перезаписывайте. Создайте новый пустой том (для каждой следующей попытки выбирайте новое имя) и восстановите архив в `/data/restored`:

```bash
docker volume create svoibumagi_restored
image_id=$(docker compose --env-file .env images -q app)
docker run --rm --user 0:0 \
  -v svoibumagi_restored:/data \
  -v "$PWD/закрытый-каталог/svoibumagi-backup.zip:/backup.zip:ro" \
  --entrypoint sh "$image_id" -c 'python -m svoibumagi restore --archive /backup.zip --data-dir /data/restored && chown -R 10001:10001 /data/restored'
```

Путь к архиву в примере замените на существующий абсолютный путь. Перед переключением проверьте восстановленную базу в отдельном контейнере:

```bash
docker run --rm --user 10001:10001 -v svoibumagi_restored:/data \
  --entrypoint python "$image_id" -c 'from pathlib import Path; from svoibumagi.storage import Storage; s=Storage(Path("/data/restored")); print("accounts", len(s.list_accounts()), "events", len(s.list_events()))'
```

Для запуска приложения на восстановленном томе создайте отдельный Compose-оверлей, например `compose.restored.yaml`:

```yaml
services:
  app:
    volumes:
      - svoibumagi_restored:/data
    command: ["python", "-m", "svoibumagi", "serve", "--host", "0.0.0.0", "--data-dir", "/data/restored"]
volumes:
  svoibumagi_restored:
    external: true
```

Остановите рабочее приложение, подключите проверенный том и запустите сервис за прежним HTTPS-прокси:

```bash
docker compose --env-file .env stop app
docker compose -f compose.yaml -f compose.restored.yaml --env-file .env up -d
```

Проверьте вход, проведённые операции и CSV через HTTPS. Все последующие команды `docker compose` для этого экземпляра должны включать оба файла `-f`; иначе Compose вернётся к старому тому. Сохраняйте путь `/data/restored`: база хранит абсолютные пути к исходным файлам. Старый том сохраните до проверки нового; удаление старого тома не является частью восстановления.

## 6. Обновление и диагностика

Перед обновлением создайте резервную копию. Затем в каталоге проекта обновите исходники и пересоберите контейнер:

```bash
git pull --ff-only
docker compose --env-file .env up -d --build
docker compose --env-file .env ps
```

Проверьте HTTPS-вход, обзор, один уже проведённый отчёт и CSV. При миграции схемы сервис создаёт предварительную копию SQLite внутри тома, но она не заменяет внешний архив. Если контейнер не здоров, смотрите `docker compose --env-file .env logs --tail=100 app`; не удаляйте том для исправления ошибки запуска.

Автоматические запросы котировок MOEX ISS по умолчанию выключены (`SVOIBUMAGI_MOEX_ISS_ENABLED=0`). Правила их включения и ограничения источника данных вынесены в [отдельный разбор](moex-data-terms.md). Текущие форматы и известные ограничения — в [матрице](format-matrix.md), критерии разработки — в [PRD](prd.md) и [SPEC](spec.md).
