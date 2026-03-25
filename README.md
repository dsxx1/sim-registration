```markdown
# SIM-карт Регистрация

Веб-приложение для регистрации SIM-карт сотрудников с интеграцией Supabase и Telegram уведомлениями.

## 🚀 Функционал

- Проверка номера телефона по базе разрешённых номеров
- Антиспам защита: 6 проверок в час, блокировка на 1 час
- Поэтапное заполнение: номер → организация → ФИО
- Автоматические уведомления в Telegram при регистрации
- Перерегистрация существующих номеров
- Валидация ФИО: фамилия, имя, отчество (3 слова)

## 🛠 Технологии

- **Backend**: Flask 2.3.3
- **Database**: Supabase (PostgreSQL)
- **Frontend**: HTML5, CSS3, JavaScript
- **Notifications**: Telegram Bot API
- **Deployment**: Railway

## 📦 Установка

1. Клонировать репозиторий:
```bash
git clone https://github.com/dsxx1/sim-registration.git
cd sim-registration
```

2. Создать виртуальное окружение:
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
```

3. Установить зависимости:
```bash
pip install -r requirements.txt
```

4. Создать файл `.env` (см. `.env.example`):
```env
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_key
BOT_TOKEN=your_telegram_bot_token
NOTIFICATION_CHAT_ID=your_chat_id
FLASK_SECRET_KEY=your_secret_key
```

5. Запустить локально:
```bash
python app.py
```

## 🚀 Деплой на Railway

1. Залей код на GitHub
2. В Railway создай новый проект → Deploy from GitHub
3. Добавь переменные окружения (см. `.env.example`)
4. Railway автоматически запустит приложение

## 📁 Структура проекта

```
sim-registration/
├── app.py              # Flask приложение
├── requirements.txt    # Python зависимости
├── Procfile           # Запуск на Railway
├── .env.example       # Шаблон переменных окружения
├── .gitignore         # Исключения git
├── README.md          # Документация
└── templates/
    └── index.html     # HTML форма регистрации
```

## 📝 Переменные окружения

| Переменная | Описание |
|------------|----------|
| `SUPABASE_URL` | URL Supabase проекта |
| `SUPABASE_KEY` | API ключ Supabase |
| `BOT_TOKEN` | Токен Telegram бота |
| `NOTIFICATION_CHAT_ID` | ID чата для уведомлений |
| `FLASK_SECRET_KEY` | Секретный ключ Flask |

## 🔄 Логика работы

1. **Проверка номера**: пользователь вводит номер → проверка в таблице `valid_numbers`
2. **Антиспам**: при каждом запросе увеличивается счётчик (6 попыток в час)
3. **Выбор организации**: из таблицы `organizations`
4. **Ввод ФИО**: валидация (3 слова, только буквы)
5. **Сохранение**: запись в `sim_registrations`, старая запись деактивируется
6. **Уведомление**: отправка в Telegram

## 🗄 Таблицы Supabase

### `organizations`
| Поле | Тип | Описание |
|------|-----|----------|
| `name` | TEXT | Название организации |

### `valid_numbers`
| Поле | Тип | Описание |
|------|-----|----------|
| `phone` | TEXT | Разрешённые номера (10 цифр) |

### `sim_registrations`
| Поле | Тип | Описание |
|------|-----|----------|
| `phone` | TEXT | Номер телефона |
| `full_name` | TEXT | ФИО сотрудника |
| `belongs_to` | TEXT | Кому принадлежит |
| `comment` | TEXT | Комментарий |
| `organization` | TEXT | Организация |
| `is_active` | BOOLEAN | Активная запись |
| `created_at` | TIMESTAMP | Дата создания |

### `phone_check_attempts`
| Поле | Тип | Описание |
|------|-----|----------|
| `id` | BIGSERIAL | Первичный ключ |
| `user_id` | TEXT | IP адрес пользователя |
| `attempt_count` | INTEGER | Количество попыток проверки |
| `last_check` | TIMESTAMP | Время последней проверки |
| `blocked_until` | TIMESTAMP | Время окончания блокировки |
| `checked_phones` | JSONB | Список проверенных номеров |
| `created_at` | TIMESTAMP | Дата создания записи |

## 🔧 SQL для создания таблиц

Выполни в Supabase SQL Editor:

```sql
-- Таблица организаций
CREATE TABLE IF NOT EXISTS organizations (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Таблица разрешённых номеров
CREATE TABLE IF NOT EXISTS valid_numbers (
    id BIGSERIAL PRIMARY KEY,
    phone TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Таблица регистраций SIM-карт
CREATE TABLE IF NOT EXISTS sim_registrations (
    id BIGSERIAL PRIMARY KEY,
    phone TEXT NOT NULL,
    full_name TEXT NOT NULL,
    belongs_to TEXT,
    comment TEXT,
    organization TEXT NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Таблица для антиспам защиты
CREATE TABLE IF NOT EXISTS phone_check_attempts (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    attempt_count INTEGER DEFAULT 0,
    last_check TIMESTAMP WITH TIME ZONE,
    blocked_until TIMESTAMP WITH TIME ZONE,
    checked_phones JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Индексы для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_sim_registrations_phone ON sim_registrations(phone);
CREATE INDEX IF NOT EXISTS idx_sim_registrations_active ON sim_registrations(is_active);
CREATE INDEX IF NOT EXISTS idx_phone_check_attempts_user_id ON phone_check_attempts(user_id);
```

## 📱 Telegram бот

Для уведомлений нужен Telegram бот:

1. Создать бота через [@BotFather](https://t.me/BotFather)
2. Получить токен вида `123456789:AAHxxxxxx`
3. Узнать ID чата через [@userinfobot](https://t.me/userinfobot)
4. Добавить бота в канал (если нужно) как администратора
