import time
import requests
import json
import threading
import hmac
import hashlib
from datetime import datetime, timedelta

# ===== НАСТРОЙКИ TELEGRAM =====
TELEGRAM_TOKEN = "8142613258:AAEuvhv7LgvFbsXgsKZzzYrjxJWrpPsi8YQ"
CHAT_ID = 1923391645

# ===== НАСТРОЙКИ BYBIT API =====
API_KEY = "oR731vMuhxOXue7j85"
API_SECRET = "w1hacAC8XJoyQzhSGPLPRLaXX47tYiaqTBpT"
RECV_WINDOW = "5000"

# ===== НАСТРОЙКИ ПРОКСИ =====
# VPN глобальный — прокси не нужен
USE_PROXY = False
PROXY = {
    "http": "socks5://127.0.0.1:1080",
    "https": "socks5://127.0.0.1:1080"
}

# ===== ТАЙМАУТЫ =====
TIMEOUT_BYBIT = 20      # увеличено с 10
TIMEOUT_TELEGRAM = 30   # увеличено с 15

# ===== НАСТРОЙКИ ПАР =====
SYMBOLS_CONFIG = {
    "BTCUSDT": {"funding_threshold": 0.02, "basis_threshold": 0.08, "max_basis": 0.5, "min_funding": -0.01},
    "ETHUSDT": {"funding_threshold": 0.03, "basis_threshold": 0.10, "max_basis": 0.5, "min_funding": -0.01},
    "SOLUSDT": {"funding_threshold": 0.04, "basis_threshold": 0.15, "max_basis": 0.6, "min_funding": -0.02},
}

# ===== ХРАНИЛИЩА =====
active_positions = {}
signal_history = []
deals_history = []
last_alerts = {}
quiet_hours_enabled = True
QUIET_HOURS_START = 23
QUIET_HOURS_END = 7
DATA_FILE = "data.json"


# ===== СЕССИЯ С РЕТРАЯМИ =====
def make_session():
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

SESSION = make_session()


# ===== ФУНКЦИИ BYBIT API =====
def bybit_request(params=None):
    """Подписанный запрос к Bybit"""
    timestamp = str(int(time.time() * 1000))
    if params is None:
        params = {}
    params["category"] = "linear"

    query_string = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    signature = hmac.new(
        API_SECRET.encode(),
        (timestamp + API_KEY + RECV_WINDOW + query_string).encode(),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "Content-Type": "application/json"
    }

    url = "https://api.bybit.com/v5/market/tickers"
    proxies = PROXY if USE_PROXY else None
    response = SESSION.get(url, params=params, headers=headers, timeout=TIMEOUT_BYBIT, proxies=proxies)
    return response.json()


# ===== ОТПРАВКА В TELEGRAM =====
def send_telegram(text, parse_mode="HTML", disable_notification=False, reply_markup=None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification
        }
        if reply_markup:
            data["reply_markup"] = reply_markup

        proxies = PROXY if USE_PROXY else None
        response = SESSION.post(url, json=data, timeout=TIMEOUT_TELEGRAM, proxies=proxies)

        if response.status_code == 200:
            print("✅ Отправлено в Telegram")
            return True
        else:
            print(f"❌ Ошибка {response.status_code}: {response.text[:100]}")
            return False
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return False


def get_updates(offset):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"offset": offset, "timeout": 10}
        proxies = PROXY if USE_PROXY else None
        response = SESSION.get(url, params=params, timeout=TIMEOUT_TELEGRAM, proxies=proxies)
        return response.json()
    except Exception as e:
        print(f"Ошибка get_updates: {e}")
        return {"ok": False}


# ===== ФУНКЦИИ ДАННЫХ =====
def get_funding(symbol):
    try:
        url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
        response = SESSION.get(url, timeout=TIMEOUT_BYBIT)
        data = response.json()
        if data.get("retCode") == 0:
            return float(data["result"]["list"][0]["fundingRate"]) * 100
    except Exception as e:
        print(f"❌ Ошибка фандинга {symbol}: {e}")
    return None


def get_basis(symbol):
    try:
        future_url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
        future_data = SESSION.get(future_url, timeout=TIMEOUT_BYBIT).json()
        if future_data.get("retCode") != 0:
            return None
        future_price = float(future_data["result"]["list"][0]["lastPrice"])

        spot_url = f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}"
        spot_data = SESSION.get(spot_url, timeout=TIMEOUT_BYBIT).json()
        if spot_data.get("retCode") != 0:
            return None
        spot_price = float(spot_data["result"]["list"][0]["lastPrice"])

        return ((future_price - spot_price) / spot_price) * 100
    except Exception as e:
        print(f"❌ Ошибка базиса {symbol}: {e}")
    return None


def get_price(symbol):
    try:
        proxies = PROXY if USE_PROXY else None
        url = f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}"
        response = SESSION.get(url, timeout=TIMEOUT_BYBIT, proxies=proxies)
        data = response.json()
        if data.get("retCode") == 0:
            return float(data["result"]["list"][0]["lastPrice"])
    except Exception as e:
        print(f"❌ Ошибка цены {symbol}: {e}")
    return None


# ===== РИСК-МЕНЕДЖМЕНТ =====
def get_next_funding():
    now = datetime.now()
    for h in [0, 8, 16]:
        ft = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if ft > now:
            return ft.strftime('%H:%M UTC')
    return "00:00 UTC"


def risk_check_entry(symbol):
    funding = get_funding(symbol)
    basis = get_basis(symbol)
    if funding is None or basis is None:
        return False, "❌ Ошибка получения данных"
    cfg = SYMBOLS_CONFIG[symbol]
    funding_status = "🟢" if funding >= cfg["funding_threshold"] else "🟡" if funding >= 0 else "🔴"
    basis_status = "🟢" if basis >= cfg["basis_threshold"] else "🟡" if basis >= 0 else "🔴"
    msg = f"Фандинг: {funding:.3f}% {funding_status}\nБазис: {basis:.2f}% {basis_status}"
    warnings = []
    if funding < cfg["funding_threshold"]:
        warnings.append(f"⚠️ Фандинг ниже порога ({cfg['funding_threshold']}%)")
    if basis < cfg["basis_threshold"]:
        warnings.append(f"⚠️ Базис ниже порога ({cfg['basis_threshold']}%)")
    if basis > cfg["max_basis"]:
        warnings.append(f"🔴 Базис {basis:.2f}% слишком высокий!")
    if funding < 0:
        warnings.append(f"🔴 Фандинг отрицательный!")
    now = datetime.now()
    for h in [0, 8, 16]:
        ft = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if ft > now:
            minutes = (ft - now).total_seconds() / 60
            if minutes < 30:
                warnings.append(f"⏰ До выплаты {minutes:.0f} мин (лучше подождать)")
            break
    if warnings:
        return False, msg + "\n\n" + "\n".join(warnings)
    return True, msg + "\n\n✅ МОЖНО ВХОДИТЬ"


def risk_check_position(symbol):
    funding = get_funding(symbol)
    basis = get_basis(symbol)
    if funding is None or basis is None:
        return "warning", "❌ Ошибка данных"
    cfg = SYMBOLS_CONFIG[symbol]
    if funding < cfg["min_funding"] or basis < 0:
        return "critical", f"🚨 Фандинг {funding:.3f}% | Базис {basis:.2f}%"
    if funding < cfg["funding_threshold"] or basis < cfg["basis_threshold"]:
        return "warning", f"⚠️ Фандинг {funding:.3f}% | Базис {basis:.2f}%"
    return "ok", f"✅ Фандинг {funding:.3f}% | Базис {basis:.2f}%"


def calculate_profit(symbol):
    if symbol not in active_positions:
        return None, "Нет позиции"
    entry_data = active_positions[symbol]
    entry_price = entry_data.get("entry_price")
    if not entry_price:
        return None, "Нет цены входа"
    current_price = get_price(symbol)
    if not current_price:
        return None, "Ошибка получения цены"
    profit = ((entry_price - current_price) / entry_price) * 100
    return profit, current_price


# ===== ХРАНЕНИЕ ДАННЫХ =====
def save_data():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump({
                "active_positions": active_positions,
                "signal_history": signal_history[-50:],
                "deals_history": deals_history[-50:],
                "last_alerts": last_alerts
            }, f)
    except:
        pass


def load_data():
    global active_positions, signal_history, deals_history, last_alerts
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
            active_positions = data.get("active_positions", {})
            signal_history = data.get("signal_history", [])
            deals_history = data.get("deals_history", [])
            last_alerts = data.get("last_alerts", {})
            print("✅ Данные загружены")
    except:
        print("📭 Новые данные")


# ===== ИНТЕРФЕЙС =====
def get_status_text():
    lines = []
    lines.append("╔══════════════════════════════════╗")
    lines.append("║   🤖 АРБИТРАЖНЫЙ ПОМОЩНИК       ║")
    lines.append("╚══════════════════════════════════╝")
    lines.append(f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    lines.append("📊 Bybit API")
    lines.append("")
    if active_positions:
        lines.append("🔴 АКТИВНЫЕ ПОЗИЦИИ:")
        for symbol, data in active_positions.items():
            level, msg = risk_check_position(symbol)
            emoji = "🚨" if level == "critical" else "⚠️" if level == "warning" else "✅"
            profit, _ = calculate_profit(symbol)
            profit_str = f" | {profit:.2f}%" if profit else ""
            lines.append(f"  {emoji} {symbol}{profit_str}")
            lines.append(f"     {msg}")
    else:
        lines.append("🟢 НЕТ АКТИВНЫХ ПОЗИЦИЙ")
    lines.append("")
    lines.append("📊 СИГНАЛЫ ДЛЯ ВХОДА:")
    for symbol in SYMBOLS_CONFIG:
        if symbol in active_positions:
            continue
        funding = get_funding(symbol)
        basis = get_basis(symbol)
        if funding and basis:
            ok, msg = risk_check_entry(symbol)
            if ok:
                lines.append(f"  ✅ {symbol}: {funding:.3f}% / {basis:.2f}% → ВХОД")
            else:
                lines.append(f"  ⚪ {symbol}: {funding:.3f}% / {basis:.2f}%")
                for line in msg.split('\n')[:1]:
                    if line:
                        lines.append(f"     {line}")
        else:
            lines.append(f"  ❌ {symbol}: ошибка")
    lines.append("")
    lines.append(f"⏰ Следующая выплата: {get_next_funding()}")
    lines.append(f"📊 Всего сделок: {len(deals_history)}")
    if deals_history:
        total = sum(d.get('profit_percent', 0) for d in deals_history)
        avg = total / len(deals_history) if deals_history else 0
        lines.append(f"💰 Средняя прибыль: {avg:.2f}%")
    if quiet_hours_enabled:
        lines.append(f"🔇 Тихий час: {QUIET_HOURS_START}:00–{QUIET_HOURS_END}:00")
    return "\n".join(lines)


def get_main_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "📊 СТАТУС", "callback_data": "status"},
                {"text": "📖 ГАЙД", "callback_data": "guide"}
            ],
            [
                {"text": "✅ В ПОЗИЦИИ", "callback_data": "enter"},
                {"text": "🚨 ВЫЙТИ", "callback_data": "exit"}
            ],
            [
                {"text": "📋 ИСТОРИЯ", "callback_data": "history"},
                {"text": "💰 СДЕЛКИ", "callback_data": "deals"}
            ],
            [
                {"text": "🔇 ТИХИЙ ЧАС", "callback_data": "quiet"}
            ]
        ]
    }


def get_pair_selection(action):
    buttons = []
    for symbol in SYMBOLS_CONFIG:
        if action == "enter" and symbol in active_positions:
            continue
        if action == "exit" and symbol not in active_positions:
            continue
        buttons.append([{"text": symbol, "callback_data": f"{action}_{symbol}"}])
    buttons.append([{"text": "🔙 НАЗАД", "callback_data": "cancel"}])
    return {"inline_keyboard": buttons}


def get_guide_text():
    return """
📖 ГАЙД ПО АРБИТРАЖУ (BYBIT)

<b>1. Фандинг</b> — плата за удержание позиции.
Если фандинг положительный → вы получаете деньги.

<b>2. Базис</b> — разница между фьючерсом и спотом.
Если базис положительный → фьючерс дороже спота.

<b>3. Условия для входа:</b>
• BTC: фандинг > 0.02%, базис > 0.08%
• ETH: фандинг > 0.03%, базис > 0.10%
• SOL: фандинг > 0.04%, базис > 0.15%

<b>4. Алгоритм:</b>
1️⃣ Ждёте сигнал в Telegram
2️⃣ Открываете сделку на Bybit
3️⃣ Нажимаете "✅ В ПОЗИЦИИ"
4️⃣ Следите за статусом
5️⃣ При сигнале выхода нажимаете "🚨 ВЫЙТИ"
    """


def open_position_with_symbol(symbol):
    if symbol not in SYMBOLS_CONFIG:
        return f"❌ Пара {symbol} не поддерживается"
    if symbol in active_positions:
        return f"⚠️ Позиция по {symbol} уже активна"
    ok, msg = risk_check_entry(symbol)
    if not ok:
        return f"❌ Риски:\n{msg}"
    price = get_price(symbol)
    if not price:
        return "❌ Ошибка получения цены"
    active_positions[symbol] = {
        "entry_time": datetime.now().isoformat(),
        "entry_price": price,
        "entry_funding": get_funding(symbol),
        "entry_basis": get_basis(symbol)
    }
    save_data()
    return f"""
✅ Позиция по {symbol} открыта!
💰 Цена входа: ${price:.0f}
{msg}
📌 Нажмите "🚨 ВЫЙТИ" для закрытия.
    """


def close_position_with_symbol(symbol):
    if symbol not in active_positions:
        return f"❌ Нет активной позиции по {symbol}"
    profit, current_price = calculate_profit(symbol)
    if profit is None:
        return f"❌ Ошибка расчёта"
    data = active_positions.pop(symbol)
    deals_history.append({
        "symbol": symbol,
        "entry_time": data["entry_time"],
        "exit_time": datetime.now().isoformat(),
        "profit_percent": round(profit, 2),
        "entry_price": data.get("entry_price"),
        "exit_price": current_price
    })
    save_data()
    emoji = "✅" if profit > 0 else "⚠️"
    return f"""
{emoji} Позиция по {symbol} закрыта!
💰 Прибыль: {profit:.2f}%
📈 Вход: ${data.get('entry_price', 0):.0f}
📉 Выход: ${current_price:.0f}
    """


def handle_callback(data):
    if data == "status":
        send_telegram(get_status_text(), reply_markup=get_main_keyboard())
    elif data == "guide":
        send_telegram(get_guide_text(), reply_markup=get_main_keyboard())
    elif data == "enter":
        send_telegram("📌 Выберите пару для входа:", reply_markup=get_pair_selection("enter"))
    elif data == "exit":
        if active_positions:
            send_telegram("📌 Выберите пару для выхода:", reply_markup=get_pair_selection("exit"))
        else:
            send_telegram("🟢 Нет активных позиций", reply_markup=get_main_keyboard())
    elif data.startswith("enter_"):
        symbol = data.split("_")[1]
        send_telegram(open_position_with_symbol(symbol), reply_markup=get_main_keyboard())
    elif data.startswith("exit_"):
        symbol = data.split("_")[1]
        send_telegram(close_position_with_symbol(symbol), reply_markup=get_main_keyboard())
    elif data == "history":
        if not signal_history:
            send_telegram("📭 Сигналов пока нет", reply_markup=get_main_keyboard())
        else:
            lines = ["📡 ПОСЛЕДНИЕ СИГНАЛЫ", ""]
            for s in signal_history[-10:]:
                lines.append(
                    f"• {s.get('time', '')[:16]} {s.get('symbol', '')}: {s.get('funding', 0):.3f}% / {s.get('basis', 0):.2f}%")
            send_telegram("\n".join(lines), reply_markup=get_main_keyboard())
    elif data == "deals":
        if not deals_history:
            send_telegram("📭 Сделок пока нет", reply_markup=get_main_keyboard())
        else:
            lines = ["📋 ИСТОРИЯ СДЕЛОК", ""]
            total = 0
            for d in deals_history[-10:]:
                p = d.get('profit_percent', 0)
                emoji = "✅" if p > 0 else "⚠️"
                lines.append(f"  {emoji} {d.get('symbol', '')}: {p:.2f}%")
                total += p
            avg = total / len(deals_history) if deals_history else 0
            lines.append(f"\n💰 Средняя прибыль: {avg:.2f}%")
            send_telegram("\n".join(lines), reply_markup=get_main_keyboard())
    elif data == "quiet":
        global quiet_hours_enabled
        quiet_hours_enabled = not quiet_hours_enabled
        send_telegram(f"🔇 Тихий час {'включен' if quiet_hours_enabled else 'выключен'}",
                      reply_markup=get_main_keyboard())
    elif data == "cancel":
        send_telegram(get_status_text(), reply_markup=get_main_keyboard())


# ===== МОНИТОРИНГ =====
def monitor():
    while True:
        try:
            for symbol in list(active_positions.keys()):
                level, msg = risk_check_position(symbol)
                if level == "critical" and last_alerts.get(symbol) != "critical":
                    send_telegram(f"""
🔴🔴🔴 КРИТИЧЕСКИЙ РИСК! 🔴🔴🔴

{symbol}
{msg}

👉 Нажмите "🚨 ВЫЙТИ" в меню
                    """, disable_notification=False)
                    last_alerts[symbol] = "critical"
                elif level == "warning" and last_alerts.get(symbol) != "warning":
                    send_telegram(f"⚠️ ВНИМАНИЕ!\n{symbol}\n{msg}", disable_notification=False)
                    last_alerts[symbol] = "warning"
                elif level == "ok" and symbol in last_alerts:
                    del last_alerts[symbol]
            for symbol in SYMBOLS_CONFIG:
                if symbol in active_positions:
                    continue
                funding = get_funding(symbol)
                basis = get_basis(symbol)
                if funding and basis:
                    ok, msg = risk_check_entry(symbol)
                    if ok:
                        send_telegram(f"""
🚨 СИГНАЛ НА ВХОД {symbol}

{msg}

1. Откройте Bybit
2. Купите спот {symbol}
3. Откройте шорт фьючерса {symbol} (плечо 2x)
4. Нажмите "✅ В ПОЗИЦИИ"
                        """, reply_markup=get_main_keyboard())
                        signal_history.append({
                            "time": datetime.now().isoformat(),
                            "symbol": symbol,
                            "funding": funding,
                            "basis": basis
                        })
                        save_data()
                        time.sleep(60)
        except Exception as e:
            print(f"❌ Ошибка мониторинга: {e}")
        time.sleep(300)


def process_callbacks():
    offset = 0
    while True:
        try:
            data = get_updates(offset)
            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    if "callback_query" in update:
                        callback = update["callback_query"]
                        callback_data = callback.get("data")
                        if callback_data:
                            try:
                                answer_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
                                proxies = PROXY if USE_PROXY else None
                                SESSION.post(answer_url, json={"callback_query_id": callback["id"]},
                                             proxies=proxies, timeout=TIMEOUT_TELEGRAM)
                            except:
                                pass
                            handle_callback(callback_data)
        except Exception as e:
            print(f"❌ Ошибка callbacks: {e}")
        time.sleep(1)


def reminder():
    while True:
        try:
            if active_positions:
                for symbol in active_positions:
                    level, msg = risk_check_position(symbol)
                    if level != "ok":
                        send_telegram(f"🔔 НАПОМИНАНИЕ\n{symbol}\n{msg}", disable_notification=False)
        except Exception as e:
            print(f"❌ Ошибка reminder: {e}")
        time.sleep(3600)


# ===== ЗАПУСК =====
if __name__ == "__main__":
    load_data()
    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=process_callbacks, daemon=True).start()
    threading.Thread(target=reminder, daemon=True).start()
    send_telegram("🚀 БОТ ЗАПУЩЕН! (Bybit API)", reply_markup=get_main_keyboard())
    print("🤖 Бот запущен!")
    print(f"📊 Отслеживаем: {', '.join(SYMBOLS_CONFIG.keys())}")
    print("🔑 Bybit API подключён")
    print("⌨️ Нажмите Ctrl+C для остановки")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n❌ Бот остановлен")
        send_telegram("🛑 Бот остановлен")
