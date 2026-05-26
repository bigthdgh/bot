# 🚀 ИНСТРУКЦИЯ ПО ДЕПЛОЮ НА RENDER

## 📋 ЧТО СДЕЛАНО:

✅ **requirements.txt** — добавлены `libsql-client` и `python-dotenv`  
✅ **main.py** — убраны хардкод токенов, только `os.getenv()`  
✅ **render.yaml** — конфиг для деплоя  
✅ **.env.example** — пример переменных с твоими токенами Turso  

---

## 🗄️ ПРО TURSO БАЗУ ДАННЫХ

**Твои токены:**
- URL: `libsql://ngcgh-bigthdgh.aws-eu-west-1.turso.io`
- Token: `jvhvj-bigthdgh.aws-eu-west-1.turso.io`

**Почему Turso сейчас НЕ используется:**
- Turso требует async-клиент (`libsql-client`)
- В боте 119 мест с `cursor.execute()` — все синхронные
- Переписывать весь код на async = несколько часов работы

**Что сейчас:**
- Бот использует локальную SQLite (`uzdechka_bot.db`)
- На Render Free данные **будут теряться** при каждом рестарте/деплое

**Решения:**
1. **Render Paid + Disk** ($7/мес) — сохранит локальный файл БД
2. **Переписать на Turso** — я могу это сделать, но займёт время
3. **Смириться с потерей данных** — для тестового бота ок

---

## 🚀 ДЕПЛОЙ НА RENDER (ШАГ ЗА ШАГОМ)

### Шаг 1: Создай .env файл
Скопируй `.env.example` в `.env`:
```bash
copy .env.example .env
```

### Шаг 2: Запуши код на GitHub
```bash
git init
git add .
git commit -m "Подготовка к деплою на Render"
git branch -M main
git remote add origin https://github.com/ТВОЙ_ЮЗЕРНАЙМ/uzdechka-bot.git
git push -u origin main
```

### Шаг 3: Создай Web Service на Render
1. Иди на [dashboard.render.com](https://dashboard.render.com)
2. **New +** → **Web Service**
3. **Build and deploy from a Git repository** → выбери свой репозиторий
4. Заполни:
   - **Name:** `uzdechka-bot`
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`
5. В разделе **Environment** добавь переменные:

| Переменная | Значение |
|-----------|----------|
| `BOT_TOKEN` | `8562246018:AAEc8_JPt4JgG_dqyfLyFhCncgNAzVSXNUk` |
| `GEMINI_API_KEY` | `AIzaSyDFJN_lKPoOaiKZNnIIMreZeD3vHHEcc7Y` |
| `GEMINI_API_KEYS` | `AIzaSyDFJN_lKPoOaiKZNnIIMreZeD3vHHEcc7Y,AIzaSyBThjpc0d83c7wfGx2ZCU2Fa7EyBVE2anU,AIzaSyDDMttAv3h1Z8TFJe4InNmF3N9AY-jQyqI,AIzaSyCdt69OipMDFyZ9vbWKRRRad9wOWVaUUWw,AIzaSyCvuz2k-UMX7JzuCMm58sDktGhpPzauIVM` |
| `GEMINI_MODEL` | `gemini-2.5-flash` |
| `QWEN_API_KEY` | `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` |
| `QWEN_MODEL` | `qwen/qwen3-next-80b-a3b-instruct:free` |
| `QWEN_ENABLED` | `true` |
| `ADMIN_IDS` | `8425434588,8062523010` |
| `MATVEY_ID` | `2076532055` |
| `AI_PHRASE_CHANCE` | `0.35` |

6. Нажми **Create Web Service**

### Шаг 4: Жди деплоя
Render автоматически:
- Установит зависимости из `requirements.txt`
- Запустит `python main.py`
- Бот начнёт работать

---

## ⚠️ ВАЖНОЕ ПРЕДУПРЕЖДЕНИЕ

**На Render Free:**
- Бот будет работать 24/7
- Но данные БД **обнулятся** при каждом деплое/рестарте
- XP, теги, админки — всё слетит

**Решения:**
1. **Render Paid + Disk** ($7/мес) — данные сохранятся
2. **Переписать на Turso** — я могу сделать, но нужно время
3. **Использовать как тестовый бот** — потеря данных не критична

---

## 🔧 ХОЧЕШЬ TURSO?

Если хочешь, чтобы данные сохранялись на Turso — скажи, я перепишу код. Это займёт ~30-40 минут:
- Заменю все `cursor.execute()` на async версии
- Добавлю `await` перед всеми SQL-запросами
- Протестирую локально

**Преимущества Turso:**
- Данные сохраняются в облаке
- Бесплатный tier: 500MB, 5M reads/мес
- Работает на Render Free без потери данных

---

## 📝 ЧЕКЛИСТ ПЕРЕД ДЕПЛОЕМ

- [ ] Создан `.env` файл
- [ ] Код запушен на GitHub
- [ ] Создан Web Service на Render
- [ ] Environment Variables добавлены
- [ ] Бот запустился и работает

---

## 🆘 ПРОБЛЕМЫ?

Если бот не запускается:
1. Проверь логи в Render Dashboard → Logs
2. Убедись, что все переменные окружения заданы
3. Проверь, что `BOT_TOKEN` правильный

Если БД обнуляется:
1. Это нормально для Render Free
2. Либо переходи на Render Paid + Disk
3. Либо проси меня переписать на Turso

---

**Готов к деплою?** Скажи, если нужна помощь с GitHub или Render!
