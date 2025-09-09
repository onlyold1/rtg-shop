
1.  **Клонируйте репозиторий:**
    ```bash
    git clone https://github.com/machka-pasla/remnawave-tg-shop
    cd remnawave-tg-shop
    ```

2.  **Создайте и настройте файл `.env`:**
    Скопируйте `env.example` в `.env` и заполните своими данными.
    ```bash
    cp .env.example .env
    nano .env 
    ```

3.  **Запустите контейнеры:**
    ```bash
    docker compose up -d
    ```
    Эта команда скачает образ и запустит сервис в фоновом режиме.

4.  **Настройка вебхуков (Обязательно):**
    Вебхуки являются **обязательным** компонентом для работы бота, так как они используются для получения уведомлений от платежных систем (YooKassa, CryptoPay, Tribute) и панели Remnawave.

    Вам понадобится обратный прокси (например, Nginx) для обработки HTTPS-трафика и перенаправления запросов на контейнер с ботом.

    **Пути для перенаправления:**
    -   `https://<ваш_домен>/webhook/yookassa` → `http://remnawave-tg-shop:<WEB_SERVER_PORT>/webhook/yookassa`
    -   `https://<ваш_домен>/webhook/cryptopay` → `http://remnawave-tg-shop:<WEB_SERVER_PORT>/webhook/cryptopay`
    -   `https://<ваш_домен>/webhook/tribute` → `http://remnawave-tg-shop:<WEB_SERVER_PORT>/webhook/tribute`
    -   `https://<ваш_домен>/webhook/panel` → `http://remnawave-tg-shop:<WEB_SERVER_PORT>/webhook/panel`
    -   **Для Telegram:** Бот автоматически установит вебхук, если в `.env` указан `WEBHOOK_BASE_URL`. Путь будет `https://<ваш_домен>/<BOT_TOKEN>`.

    Где `remnawave-tg-shop` — это имя сервиса из `docker-compose.yml`, а `<WEB_SERVER_PORT>` — порт, указанный в `.env`.

5.  **Просмотр логов:**
    ```bash
    docker compose logs -f remnawave-tg-shop
    ```

## 🐳 Docker

Файлы `Dockerfile` и `docker-compose.yml` уже настроены для сборки и запуска проекта. `docker-compose.yml` использует готовый образ с GitHub Container Registry, но вы можете раскомментировать `build: .` для локальной сборки.

## 📁 Структура проекта

```
.
├── bot/
│   ├── filters/          # Пользовательские фильтры Aiogram
│   ├── handlers/         # Обработчики сообщений и колбэков
│   ├── keyboards/        # Клавиатуры
│   ├── middlewares/      # Промежуточные слои (i18n, проверка бана)
│   ├── services/         # Бизнес-логика (платежи, API панели)
│   ├── states/           # Состояния FSM
│   └── main_bot.py       # Основная логика бота
├── config/
│   └── settings.py       # Настройки Pydantic
├── db/
│   ├── dal/              # Слой доступа к данным (DAL)
│   ├── database_setup.py # Настройка БД
│   └── models.py         # Модели SQLAlchemy
├── locales/              # Файлы локализации (ru, en)
├── .env.example          # Пример файла с переменными окружения
├── Dockerfile            # Инструкции для сборки Docker-образа
├── docker-compose.yml    # Файл для оркестрации контейнеров
├── requirements.txt      # Зависимости Python
└── main.py               # Точка входа в приложение
```

