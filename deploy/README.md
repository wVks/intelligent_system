# Один VPS: Docker Compose и Caddy

Исходники находятся в `/opt/studynavigator/app`, серверные настройки — отдельно
в `/opt/studynavigator/server.env` с правами `0600`. Используйте `.env.example`
как шаблон; ключ остаётся только на сервере. В командах ниже `--env-file /dev/null`
отключает автоматическую загрузку локального `.env` самим Compose; приложение
получает настройки через `env_file` из серверного файла.

## Запуск и проверка

Нужны Docker Engine с Compose V2, свободные TCP 80/443, UDP 443 для HTTP/3,
исходящий HTTPS и DNS, указывающий на VPS. Основной адрес:
`https://studynavigator.136-234-76-207.sslip.io`.

```sh
cd /opt/studynavigator/app
docker compose --env-file /dev/null config --quiet
docker compose --env-file /dev/null build --pull app
docker compose --env-file /dev/null run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile
docker compose --env-file /dev/null up -d --wait
docker compose --env-file /dev/null ps
curl --fail https://studynavigator.136-234-76-207.sslip.io/health
```

Для другого файла настроек задайте `STUDYNAV_ENV_FILE`, для другого домена —
`DOMAIN` перед командами Compose. Не выводите `docker compose config`
без `--quiet`: развёрнутые настройки могут включать API-ключ.

Backend работает от `10001:10001`, слушает `0.0.0.0:8000` внутри контейнера и
не публикует порт на хост. Единственный worker задан в Python независимо от
`WEB_CONCURRENCY`. Caddy на `172.30.43.2` — единственный доверенный источник
forwarded-заголовков. Отдельная bridge-сеть `172.30.43.0/28` не должна пересекаться
с сетью VPS/VPN; при смене IP прокси обновите `FORWARDED_ALLOW_IPS` вместе с IPAM.
Сеть допускает исходящие подключения: они нужны для API модели.

Именованный том `studynavigator_app_data` сохраняет `/app/var`: SQLite, загруженные
PDF и журнал расходов. При первом создании Docker переносит права `10001:10001`
из образа. Тома `studynavigator_caddy_data` и `studynavigator_caddy_config`
сохраняют сертификаты и состояние Caddy. Оба сервиса имеют `unless-stopped`
и ротацию stdout/stderr до трёх файлов по 10 MiB. Docker должен быть включён
в автозапуск на VPS. Не используйте `down -v` для обычного обновления.

`/health` проверяет приложение и MCP при старте, но не вызывает платную модель.
Успешный healthcheck не заменяет проверку пользовательского сценария с LLM.
При обновлении кода выполните `build --pull app`, затем `up -d --wait`.
Для перезагрузки Caddy после изменения файла:

```sh
docker compose --env-file /dev/null exec caddy caddy reload --config /etc/caddy/Caddyfile
docker compose --env-file /dev/null logs --tail 100 app caddy
```

## Копия данных и восстановление

Копия согласованна, когда `app` остановлен. Она содержит пользовательские PDF,
SQLite и журнал расходов; храните её с ограниченным доступом и скопируйте вне VPS.
Настройки и ключ не входят в том и требуют отдельного защищённого хранения.

```sh
cd /opt/studynavigator/app
umask 077
mkdir -p /opt/studynavigator/backups
docker compose --env-file /dev/null stop app
docker run --rm --network none --read-only --user 0:0 --entrypoint tar \
  -v studynavigator_app_data:/data:ro studynavigator:local \
  -C /data -czf - . > /opt/studynavigator/backups/app-var.tar.gz
docker compose --env-file /dev/null start app
```

Если архивирование завершилось ошибкой, всё равно запустите `app` последней
командой. Для восстановления остановите `app`, сохраните текущие данные и
актуальный журнал расходов отдельно. Установите `STUDYNAV_MODE=offline` в
серверном файле. На пустом томе выполните:

```sh
docker volume create studynavigator_app_data
docker run --rm -i --network none --read-only --user 0:0 --entrypoint sh \
  -v studynavigator_app_data:/data studynavigator:local \
  -c 'tar -C /data -xzf - && chown -R 10001:10001 /data' \
  < /opt/studynavigator/backups/app-var.tar.gz
docker compose --env-file /dev/null up -d --wait app
```

Восстановление предназначено для пустого тома; создание уже существующего тома
не очищает его. Перед повторным включением live верните актуальный журнал
расходов: старый архив откатывает счётчик и может повторно открыть потраченный
лимит. При отсутствии актуального журнала оставьте offline до явного решения
о новом бюджете. Проверьте задания, документы и план после восстановления.

Образы закреплены по digest, Python-зависимости — по версиям и хешам в
`requirements.lock`. Обновление digest выполняется отдельно вместе с проверкой.
Конфигурация опирается на [Docker Compose](https://docs.docker.com/reference/compose-file/services/),
[официальный образ Caddy](https://caddyserver.com/docs/running#docker-compose),
[защиту forwarded-заголовков Caddy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#defaults)
и [настройки доверенного прокси Uvicorn](https://uvicorn.dev/settings/#http).
