# Remna Routing Updater

Сервис публикует проверенные GeoSite/GeoIP и Happ routing-профиль в двух
изолированных режимах:

- `original` — последние stable assets `hydraponique/roscomvpn-geosite` и
  `hydraponique/roscomvpn-geoip` без изменения ни одного байта;
- `custom` — те же базы, но только `whitelist` заменён данными vahellame.

Custom по умолчанию никогда не читает и не изменяет Remnawave. Старый `.env`
совместим: без новых переменных режимом остаётся `original`, а публикация в
панель включена.

## Гарантии обновления

Каждый DAT и checksum берутся из одного ответа конкретного GitHub Release.
Проверяются checksum-файл и `digest` GitHub asset, если он предоставлен. До
публикации проверяются protobuf, CIDR, категории, непустой whitelist и
побайтовая сохранность всех незаменяемых категорий.

Оба DAT, JSON и deeplink сначала записываются в новый каталог
`<mode>/releases/<snapshot-id>`, затем единым `rename(2)` переключается симлинк
`<mode>/current`. Поэтому читатель `current` не видит половину новой пары.
Старые snapshots сохраняются для отката. Статус и lock разделены по режимам.

Публикуются:

```text
<mode>/current/geosite.dat
<mode>/current/geosite.dat.sha256
<mode>/current/geosite.dat.sha256sum
<mode>/current/geoip.dat
<mode>/current/geoip.dat.sha256
<mode>/current/geoip.dat.sha256sum
<mode>/current/manifest.json
<mode>/current/routing.json
<mode>/current/routing.deeplink
<mode>/status.json
```

Immutable URL набора строится как
`/files/<mode>/releases/<snapshot-id>/<filename>`.

## Custom whitelist

GeoSite: категория `whitelist` RoscomVPN заменяется одноимённой категорией
`vahellame/russia-whitelist-geosite`. Типы `plain/domain/full/regexp` и
атрибуты сохраняются в исходном protobuf-сообщении донора.

GeoIP: `whitelist` заменяется точным объединением категорий из
`GEOIP_WHITELIST_CATEGORIES` репозитория
`vahellame/russia-whitelist-geoip`. По умолчанию это `other,vk,yandex`, как в
актуальном `profiles/whitelist.json`. Совпадающие IPv4/IPv6 CIDR удаляются, но
сети не агрегируются и разрешённый диапазон не расширяется. `trash` и
`category-public-dns` запрещены. Инвертированные и имеющие неизвестную
семантику категории отклоняются.

Custom JSON получает отличимое имя и минимальное `geoip:whitelist` в
`DirectIp`, если правила ещё нет. Другие правила, DNS и порядок не меняются.

## Конфигурация

См. [.env.example](.env.example) и
[.env.custom.example](.env.custom.example). Основные параметры:

| Переменная | Значение |
|---|---|
| `GEODATA_MODE` | `original` или `custom` |
| `PUBLISH_TO_REMNA` | Разрешить GET/PATCH панели; default `true` только для original |
| `ROUTING_ASSETS_DIR` | Общий корень; сервис пишет только в `<root>/<mode>` |
| `MODE_OUTPUT_DIR` | Необязательный явный каталог конкретного экземпляра |
| `GEODATA_CHECK_INTERVAL` | Независимый интервал проверки релизов, минимум 60 секунд |
| `GEOIP_WHITELIST_CATEGORIES` | Список GeoIP-категорий донора через запятую |
| `KEEP_RELEASES` | Сколько snapshots хранить, минимум 2 |
| `MAX_WHITELIST_SHRINK_FRACTION` | Допустимое сокращение после первой custom-миграции |

`REMNA_BASE_URL`, `REMNA_TOKEN` и `COOKIE` не нужны и не читаются при
`PUBLISH_TO_REMNA=false`.

## Запуск двух экземпляров

Production original сохраняет пользовательский `.env`. Custom использует
отдельный `.env.custom` без реквизитов панели:

```bash
cp .env.custom.example .env.custom
# Настройте только source/public URLs.

# Сначала безопасный custom:
docker compose --profile custom up -d --build --no-deps routing-custom

# Existing original — отдельно, после подготовки original/current и nginx:
docker compose up -d --build routing-updater
```

В Compose нет `container_name`: имена изолирует Compose project/service. Оба
сервиса монтируют общий корень, но владеют разными каталогами `original` и
`custom`, отдельными lock/state/history.

Разовый запуск и healthcheck:

```bash
docker compose --profile custom run --rm --no-deps routing-custom python app.py --once
docker compose --profile custom exec routing-custom python app.py --healthcheck
```

## Nginx

Существующие production URL должны продолжать указывать на original. После
однократной подготовки `original/current` рекомендуемая схема:

```nginx
# Legacy production URL — не меняется для клиентов.
location = /files/geoip.dat {
    alias /var/www/subscription-files/original/current/geoip.dat;
}
location = /files/geosite.dat {
    alias /var/www/subscription-files/original/current/geosite.dat;
}

location = /files/custom/status.json {
    alias /var/www/subscription-files/custom/status.json;
    add_header Cache-Control "no-store" always;
}
location ^~ /files/custom/releases/ {
    alias /var/www/subscription-files/custom/releases/;
    add_header Cache-Control "public, immutable" always;
}
location ^~ /files/custom/ {
    alias /var/www/subscription-files/custom/current/;
    add_header Cache-Control "no-cache" always;
}
```

Проверка перед reload:

```bash
docker compose -f /opt/remnawave/docker-compose.yml exec -T remnawave-nginx nginx -t
docker compose -f /opt/remnawave/docker-compose.yml restart remnawave-nginx
```

## Remnawave sync

При `PUBLISH_TO_REMNA=true` fingerprint включает профиль без upstream
`LastUpdated` и SHA256 обеих баз. Свой `LastUpdated` меняется только при
реальном изменении. Файлы публикуются до PATCH. Успешная синхронизация
записывается только после повторного GET и подтверждения значения. Ошибка PATCH
не помечается успехом и повторяется после следующего цикла/рестарта. Перед
PATCH настройки перечитываются, а остальные `customResponseHeaders`
сохраняются. Для старого API без `customResponseHeaders` остаётся fallback
`happRouting`.

## External squad в Remnawave 3.x

Remnawave 3.x позволяет external squad добавлять свой header через
`responseHeadersAdd`; это не глобальный `/subscription-settings`. В 3.x endpoint
обновления — `PATCH /api/external-squads`, UUID передаётся в JSON body.

Сначала получите custom deeplink и текущий squad, затем объедините существующие
headers (не заменяйте их пустым объектом):

```bash
CUSTOM_ROUTING=$(tr -d '\n' </opt/remnawave/downloads/custom/current/routing.deeplink)
curl -fsS -H "Authorization: Bearer $REMNA_TOKEN" \
  "$REMNA_BASE_URL/external-squads/$SQUAD_UUID" > /tmp/squad.json
jq --arg routing "$CUSTOM_ROUTING" --arg uuid "$SQUAD_UUID" \
  '{uuid: $uuid, responseHeadersAdd: ((.response.responseHeadersAdd // {}) + {routing: $routing})}' \
  /tmp/squad.json > /tmp/squad-patch.json
curl -fsS -X PATCH -H "Authorization: Bearer $REMNA_TOKEN" \
  -H 'Content-Type: application/json' --data-binary @/tmp/squad-patch.json \
  "$REMNA_BASE_URL/external-squads"
```

Назначение пользователя в external squad — отдельная операция. Этот сервис её
не выполняет. Subscription Response Rules могут переопределить настройки
external squad; SRR с `applyHeadersToEnd=true` имеет финальный приоритет.

## Диагностика и откат

```bash
docker compose --profile custom logs -f routing-custom
docker compose logs -f routing-updater
jq . /opt/remnawave/downloads/custom/status.json
readlink /opt/remnawave/downloads/custom/current
```

Откат custom на сохранённый immutable snapshot:

```bash
cd /opt/remnawave/downloads/custom
ln -s "releases/<snapshot-id>" current.next
mv -Tf current.next current
```

После ручного отката остановите updater или ожидайте, что следующий успешный
цикл снова активирует текущий upstream snapshot.

## Тесты

```bash
python3 -m unittest discover -s tests -v
docker compose config -q
docker compose build
```
