# alphaconsultantbot: ИИ-помощник Альфа-Курса
## Запуск

1. Установите Docker и [Ollama](https://ollama.com). Скачайте модель: ollama pull qwen3:8b
   (нужно около 6 ГБ памяти; на слабом компьютере возьмите qwen3:4b и укажите её в LLM_MODEL).
2. Создайте бота у @BotFather и получите токен.
3. Подготовьте настройки и данные:
   bash
   cp env.example .env              # впишите токен от бота
   mkdir -p data && cp /путь/к/alfa_course_hackathon.backup data/
   
4. docker compose up --build

При первом старте бот сам строит индекс data/kb.db из дампа (около минуты).
Дамп и индекс лежат в data/, необходимо.

Ollama в контейнере вместо установленной на компьютере: в .env укажите LLM_BASE_URL=http://ollama:11434/v1,
запускайте docker compose --profile local-llm up --build и один раз выполните
docker compose exec ollama ollama pull qwen3:8b.

Сценарий ручной проверки всех пунктов: TESTING.md.

## Как бот отвечает

1. Вопрос превращается в основы слов.
2. Поиск BM25 по бесплатным опубликованным материалам.
3. Проверка релевантности: доля слов запроса во фрагменте, взвешенная по редкости слов MIN_COVERAGE).
   Не нашлось, значит ответ "в базе нет", без обращения к модели.
4. Модель получает вопрос и до 4 фрагментов и отвечает только по ним; если ответа в них нет, возвращает НЕТ_ОТВЕТА.
5. К ответу добавляются источники со ссылками и UTM-метками.
6. Ollama недоступна: бот показывает самый подходящий фрагмент вместо ошибки.

## Проверки без Telegram и без модели

python -m tests.test_pipeline, test_clarify, test_followup, test_handlers, test_profile, test_recommend (все через python -m tests.<имя>)
(нужен data/kb.db; вместо Ollama используется заглушка).
