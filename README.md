# Remna Routing Updater

Микросервис для автоматического обновления `happRouting` в Remna панели при появлении новых данных в GitHub-репозитории [roscomvpn-happ-routing](https://github.com/hydraponique/roscomvpn-happ-routing).

## Как работает

1. При запуске получает текущие настройки подписки из Remna API (`GET /subscription-settings`)
2. Периодически скачивает `DEFAULT.DEEPLINK` из GitHub-репозитория
3. Декодирует диплинк из `base64` в JSON
4. Скачивает `geoip.dat` и `geosite.dat` по URL из JSON или по URL, заданным через env
5. Сохраняет файлы в каталог `ROUTING_ASSETS_DIR` как `geoip.dat` и `geosite.dat` с заменой существующих файлов
6. Подменяет `Geoipurl` и `Geositeurl` в JSON на ваши URL
7. Кодирует JSON обратно в `base64` и отправляет обновление в Remna (`PATCH /subscription-settings`)

## Быстрый старт

```bash
mkdir remna-routing-updater && cd remna-routing-updater
```

### Внешняя панель (HTTPS)

Создайте файл `.env`:

```env
REMNA_BASE_URL=https://your-host/api
REMNA_TOKEN=your_bearer_token
# если API дополнительно требует cookie:
# COOKIE=panel_auth=abc123secret
GITHUB_RAW_URL=https://raw.githubusercontent.com/hydraponique/roscomvpn-happ-routing/refs/heads/main/HAPP/DEFAULT.DEEPLINK
CHECK_INTERVAL=300
ROUTING_ASSETS_DIR=/opt/remnawave/downloads
GEOIP_PUBLIC_URL=https://your-host/routing/geoip.dat
GEOSITE_PUBLIC_URL=https://your-host/routing/geosite.dat
DEEPLINK_PREFIX=happ://routing/add/
```

Создайте файл `docker-compose.yml`:

```yaml
services:
  routing-updater:
    build:
      context: .
    image: remna-routing-updater:local
    container_name: remna-routing-updater
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ${ROUTING_ASSETS_DIR}:${ROUTING_ASSETS_DIR}
```

### Локальная панель (Docker)

Если RemnaWave панель запущена локально в Docker (образ `remnawave/backend:latest`), контейнер updater нужно подключить к той же сети `remnawave-network` и обращаться к панели по имени контейнера.

Создайте файл `.env`:

```env
REMNA_BASE_URL=http://remnawave-backend:3000/api
REMNA_TOKEN=your_bearer_token
# если API дополнительно требует cookie:
# COOKIE=panel_auth=abc123secret
GITHUB_RAW_URL=https://raw.githubusercontent.com/hydraponique/roscomvpn-happ-routing/refs/heads/main/HAPP/DEFAULT.DEEPLINK
CHECK_INTERVAL=300
ROUTING_ASSETS_DIR=/opt/remnawave/downloads
GEOIP_PUBLIC_URL=https://your-host/routing/geoip.dat
GEOSITE_PUBLIC_URL=https://your-host/routing/geosite.dat
DEEPLINK_PREFIX=happ://routing/add/
```

> `remnawave-backend` — имя контейнера панели, `3000` — порт по умолчанию. Измените при необходимости.

Создайте файл `docker-compose.yml`:

```yaml
services:
  routing-updater:
    build:
      context: .
    image: remna-routing-updater:local
    container_name: remna-routing-updater
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ${ROUTING_ASSETS_DIR}:${ROUTING_ASSETS_DIR}
    networks:
      - remnawave-network

networks:
  remnawave-network:
    name: remnawave-network
    external: true
```

> Сеть `remnawave-network` должна уже существовать (создаётся docker-compose панели RemnaWave).
>
> Docker bind mount берёт путь из `ROUTING_ASSETS_DIR`, поэтому эта директория будет использоваться и внутри контейнера, и на хосте.
>
> Файлы всегда сохраняются как:
>
> ```env
> ROUTING_ASSETS_DIR=/opt/remnawave/downloads
> ```
>
> Итоговые пути будут:
>
> - `/opt/remnawave/downloads/geoip.dat`
> - `/opt/remnawave/downloads/geosite.dat`
>
> Эту же директорию нужно раздавать вашим HTTP-сервером так, чтобы `GEOIP_PUBLIC_URL` и `GEOSITE_PUBLIC_URL` были доступны клиентам.

Запуск:

```bash
docker compose up -d --build
```

### Сборка из исходников

Если хотите собрать образ самостоятельно:

```bash
git clone https://github.com/lifeindarkside/Remnawave-Routing-update.git
cd Remnawave-Routing-update
cp .env.example .env
# отредактируйте .env
docker build -t remna-routing-updater .
docker compose up -d --build
```

## Переменные окружения

| Переменная | Обязательная | По умолчанию | Описание |
|---|---|---|---|
| `REMNA_BASE_URL` | да | — | Базовый URL API Remna (например `https://host/api` или `http://remnawave-backend:3000/api`) |
| `REMNA_TOKEN` | да | — | Bearer-токен для авторизации в Remna API |
| `COOKIE` | нет | — | Cookie для дополнительной авторизации в API, например `panel_auth=abc123secret` |
| `GITHUB_RAW_URL` | нет | [DEFAULT.DEEPLINK](https://raw.githubusercontent.com/hydraponique/roscomvpn-happ-routing/refs/heads/main/HAPP/DEFAULT.DEEPLINK) | URL файла с роутингом на GitHub |
| `CHECK_INTERVAL` | нет | `300` | Интервал проверки обновлений (в секундах) |
| `ROUTING_ASSETS_DIR` | нет | `/opt/remnawave/downloads` | Каталог-таргет для файлов на хосте и внутри контейнера. Сервис всегда сохраняет туда `geoip.dat` и `geosite.dat` |
| `GEOIP_PUBLIC_URL` | нет | исходный `Geoipurl` из JSON | URL, который будет записан в JSON вместо исходного `Geoipurl` |
| `GEOSITE_PUBLIC_URL` | нет | исходный `Geositeurl` из JSON | URL, который будет записан в JSON вместо исходного `Geositeurl` |
| `DEEPLINK_PREFIX` | нет | `happ://routing/add/` | Префикс deeplink, который будет собран перед закодированным payload |

## Что в итоге уходит в Remna

В Remna отправляется уже модифицированный `happRouting`:

1. исходный диплинк скачан из GitHub
2. JSON внутри декодирован
3. `geoip.dat` и `geosite.dat` сохранены локально
4. `Geoipurl` и `Geositeurl` заменены на ваши URL
5. JSON снова закодирован в `base64`
6. перед `base64` добавлен префикс из `DEEPLINK_PREFIX`, по умолчанию `happ://routing/add/`

## Сравнение обновлений

Сервис сравнивает диплинки по полю `LastUpdated`.

- Если `LastUpdated` в исходном диплинке отсутствует, обновление пропускается.
- Если текущий `happRouting` пустой, битый или без `LastUpdated`, сервис считает, что обновление требуется.

## Логи

```bash
docker compose logs -f
```

## Лицензия

MIT
