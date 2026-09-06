# Аренда в Армении

Telegram Mini App: аренда без комиссии и предложения агентов. Лента фото, сетка, поиск, подача, «Мои» и модерация.

Подача: жильё → адрес → цена → фото и необязательное описание. Телефон необязателен. Контакт — Telegram автора; без username — обсуждение публикации. Агент один раз заполняет имя, телефон и агентство для администратора; в карточке видны только агентство и комиссия. Даты аренды и время в пути вводятся вручную.

## Запуск

```sh
cp .env.example .env
# Заполнить локально BOT_TOKEN, BOT_USERNAME, ADMIN_IDS и PUBLIC_URL.
docker compose up -d --build
```

Нужен доверенный HTTPS. LIVE=1 включает Telegram; LIVE=0 позволяет запускать API локально без poller. Данные — data/rent.sqlite3. Карточки из архива Telegram загружаются отдельно от кода, с исходными датами и ссылками; повторной публикации нет.

PUBLISH_CHAT_ID/PUBLISH_THREAD_ID — публикации без комиссии; PUBLISH_PAID_CHAT_ID/PUBLISH_PAID_THREAD_ID — агентские. Пустые значения отключают публикацию. Пока обсуждение не подключено, его кнопка недоступна.

Один FastAPI-процесс, SQLite/WAL, 256 MiB RAM. Node, Redis и Postgres для запуска не нужны. Фото отправляются в чат бота; Telegram хранит их и готовит размеры. Сервер хранит file_id и передаёт фото без обработки, скрывая токен. Фото публичных постов браузер загружает напрямую с Telegram CDN. Галерея — PhotoSwipe (MIT, web/vendor).

## Изменения и обслуживание

Редактировать web/app.js, core.js, style.css и index.template.html; затем `python build.py`.

```sh
pip install -r requirements-dev.txt
python -m pytest -q
node tests/test_core.js
python tests/mobile_smoke.py
python tests/mobile_moderation.py
python tests/mobile_gallery.py
```

Для браузерных тестов установить Chromium: `python -m playwright install chromium` или задать CHROMIUM_PATH.

Перед обновлением: `python scripts/backup.py --database data/rent.sqlite3 --output /secure/backup.sqlite3`. Копия исключает реквизиты проверки. Сохранять том и private.key; обновлять только app, не затрагивая соседние сервисы. HTTPS-примеры — deploy/. Один токен — один poller.

Проверка собственности добровольная: администратор сверяет документ через e-cadastre, отдельно личность и полномочия. Номер и пароль документа шифруются и удаляются после решения или через 7 дней. Копии документов не загружаются.

.env, ключи, базы, фотографии и экспорты не публикуются. Перед push обязательны ревью и проверка секретов.
