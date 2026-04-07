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
supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

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
    """Парсит дату из Supabase. Возвращает None при ошибке."""
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
        logger.error(f"Error parsing datetime '{datetime_str}': {e}")
        return None 


# ──────────────────────────────────────────────
# АНТИ-СПАМ ЗАЩИТА (исправленная версия)
# ──────────────────────────────────────────────

def check_phone_spam(user_ip: str, phone: str) -> tuple:
    """
    Проверяет лимит проверок номера (6 в час)
    Возвращает: (is_blocked, remaining_seconds, attempts_left)
    """
    now = datetime.now(timezone.utc)
    
    try:
        # Получаем данные пользователя по IP
        resp = supabase.table('phone_check_attempts').select('*').eq('user_id', user_ip).execute()
        

        if not resp.data:  
            supabase.table('phone_check_attempts').upsert({
                'user_id': user_ip,
                'attempt_count': 1,
                'last_check': now.isoformat(),
                'blocked_until': None,
                'checked_phones': [phone]
            }, on_conflict='user_id').execute()
            logger.info(f"SPAM: New user {user_ip}, count=1, left={PHONE_CHECK_LIMIT - 1}")
            return False, 0, PHONE_CHECK_LIMIT - 1
        
        data = resp.data[0]
        
        # Безопасный парсинг дат
        blocked_until_raw = data.get('blocked_until')
        blocked_until = parse_supabase_datetime(blocked_until_raw) if blocked_until_raw else None
        
        last_check_raw = data.get('last_check')
        last_check = parse_supabase_datetime(last_check_raw) if last_check_raw else None
        
        count = data.get('attempt_count', 0)
        checked_phones = data.get('checked_phones', []) or []
        
        # 🔒 1. Проверяем активную блокировку
        if blocked_until and now < blocked_until:
            remaining = int((blocked_until - now).total_seconds())
            logger.warning(f"SPAM: User {user_ip} BLOCKED, remaining={remaining}s")
            return True, remaining, 0
        
        # 🔄 2. Если блокировка истекла — полный сброс
        if blocked_until and now >= blocked_until:
            count = 0
            checked_phones = []
            logger.info(f"SPAM: User {user_ip} unblocked, resetting counter")
        
        # 🕐 3. Сброс по времени (скользящее окно)
        if last_check:
            time_since_last = (now - last_check).total_seconds()
            if time_since_last > PHONE_CHECK_WINDOW:
                count = 0
                checked_phones = []
                logger.info(f"SPAM: User {user_ip} reset after {PHONE_CHECK_WINDOW}s window")
        
        # ➕ 4. Увеличиваем счётчик
        count += 1
        checked_phones.append(phone)
        if len(checked_phones) > 20:
            checked_phones = checked_phones[-20:]
        
        attempts_left = max(0, PHONE_CHECK_LIMIT - count)
        
        logger.info(f"SPAM: User {user_ip}, count={count}, left={attempts_left}, phone={phone}")
        
        # 🚫 5. Превышен лимит — блокируем
        if count > PHONE_CHECK_LIMIT:
            blocked_until = now + timedelta(seconds=PHONE_BLOCK_TIME)
            supabase.table('phone_check_attempts').upsert({
                'user_id': user_ip,
                'attempt_count': count,
                'last_check': now.isoformat(),
                'blocked_until': blocked_until.isoformat(),
                'checked_phones': checked_phones
            }, on_conflict='user_id').execute()
            logger.warning(f"SPAM: User {user_ip} NOW BLOCKED for {PHONE_BLOCK_TIME}s")
            return True, PHONE_BLOCK_TIME, 0
        
        # 💾 6. Обновляем запись (атомарно через upsert)
        supabase.table('phone_check_attempts').upsert({
            'user_id': user_ip,
            'attempt_count': count,
            'last_check': now.isoformat(),
            'blocked_until': None,  # явный сброс
            'checked_phones': checked_phones
        }, on_conflict='user_id').execute()
        
        return False, 0, attempts_left
        
    except Exception as e:
        logger.error(f"SPAM: check_phone_spam error for {user_ip}: {e}", exc_info=True)
        # Fail-open: при ошибке разрешаем проверку, но с минимальными попытками
        return False, 0, 1


def reset_phone_spam(user_ip: str):
    """Сбрасывает счётчик после успешной регистрации"""
    try:
        now = datetime.now(timezone.utc)
        supabase.table('phone_check_attempts').upsert({
            'user_id': user_ip,
            'attempt_count': 0,
            'blocked_until': None,
            'last_check': now.isoformat(),
            'checked_phones': []
        }, on_conflict='user_id').execute()
        logger.info(f"SPAM: User {user_ip} counter reset after registration")
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
    user_ip = request.remote_addr
    
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
                'message': f'❌ Номер не найден в базе разрешенных номеров.\nОсталось попыток: {attempts_left}'
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
            'message': f'✅ Номер подтверждён. Осталось попыток: {attempts_left}'
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
    user_ip = request.remote_addr
    
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
            'message': f'✅ SIM-карта успешно {prefix}зарегистрирована!\n📱 {phone}\n🏢 {organization}\n👤 {full_name}'
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
