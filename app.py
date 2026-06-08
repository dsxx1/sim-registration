import os
import re
import logging
import httpx
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'change-me-in-production')
CORS(app)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.error("SUPABASE_URL / SUPABASE_KEY не заданы — проверь переменные окружения (.env)")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Анти-спам константы
PHONE_CHECK_LIMIT = 6
PHONE_CHECK_WINDOW = 3600  # 1 час
PHONE_BLOCK_TIME = 3600     # 1 час блокировки


# ──────────────────────────────────────────────
# ВАЛИДАЦИЯ
# ──────────────────────────────────────────────

def validate_phone(phone: str) -> bool:
    """Проверка формата телефона: 10 цифр, начинается с 9"""
    phone = phone.strip()
    return phone.isdigit() and len(phone) == 10 and phone.startswith('9')


def validate_full_name(name: str) -> bool:
    """Проверка ФИО: от 5 до 50 символов, 2-3 слова, только буквы и дефис"""
    name = name.strip()
    if not (5 <= len(name) <= 50):
        return False
    words = name.split()
    if not (2 <= len(words) <= 3):
        return False
    return re.match(r'^[а-яА-ЯёЁa-zA-Z\s\-]+$', name) is not None


def parse_supabase_datetime(datetime_str: str) -> datetime:
    """Парсит дату из Supabase"""
    if not datetime_str:
        return None
    try:
        if datetime_str.endswith('Z'):
            return datetime.fromisoformat(datetime_str[:-1] + '+00:00')
        elif 'T' in datetime_str:
            return datetime.fromisoformat(datetime_str.replace('Z', '+00:00'))
        else:
            dt = datetime.fromisoformat(datetime_str.replace(' ', 'T'))
            return dt.replace(tzinfo=timezone.utc)
    except Exception as e:
        logger.error(f"Error parsing datetime {datetime_str}: {e}")
        return datetime.now(timezone.utc)


# ──────────────────────────────────────────────
# АНТИ-СПАМ ЗАЩИТА (исправленная версия)
# ──────────────────────────────────────────────

def get_client_ip() -> str:
    """Реальный IP клиента за обратным прокси (Railway / Heroku / Nginx).

    За прокси request.remote_addr возвращает IP самого прокси и может меняться
    от запроса к запросу — поэтому счётчик попыток «прыгал». Берём первый адрес
    из X-Forwarded-For (исходный клиент), с откатом на X-Real-IP / remote_addr.
    """
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        ip = xff.split(',')[0].strip()
        if ip:
            return ip
    return request.headers.get('X-Real-IP') or request.remote_addr or 'unknown'


def check_phone_spam(user_ip: str, phone: str) -> tuple:
    """
    Лимит проверок: до PHONE_CHECK_LIMIT РАЗНЫХ номеров в час с одного IP.
    Повторная проверка того же номера попытку НЕ расходует.
    Возвращает: (is_blocked, remaining_seconds, attempts_left)
    """
    now = datetime.now(timezone.utc)

    try:
        # Детерминированно берём самую свежую запись пользователя по IP.
        # order(last_check) защищает от «рандома» при случайных дублях строк.
        resp = (supabase.table('phone_check_attempts')
                .select('*')
                .eq('user_id', user_ip)
                .order('last_check', desc=True)
                .execute())

        # Новый пользователь — первая проверка
        if not resp.data:
            try:
                supabase.table('phone_check_attempts').insert({
                    'user_id': user_ip,
                    'attempt_count': 1,
                    'last_check': now.isoformat(),
                    'blocked_until': None,
                    'checked_phones': [phone]
                }).execute()
            except Exception as e:
                # Возможна гонка: строку уже создал параллельный запрос — не критично
                logger.warning(f"insert race for {user_ip}: {e}")
            logger.info(f"New user {user_ip}: distinct=1, left={PHONE_CHECK_LIMIT - 1}")
            return False, 0, PHONE_CHECK_LIMIT - 1

        data = resp.data[0]
        blocked_until = parse_supabase_datetime(data.get('blocked_until'))
        last_check = parse_supabase_datetime(data['last_check'])
        checked_phones = data.get('checked_phones') or []

        # Активная блокировка
        if blocked_until and now < blocked_until:
            remaining = int((blocked_until - now).total_seconds())
            logger.info(f"User {user_ip} blocked for {remaining}s")
            return True, remaining, 0

        # Окно сброшено: блокировка истекла ИЛИ прошёл час с последней проверки
        window_expired = (
            (blocked_until is not None and now >= blocked_until) or
            (now - last_check).total_seconds() > PHONE_CHECK_WINDOW
        )
        if window_expired:
            checked_phones = []
            logger.info(f"User {user_ip} window reset")

        # Считаем РАЗНЫЕ номера: повтор того же номера попытку не тратит
        already_checked = phone in checked_phones
        if not already_checked:
            checked_phones.append(phone)

        count = len(checked_phones)
        attempts_left = max(0, PHONE_CHECK_LIMIT - count)
        logger.info(
            f"User {user_ip}: distinct={count}, left={attempts_left}, "
            f"phone={phone}, repeat={already_checked}"
        )

        # Превышен лимит разных номеров — блокируем
        if count > PHONE_CHECK_LIMIT:
            blocked_at = now + timedelta(seconds=PHONE_BLOCK_TIME)
            supabase.table('phone_check_attempts').update({
                'attempt_count': count,
                'blocked_until': blocked_at.isoformat(),
                'last_check': now.isoformat(),
                'checked_phones': checked_phones[-PHONE_CHECK_LIMIT:]
            }).eq('user_id', user_ip).execute()
            logger.warning(f"User {user_ip} BLOCKED for {PHONE_BLOCK_TIME}s")
            return True, PHONE_BLOCK_TIME, 0

        # Обновляем счётчик (update по user_id затрагивает все дубли — они сходятся)
        supabase.table('phone_check_attempts').update({
            'attempt_count': count,
            'last_check': now.isoformat(),
            'blocked_until': None,
            'checked_phones': checked_phones
        }).eq('user_id', user_ip).execute()

        return False, 0, attempts_left

    except Exception as e:
        logger.error(f"check_phone_spam error: {e}")
        # В случае ошибки разрешаем проверку (fail open)
        return False, 0, PHONE_CHECK_LIMIT


def reset_phone_spam(user_ip: str):
    """Сбрасывает счётчик после успешной регистрации"""
    try:
        now = datetime.now(timezone.utc)
        supabase.table('phone_check_attempts').update({
            'attempt_count': 0,
            'blocked_until': None,
            'last_check': now.isoformat(),
            'checked_phones': []
        }).eq('user_id', user_ip).execute()
        logger.info(f"User {user_ip} spam counter reset after registration")
    except Exception as e:
        logger.error(f"reset_phone_spam error: {e}")


# ──────────────────────────────────────────────
# TELEGRAM УВЕДОМЛЕНИЯ
# ──────────────────────────────────────────────

def escape_markdown_v2(text: str) -> str:
    """Экранирует спецсимволы для Telegram MarkdownV2"""
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{c}' if c in escape_chars else c for c in str(text))


def send_telegram_notification(phone: str, organization: str, full_name: str, is_reregister: bool = False):
    """Отправляет уведомление в Telegram"""
    bot_token = os.getenv('BOT_TOKEN')
    chat_id = os.getenv('NOTIFICATION_CHAT_ID')
    
    if not bot_token or not chat_id:
        logger.warning("Telegram notifications not configured")
        return
    
    action = "перерегистрирована" if is_reregister else "зарегистрирована"
    current_time = datetime.now(timezone(timedelta(hours=5))).strftime('%d.%m.%Y %H:%M')
    
    text = (
        f"📢 *SIM\\-карта {escape_markdown_v2(action)}*\n\n"
        f"👤 *Сотрудник:* {escape_markdown_v2(full_name)}\n"
        f"📱 *Телефон:* {escape_markdown_v2(phone)}\n"
        f"🏢 *Организация:* {escape_markdown_v2(organization)}\n"
        f"📅 *Дата:* {escape_markdown_v2(current_time)}"
    )
    
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2"
    }
    
    try:
        response = httpx.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info(f"Telegram notification sent for {phone}")
        else:
            logger.error(f"Telegram API error {response.status_code}: {response.text}")
    except Exception as e:
        logger.error(f"send_telegram_notification exception: {e}")


# ──────────────────────────────────────────────
# KEEP-ALIVE (чтобы Supabase не засыпал)
# ──────────────────────────────────────────────

def keep_supabase_alive():
    """Периодически пингует Supabase (раз в 5 минут)"""
    import threading
    import time
    
    def ping():
        while True:
            try:
                time.sleep(300)  # 5 минут
                supabase.table('organizations').select('name').limit(1).execute()
                logger.info("Supabase keep-alive ping OK")
            except Exception as e:
                logger.error(f"Supabase keep-alive failed: {e}")
    
    thread = threading.Thread(target=ping, daemon=True)
    thread.start()
    logger.info("Keep-alive thread started")


# Запускаем keep-alive при старте
keep_supabase_alive()


# ──────────────────────────────────────────────
# API ENDPOINTS
# ──────────────────────────────────────────────

@app.route('/api/organizations', methods=['GET'])
def get_organizations():
    """Получить список организаций"""
    try:
        resp = supabase.table('organizations').select('name').execute()
        return jsonify({'organizations': [o['name'] for o in resp.data]})
    except Exception as e:
        logger.error(f"get_organizations error: {e}")
        return jsonify({'error': 'Failed to fetch organizations'}), 500


@app.route('/api/check-phone', methods=['POST'])
def check_phone_api():
    """Проверка номера телефона"""
    data = request.json or {}
    phone = data.get('phone', '').strip()
    user_ip = get_client_ip()

    if not phone:
        return jsonify({'error': 'Phone number required'}), 400
    
    if not validate_phone(phone):
        return jsonify({'error': 'Неверный формат. Нужно 10 цифр, начиная с 9'}), 400
    
    # Проверка спама
    is_blocked, remaining, attempts_left = check_phone_spam(user_ip, phone)
    
    if is_blocked:
        minutes = remaining // 60
        seconds = remaining % 60
        return jsonify({
            'blocked': True,
            'message': f'Превышен лимит проверок. Попробуйте через {minutes} мин {seconds} сек',
            'remaining_seconds': remaining
        }), 403
    
    try:
        # Проверка в таблице разрешённых номеров
        valid_check = supabase.table('valid_numbers').select('*').eq('phone', phone).execute()
        
        if not valid_check.data:
            return jsonify({
                'valid': False,
                'message': f'Номер не найден в базе разрешённых номеров. Осталось попыток: {attempts_left}'
            }), 404
        
        # Проверка существующей регистрации
        existing = supabase.table('sim_registrations').select('*').eq('phone', phone).eq('is_active', True).execute()
        
        if existing.data:
            reg = existing.data[0]
            return jsonify({
                'exists': True,
                'phone': phone,
                'full_name': reg['full_name'],
                'organization': reg['organization'],
                'created_at': reg['created_at'][:10],
                'attempts_left': attempts_left
            })
        
        return jsonify({
            'valid': True,
            'phone': phone,
            'attempts_left': attempts_left,
            'message': f'Номер подтверждён. Осталось попыток: {attempts_left}'
        })
        
    except Exception as e:
        logger.error(f"check_phone_api error: {e}")
        return jsonify({'error': 'Ошибка сервера'}), 500


@app.route('/api/register', methods=['POST'])
def register_sim():
    """Регистрация SIM-карты"""
    data = request.json or {}
    phone = data.get('phone', '').strip()
    organization = data.get('organization', '').strip()
    full_name = data.get('full_name', '').strip()
    user_ip = get_client_ip()

    if not all([phone, organization, full_name]):
        return jsonify({'error': 'Все поля обязательны'}), 400
    
    if not validate_phone(phone):
        return jsonify({'error': 'Неверный формат номера'}), 400
    
    if not validate_full_name(full_name):
        return jsonify({'error': 'Некорректное ФИО. Формат: Иванов Иван Иванович'}), 400
    
    try:
        # Проверяем существующую активную запись
        existing = supabase.table('sim_registrations').select('*').eq('phone', phone).eq('is_active', True).execute()
        is_reregister = bool(existing.data)
        
        # Деактивируем старую запись
        if existing.data:
            supabase.table('sim_registrations').update({'is_active': False}).eq('phone', phone).execute()
            logger.info(f"Deactivated old registration for {phone}")
        
        # Создаём новую запись
        created_at = datetime.now(timezone(timedelta(hours=5))).isoformat()
        new_reg = {
            'phone': phone[:10],
            'full_name': full_name[:100],
            'belongs_to': full_name[:100],
            'comment': '',
            'organization': organization[:100],
            'is_active': True,
            'created_at': created_at
        }
        
        supabase.table('sim_registrations').insert(new_reg).execute()
        logger.info(f"New registration saved: {phone} - {full_name}")
        
        # Сбрасываем спам-счётчик
        reset_phone_spam(user_ip)
        
        # Отправляем уведомление в Telegram
        send_telegram_notification(phone, organization, full_name, is_reregister)
        
        prefix = "пере" if is_reregister else ""
        return jsonify({
            'success': True,
            'message': f'SIM-карта успешно {prefix}зарегистрирована\nНомер: {phone}\nОрганизация: {organization}\nСотрудник: {full_name}'
        })
        
    except Exception as e:
        logger.error(f"register_sim error: {e}")
        return jsonify({'error': 'Ошибка при сохранении данных'}), 500


@app.route('/')
def index():
    """Главная страница"""
    return render_template('index.html')


@app.route('/health')
def health():
    """Проверка работоспособности"""
    return jsonify({'status': 'ok'}), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
