# «Свои бумаги» с нуля: компьютер или VDS

Выберите **один** путь:

- [На своём компьютере](#вариант-а-на-своём-компьютере) — проще всего попробовать сервис. Открывается только на этом компьютере по адресу `http://127.0.0.1:18080/`.
- [На VDS](#вариант-б-на-чистом-vds) — сервис доступен по вашему домену через HTTPS, когда сервер включён.

Знаний Python не требуется. Нужен терминал для нескольких команд. Программа настройки спросит пароль и сама создаст файл `.env`; пароль на экран не выводится. В один экземпляр входит один владелец. Отчёты и база хранятся у вас, в Docker-томе.

## Вариант А. На своём компьютере

### Шаг 1. Установите необходимое

1. Установите и **запустите** [Docker Desktop для macOS](https://docs.docker.com/desktop/setup/install/mac-install/) или [Docker Desktop для Windows](https://docs.docker.com/desktop/setup/install/windows-install/). На Linux установите [Docker Engine и Compose](https://docs.docker.com/engine/install/). Дождитесь сообщения в Docker Desktop, что движок запущен.
2. Установите [Python 3](https://www.python.org/downloads/), если команда `python3 --version` на macOS/Linux или `py -3 --version` на Windows не показывает версию. На Windows при установке включите добавление Python в PATH, если установщик предлагает такой пункт.
3. Скачайте исходники: откройте [репозиторий](https://github.com/abukreev-dev/svoibumagi), нажмите **Code → Download ZIP** и распакуйте архив. В полученной папке должен лежать файл `compose.yaml`.

### Шаг 2. Откройте терминал в папке проекта

- **Windows:** откройте распакованную папку в Проводнике, нажмите на адресную строку, напишите `powershell` и нажмите Enter.
- **macOS:** откройте Terminal, напишите `cd ` (с пробелом), перетащите папку проекта из Finder в окно Terminal и нажмите Enter.
- **Linux:** откройте папку в файловом менеджере и выберите «Открыть в терминале».

Проверьте, что видите файлы проекта: `ls` на macOS/Linux или `dir` на Windows. Среди них должен быть `compose.yaml`.

### Шаг 3. Создайте пароль и запустите сервис

На macOS/Linux выполните по очереди:

```bash
python3 scripts/first_run.py
docker compose -f compose.yaml -f compose.local-http.yaml up -d --build
```

На Windows в PowerShell выполните:

```powershell
py -3 scripts/first_run.py
docker compose -f compose.yaml -f compose.local-http.yaml up -d --build
```

Первая команда дважды попросит пароль (минимум 12 символов). Когда вы печатаете пароль, символы в терминале **не появляются** — так и должно быть. Запомните пароль или сохраните его в менеджере паролей. Созданный `.env` не загружается в Git. Вторая команда скачает и соберёт необходимые компоненты; первый запуск может занять несколько минут.

Откройте **[http://127.0.0.1:18080/](http://127.0.0.1:18080/)** и нажмите «Войти». Используйте только что созданный пароль. С другого устройства этот адрес не откроется — локальный режим ограничен вашим компьютером.

### Что делать дальше

Создайте счёт нужного брокера, загрузите отчёт и внимательно просмотрите предпросмотр. Проверьте номер счёта, операции, деньги, позиции, итоги и предупреждения. Подтверждайте импорт после проверки. Неполная история может оставить доходность недоступной — сервис объяснит причину.

Резервную копию можно скачать кнопкой **«Скачать копию базы и файлов»** после входа. Храните её в закрытой папке, лучше на другом носителе. Удаление Docker-тома удалит и отчёты, и журнал.

Чтобы остановить сервис, выполните в той же папке:

```bash
docker compose -f compose.yaml -f compose.local-http.yaml stop
```

Чтобы снова запустить:

```bash
docker compose -f compose.yaml -f compose.local-http.yaml up -d
```

## Вариант Б. На чистом VDS

Здесь предполагается новый сервер Ubuntu 24.04 без другого сайта на портах 80 и 443. Нужны также домен и доступ к настройкам DNS. Caddy в комплекте сам запросит и продлит HTTPS-сертификат, если домен указывает на VDS и порты доступны из интернета. Если на сервере уже работают Nginx, Apache или другие сайты, используйте [техническую инструкцию](operations.md) для подключения к существующему HTTPS-прокси.

### Шаг 1. Настройте домен

В панели управления доменом создайте **A-запись** для выбранного имени, например `portfolio.example.org`, со значением IPv4-адреса VDS. Если есть AAAA-запись, она должна указывать на работающий IPv6-адрес этого VDS; иначе удалите неверную запись. В панели VDS разрешите входящие соединения на TCP-порты **80** и **443**. Изменения DNS могут примениться не сразу.

### Шаг 2. Зайдите на сервер

Откройте Terminal или PowerShell на своём компьютере. Команду и имя пользователя для SSH обычно показывает провайдер VDS. Пример:

```bash
ssh ubuntu@203.0.113.10
```

Замените `ubuntu` и адрес на данные провайдера. Все следующие команды варианта Б выполняются **на VDS внутри SSH-сеанса**.

### Шаг 3. Установите Docker, если его нет

Проверьте:

```bash
sudo docker compose version
```

Если версия вывелась — переходите к шагу 4. Если команда не найдена, выполните на Ubuntu 24.04 следующий блок из [официальной инструкции Docker](https://docs.docker.com/engine/install/ubuntu/):

```bash
sudo apt update
sudo apt install -y ca-certificates curl git python3
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF_DOCKER
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF_DOCKER
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo docker compose version
```

Если Docker уже был установлен, но нет `git` или `python3`, установите их через `sudo apt install -y git python3`.

Если на VDS включён `ufw`, дополнительно разрешите веб-порты: `sudo ufw allow 80/tcp` и `sudo ufw allow 443/tcp`. SSH-порт оставьте доступным.

### Шаг 4. Скачайте сервис и создайте пароль

```bash
git clone https://github.com/abukreev-dev/svoibumagi.git
cd svoibumagi
python3 scripts/first_run.py --domain portfolio.example.org
```

Замените `portfolio.example.org` своим именем **без** `https://` и слеша. Программа дважды спросит пароль (минимум 12 символов) и создаст закрытый `.env`. Если файл `.env` уже существует, она его не перепишет.

### Шаг 5. Запустите

```bash
sudo docker compose -f compose.yaml -f compose.public.yaml up -d --build
sudo docker compose -f compose.yaml -f compose.public.yaml ps
```

Дождитесь, пока у `app` появится состояние `healthy`. Затем откройте `https://portfolio.example.org/`, подставив свой домен. Первый выпуск сертификата может занять несколько минут. После входа используйте пароль из шага 4. Caddy сохраняет сертификаты в отдельном Docker-томе, а приложение — отчёты и базу в своём томе.

После первого импорта скачайте резервную копию через интерфейс и сохраните её **не только на VDS**. Для регулярных копий и восстановления используйте [техническую инструкцию](operations.md#4-резервная-копия).

## Если что-то не получилось

| Что видите | Что проверить |
|---|---|
| `docker: command not found` или `Cannot connect to the Docker daemon` | Установлен и запущен Docker Desktop; на VDS выполнен шаг 3. |
| `python3: command not found` или `py: command not found` | Установите Python 3 по ссылке из шага 1; на VDS — `sudo apt install -y python3`. |
| `Файл .env уже существует` | Пароль уже был настроен. Запускайте Compose с этим файлом; инструкция не стирает существующую настройку. |
| Порт `18080` занят | Для новой установки удалите ещё не использованный `.env` и повторите `first_run.py --port 18081`; адрес станет `http://127.0.0.1:18081/`. Если в экземпляре уже есть данные, измените только `SVOIBUMAGI_HOST_PORT` в `.env`. |
| На VDS сайт не открывается или сертификат не выдаётся | Проверьте A/AAAA-записи, доступность 80/443, отсутствие другого веб-сервера на этих портах и `sudo docker compose -f compose.yaml -f compose.public.yaml logs --tail=100 caddy`. [Условия автоматического HTTPS у Caddy](https://caddyserver.com/docs/quick-starts/https). |
| Страница входа открылась, но после входа снова просит пароль | На VDS заходите по `https://`, а на компьютере запускайте Compose **с** `compose.local-http.yaml`. |

Для проверки контейнеров на компьютере: `docker compose -f compose.yaml -f compose.local-http.yaml ps`; на VDS: `sudo docker compose -f compose.yaml -f compose.public.yaml ps`. В логах приложения будут подсказки: замените в этих командах `ps` на `logs --tail=100 app`.

**Не выполняйте `docker compose down -v`**, если хотите сохранить загруженные отчёты. Для обновления и восстановления есть [отдельные шаги](operations.md#6-обновление-и-диагностика).
