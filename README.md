# AI Orb Assistant

Красивый голосовой AI-помощник в виде интерактивного шара.

## Что уже реализовано

- Русский / English интерфейс и распознавание речи.
- Нажатие на шар запускает микрофон.
- Повторное нажатие отправляет запись.
- Визуальная реакция шара на громкость микрофона.
- Плавающий "газ" вокруг шара.
- Автоматическая отправка после паузы.
- Groq Whisper для STT.
- Groq LLM для маршрутизации: обычный ответ / изображение / погода.
- Cloudflare Workers AI + FLUX для изображений.
- OpenWeather для текущей погоды.
- Groq TTS для English.
- Browser SpeechSynthesis fallback для русского TTS.
- Регистрация / вход.
- История разговоров в MongoDB.
- Один Render Web Service: API + frontend.
- Все секреты только через environment variables.

## Локальный запуск

```bash
cp .env.example .env
npm install
npm start
```

Откройте `http://localhost:10000`.

## Render

Самый простой вариант:

1. Загрузите этот проект в ваш GitHub repository.
2. В Render: New → Web Service → подключите repository.
3. Build Command: `npm install`
4. Start Command: `npm start`
5. Добавьте environment variables из `.env.example`.

Можно также использовать `render.yaml` через Blueprint.

## Environment variables

Обязательные:

- `MONGODB_URI`
- `JWT_SECRET`
- `GROQ_API_KEY`
- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`
- `OPENWEATHER_API_KEY`

Не добавляйте реальные ключи в GitHub.

## MongoDB

Строка должна быть примерно:

`mongodb+srv://USER:PASSWORD@CLUSTER.mongodb.net/ai-orb?retryWrites=true&w=majority`

Также разрешите Render подключаться к MongoDB Atlas в Network Access. Для быстрого теста можно временно разрешить `0.0.0.0/0`, а для production лучше ограничить доступ согласно вашей MongoDB/сетевой схеме.

## Cloudflare

По умолчанию используется:

`@cf/black-forest-labs/flux-1-schnell`

Это можно заменить переменной `CLOUDFLARE_IMAGE_MODEL`.

## Важно про русский TTS

Актуальная документация Groq перечисляет для TTS английскую и саудовскую арабскую модели. Поэтому проект получает русский текст и произносит его через системный `SpeechSynthesis`, а English озвучивает через Groq. Когда появится подходящая русская Groq-модель, достаточно заменить функцию TTS.

## Следующий этап

После первого деплоя можно добавить:

- потоковую STT/TTS без ожидания окончания записи;
- более умный turn detection;
- красивую историю разговоров и боковую панель;
- редактирование/удаление разговоров;
- хранение изображений в Cloudflare R2 вместо data URI;
- PWA и установку на телефон;
- tool/action framework для новых действий;
- production rate limits и refresh tokens.
