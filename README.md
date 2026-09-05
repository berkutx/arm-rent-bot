# Аренда в Армении

Telegram Mini App для аренды жилья в Армении: без комиссии и через агентов с комиссией. FastAPI, SQLite и обычный JavaScript; один процесс, без Redis и Postgres.

Фотографии доступны в ленте альбомов, компактной сетке и PhotoSwipe с увеличением. Есть поиск по городу, цене и комнатам, ручная подача, фотографии, подписки, модерация с причиной бана и уникальные просмотры. Проверка собственности — отдельная ручная сверка администратором через e-cadastre.

## Запуск

Python 3.11+:

```sh
python -m venv .venv
# Linux/macOS:
. .venv/bin/activate
# Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts/seed_demo.py
python server.py
```

Открыть http://127.0.0.1:8000. По умолчанию `LIVE=0`, Telegram не вызывается. Готовый `web/index.html` можно открыть и без сервера.

`seed.json` содержит **39 вымышленных примеров** без реальных контактов, исходных сообщений и фотографий. Демо не импортируется в рабочую базу.

Для бота скопировать `.env.example` в `.env` и заполнить локально. Нужны HTTPS, токен бота и числовые ID администраторов. [Размещение](docs/DEPLOYMENT.md).

## Разработка

```sh
pip install -r requirements-dev.txt
python build.py
python -m pytest -q
node tests/test_core.js
python -m playwright install chromium
python tests/mobile_smoke.py
python tests/mobile_moderation.py
python tests/mobile_gallery.py
```

Редактировать `web/app.js`, `web/core.js`, `web/style.css` и `web/index.template.html`; затем запускать `build.py`. Node нужен только для JS-тестов, не для работы приложения.

- [Правила продукта](docs/PRODUCT.md)
- [Проверка собственности](docs/VERIFICATION.md)
- [Тесты и ограничения](docs/TESTING.md)
- [Отложенные задачи](docs/TODO.md)

Экспорты Telegram, базы, ключи, `.env`, фотографии и сведения о частном сервере исключены из Git. Публичная версия не содержит фактически проверенных объектов.

PhotoSwipe 5.4.4 включён локально из [официального проекта](https://github.com/dimsemenov/PhotoSwipe); лицензия MIT — `web/vendor/PHOTOSWIPE-LICENSE`.
