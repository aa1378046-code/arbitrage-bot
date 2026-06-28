import time
import requests
import json
import threading
import hmac
import hashlib
from datetime import datetime

# ===== НАСТРОЙКИ TELEGRAM =====
TELEGRAM_TOKEN = "8142613258:AAEuvhv7LgvFbsXgsKZzzYrjxJWrpPsi8YQ"
CHAT_ID = 1923391645

# ===== НАСТРОЙКИ BYBIT API =====
API_KEY = "oR731vMuhxOXue7j85"
API_SECRET = "w1hacAC8XJoyQzhSGPLPRLaXX47tYiaqTBpT"
RECV_WINDOW = "5000"

# ===== ТАЙМАУТЫ =====
TIMEOUT = 20

# ===== ПАРАМЕТРЫ СТРАТЕГИИ =====
MIN_FUNDING = 0.01           # минимальный фандинг %
MIN_FUNDING_PERIODS = 1      # минимум периодов подряд с положительным фандингом
MIN_VOLUME_24H = 5_000_000  # минимальный оборот $50M
MAX_SPREAD = 1.0             # максимальный спред фьючерс/спот %
MIN_PROFIT_AFTER_FEES = 0.001 # минимальная чистая прибыль после комиссий %
TAKER_FEE = 0.055            # комиссия тейкера Bybit (%)
MIN_MINUTES_TO_FUNDING = 5  # минимум минут до выплаты
MAX_PRICE_CHANGE_1H = 10.0    # максимальное движение цены за час %
MONITOR_INTERVAL = 300        # интервал мониторинга (сек)
MIN_RISK_SCORE = 3            # минимальная оценка риска для сигнала

# ===== ХРАНИЛИЩА =====
active_positions = {}
signal_history = []
deals_history = []
last_alerts = {}
alerted_symbols = set()
DATA_FILE = "data.json"


# ===== СЕССИЯ =====
def make_session():
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=2)
    session.mount("https://", adapter)
    return session

SESSION = make_session()


# ===== BYBIT API =====
def bybit_get(endpoint, params=None):
    try:
        url = f"https://api.bytick.com/v5/{endpoint}"
        r = SESSION.get(url, params=params, timeout=TIMEOUT)
        data = r.json()
        if data.get("retCode") == 0:
            return data["result"]
        return None
    except Exception as e:
        print(f"API ошибка {endpoint}: {e}")
        return None


# ===== ПОЛУЧЕНИЕ ДАННЫХ =====
def get_all_linear_tickers():
    result = bybit_get("market/tickers", {"category": "linear"})
    if result:
        return result.get("list", [])
    return []


def get_spot_price(symbol):
    result = bybit_get("market/tickers", {"category": "spot", "symbol": symbol})
    if result and result.get("list"):
        return float(result["list"][0]["lastPrice"])
    return None


def get_funding_history(symbol, limit=5):
    result = bybit_get("market/funding/history", {
        "category": "linear",
        "symbol": symbol,
        "limit": limit
    })
    if result and result.get("list"):
        return [float(x["fundingRate"]) * 100 for x in result["list"]]
    return []


def get_orderbook_spread(symbol):
    result = bybit_get("market/orderbook", {"category": "linear", "symbol": symbol, "limit": 1})
    if result:
        ask = float(result["a"][0][0]) if result.get("a") else None
        bid = float(result["b"][0][0]) if result.get("b") else None
        if ask and bid:
            mid = (ask + bid) / 2
            spread = (ask - bid) / mid * 100
            return spread, ask, bid
    return None, None, None


def get_kline_change(symbol):
    result = bybit_get("market/kline", {
        "category": "linear",
        "symbol": symbol,
        "interval": "60",
        "limit": 2
    })
    if result and result.get("list") and len(result["list"]) >= 2:
        open_price = float(result["list"][1][1])
        close_price = float(result["list"][0][4])
        return abs((close_price - open_price) / open_price * 100)
    return None


def get_minutes_to_funding():
    now = datetime.utcnow()
    for h in [0, 8, 16]:
        ft = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if ft > now:
            return (ft - now).total_seconds() / 60
    from datetime import timedelta
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds() / 60


def get_next_funding_time():
    now = datetime.utcnow()
    for h in [0, 8, 16]:
        ft = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if ft > now:
            minutes = (ft - now).total_seconds() / 60
            hours = int(minutes // 60)
            mins = int(minutes % 60)
            return f"{hours}ч {mins}мин"
    return "—"


# ===== АНАЛИЗ =====
def analyze_symbol(ticker):
    symbol = ticker.get("symbol", "")
    if not symbol.endswith("USDT"):
        return None
    try:
        funding = float(ticker.get("fundingRate", 0)) * 100
        future_price = float(ticker.get("lastPrice", 0))
        volume_24h = float(ticker.get("turnover24h", 0))
    except:
        return None

    if funding < MIN_FUNDING:
        return None
    if volume_24h < MIN_VOLUME_24H:
        return None

    minutes_to_funding = get_minutes_to_funding()
    if minutes_to_funding < MIN_MINUTES_TO_FUNDING:
        return None

    spot_price = get_spot_price(symbol)
    if not spot_price:
        return None
    basis = (future_price - spot_price) / spot_price * 100
    if basis > MAX_SPREAD or basis < -0.1:
        return None

    history = get_funding_history(symbol, 5)
    if not history:
        return None
    positive_periods = sum(1 for x in history if x > 0)
    if positive_periods < MIN_FUNDING_PERIODS:
        return None
    avg_funding = sum(history) / len(history)

    price_change_1h = get_kline_change(symbol)
    if price_change_1h and price_change_1h > MAX_PRICE_CHANGE_1H:
        return None

    ob_spread, ask, bid = get_orderbook_spread(symbol)
    if ob_spread is None:
        ob_spread = 0.02

    entry_cost = ob_spread + TAKER_FEE * 2
    exit_cost = ob_spread + TAKER_FEE * 2
    total_fees = entry_cost + exit_cost
    net_profit = funding - total_fees / 2

    if net_profit < MIN_PROFIT_AFTER_FEES:
        return None

    score = 10
    if funding < 0.15: score -= 1
    if positive_periods < 4: score -= 1
    if volume_24h < 100_000_000: score -= 1
    if ob_spread > 0.05: score -= 1
    if price_change_1h and price_change_1h > 1.5: score -= 1
    if basis > 0.15: score -= 1
    if avg_funding < funding * 0.7: score -= 1

    if score < MIN_RISK_SCORE:
        return None

    predicted_funding = round(avg_funding * 0.9, 4)

    return {
        "symbol": symbol,
        "funding": funding,
        "predicted_funding": predicted_funding,
        "avg_funding": avg_funding,
        "positive_periods": positive_periods,
        "volume_24h": volume_24h,
        "basis": basis,
        "ob_spread": ob_spread,
        "entry_cost": round(entry_cost, 4),
        "exit_cost": round(exit_cost, 4),
        "total_fees": round(total_fees, 4),
        "net_profit": round(net_profit, 4),
        "risk_score": score,
        "minutes_to_funding": minutes_to_funding,
        "spot_price": spot_price,
        "future_price": future_price,
    }


def format_signal(d):
    score = d["risk_score"]
    score_emoji = "🟢" if score >= 8 else "🟡" if score >= 6 else "🔴"
    vol_m = d["volume_24h"] / 1_000_000
    minutes = d["minutes_to_funding"]
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    time_str = f"{hours}ч {mins}мин" if hours > 0 else f"{mins}мин"

    return f"""
🚨 <b>СИГНАЛ: {d['symbol']}</b>
━━━━━━━━━━━━━━━━━━━━
💰 <b>Фандинг:</b> {d['funding']:.4f}% (стабильно {d['positive_periods']} периодов)
📈 <b>Прогноз след. ставки:</b> {d['predicted_funding']:.4f}%
⏰ <b>До выплаты:</b> {time_str}
{score_emoji} <b>Оценка риска:</b> {score}/10

💹 <b>ФИНАНСЫ:</b>
• Спред стакана: {d['ob_spread']:.3f}%
• Базис (фьюч-спот): {d['basis']:.3f}%
• Комиссия входа: {d['entry_cost']:.3f}%
• Комиссия выхода: {d['exit_cost']:.3f}%
• Итого затраты: {d['total_fees']:.3f}%
• <b>Чистая прибыль за выплату: ~{d['net_profit']:.3f}%</b>
• Объём 24ч: ${vol_m:.1f}M

📋 <b>ЧТО ДЕЛАТЬ:</b>
1️⃣ Открой Bybit → раздел <b>Торговля</b>
2️⃣ Купи <b>{d['symbol'].replace('USDT','')}</b> на споте по ~${d['spot_price']:.4f}
3️⃣ Открой <b>шорт фьючерса</b> {d['symbol']} с плечом <b>1x</b>
4️⃣ Нажми кнопку <b>✅ В ПОЗИЦИИ</b> ниже

🚨 <b>ВЫЙДИ ЕСЛИ:</b>
• Фандинг упадёт ниже {MIN_FUNDING/2:.3f}%
• Базис уйдёт в минус
• Собрал 3+ выплаты подряд
━━━━━━━━━━━━━━━━━━━━
    """.strip()


# ===== TELEGRAM =====
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
        r = SESSION.post(url, json=data, timeout=30)
        if r.status_code == 200:
            print("✅ Telegram")
            return True
        print(f"❌ Telegram {r.status_code}: {r.text[:100]}")
        return False
    except Exception as e:
        print(f"❌ Telegram: {e}")
        return False


def get_updates(offset):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        r = SESSION.get(url, params={"offset": offset, "timeout": 10}, timeout=30)
        return r.json()
    except Exception as e:
        print(f"Ошибка get_updates: {e}")
        return {"ok": False}


# ===== КЛАВИАТУРЫ =====
def main_keyboard():
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
                {"text": "🔍 СКАНИРОВАТЬ", "callback_data": "scan"}
            ]
        ]
    }


def pair_keyboard(action):
    buttons = []
    if action == "exit":
        for symbol in active_positions:
            buttons.append([{"text": symbol, "callback_data": f"exit_{symbol}"}])
    if action == "enter":
        for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            if s not in active_positions:
                buttons.append([{"text": s, "callback_data": f"enter_{s}"}])
    buttons.append([{"text": "🔙 НАЗАД", "callback_data": "cancel"}])
    return {"inline_keyboard": buttons}


# ===== ДАННЫЕ =====
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


def get_current_price(symbol):
    result = bybit_get("market/tickers", {"category": "spot", "symbol": symbol})
    if result and result.get("list"):
        return float(result["list"][0]["lastPrice"])
    return None


def calculate_profit(symbol):
    if symbol not in active_positions:
        return None
    entry_price = active_positions[symbol].get("entry_price")
    if not entry_price:
        return None
    current = get_current_price(symbol)
    if not current:
        return None
    return ((entry_price - current) / entry_price) * 100


def check_position_health(symbol):
    result = bybit_get("market/tickers", {"category": "linear", "symbol": symbol})
    if not result or not result.get("list"):
        return "warning", None, None
    ticker = result["list"][0]
    funding = float(ticker.get("fundingRate", 0)) * 100
    future_price = float(ticker.get("lastPrice", 0))
    spot = get_current_price(symbol)
    basis = ((future_price - spot) / spot * 100) if spot else 0
    if funding < 0 or basis < -0.1:
        return "critical", funding, basis
    if funding < MIN_FUNDING / 2:
        return "warning", funding, basis
    return "ok", funding, basis


def open_position(symbol):
    if symbol in active_positions:
        return f"⚠️ Позиция по {symbol} уже открыта"
    price = get_current_price(symbol)
    if not price:
        return "❌ Не удалось получить цену"
    result = bybit_get("market/tickers", {"category": "linear", "symbol": symbol})
    funding = None
    if result and result.get("list"):
        funding = float(result["list"][0].get("fundingRate", 0)) * 100
    active_positions[symbol] = {
        "entry_time": datetime.now().isoformat(),
        "entry_price": price,
        "entry_funding": funding,
    }
    save_data()
    f_str = f"{funding:.4f}%" if funding else "—"
    return f"✅ Позиция <b>{symbol}</b> открыта!\n💰 Цена входа: ${price:.4f}\n💸 Фандинг при входе: {f_str}"


def close_position(symbol):
    if symbol not in active_positions:
        return f"❌ Нет позиции по {symbol}"
    profit = calculate_profit(symbol)
    current = get_current_price(symbol)
    data = active_positions.pop(symbol)
    deals_history.append({
        "symbol": symbol,
        "entry_time": data["entry_time"],
        "exit_time": datetime.now().isoformat(),
        "profit_percent": round(profit, 3) if profit else 0,
        "entry_price": data.get("entry_price"),
        "exit_price": current
    })
    save_data()
    emoji = "✅" if (profit or 0) > 0 else "⚠️"
    profit_str = f"{profit:.3f}%" if profit is not None else "—"
    current_str = f"${current:.4f}" if current else "—"
    return f"{emoji} Позиция <b>{symbol}</b> закрыта!\n💰 Прибыль: {profit_str}\n📈 Вход: ${data.get('entry_price', 0):.4f}\n📉 Выход: {current_str}"


# ===== СТАТУС =====
def get_status_text():
    lines = []
    lines.append("╔══════════════════════════════════╗")
    lines.append("║   🤖 АРБИТРАЖНЫЙ ПОМОЩНИК v2    ║")
    lines.append("╚══════════════════════════════════╝")
    lines.append(f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    lines.append(f"⏰ До выплаты: {get_next_funding_time()}")
    lines.append("")
    if active_positions:
        lines.append("🔴 АКТИВНЫЕ ПОЗИЦИИ:")
        for symbol in active_positions:
            level, funding, basis = check_position_health(symbol)
            profit = calculate_profit(symbol)
            emoji = "🚨" if level == "critical" else "⚠️" if level == "warning" else "✅"
            profit_str = f"{profit:.3f}%" if profit is not None else "—"
            funding_str = f"{funding:.4f}%" if funding is not None else "—"
            lines.append(f"  {emoji} {symbol}")
            lines.append(f"     Фандинг: {funding_str} | Прибыль: {profit_str}")
    else:
        lines.append("🟢 НЕТ АКТИВНЫХ ПОЗИЦИЙ")
    lines.append("")
    lines.append(f"📊 Всего сделок: {len(deals_history)}")
    if deals_history:
        total = sum(d.get("profit_percent", 0) for d in deals_history)
        avg = total / len(deals_history)
        lines.append(f"💰 Средняя прибыль: {avg:.3f}%")
    lines.append("")
    lines.append("🔍 Нажми СКАНИРОВАТЬ для поиска сигналов")
    return "\n".join(lines)


def get_guide_text():
    return """
📖 <b>ГАЙД: FUNDING RATE АРБИТРАЖ</b>

<b>Суть стратегии:</b>
Покупаешь монету на споте + открываешь шорт фьючерса того же размера. Позиции нейтральны к цене — зарабатываешь только на фандинге.

<b>Когда бот даёт сигнал:</b>
✅ Фандинг выше порога несколько периодов подряд
✅ Объём > $50M (хорошая ликвидность)
✅ Чистая прибыль покрывает все комиссии
✅ Оценка риска 6+/10

<b>Алгоритм входа:</b>
1️⃣ Получил сигнал в Telegram
2️⃣ Открыл Bybit → Торговля
3️⃣ Купил монету на споте
4️⃣ Открыл шорт фьючерса (плечо 1x!)
5️⃣ Нажал ✅ В ПОЗИЦИИ

<b>Когда выходить:</b>
🚨 Фандинг упал ниже 0.04%
🚨 Базис ушёл в минус
🚨 Собрал 3+ выплаты подряд
🚨 Бот прислал алерт

<b>Важно:</b>
• Плечо всегда 1x — без риска ликвидации
• Размер спота = размеру шорта точно
• Не жадничать — вышел с прибылью = хорошо
    """.strip()


# ===== СКАНИРОВАНИЕ =====
def run_scan():
    global alerted_symbols
    tickers = get_all_linear_tickers()
    if not tickers:
        send_telegram("❌ Не удалось получить данные с Bybit", reply_markup=main_keyboard())
        return

    signals = []
    passed_funding = 0
    passed_volume = 0
    passed_time = 0
    total = 0

    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        if symbol in active_positions:
            continue
        total += 1
        try:
            funding = float(ticker.get("fundingRate", 0)) * 100
            volume_24h = float(ticker.get("turnover24h", 0))
        except:
            continue
        if funding >= MIN_FUNDING:
            passed_funding += 1
        if volume_24h >= MIN_VOLUME_24H:
            passed_volume += 1
        if funding >= MIN_FUNDING and volume_24h >= MIN_VOLUME_24H:
            passed_time += 1
        result = analyze_symbol(ticker)
        if result:
            signals.append(result)
        time.sleep(0.15)

    debug_msg = (
        f"🔍 Сканирование завершено\n"
        f"Всего пар: {total}\n"
        f"Прошли фандинг (>{MIN_FUNDING}%): {passed_funding}\n"
        f"Прошли объём (>${MIN_VOLUME_24H/1e6:.0f}M): {passed_volume}\n"
        f"Прошли оба фильтра: {passed_time}\n"
        f"Итого сигналов: {len(signals)}"
    )
    send_telegram(debug_msg, disable_notification=True)

    if not signals:
        send_telegram(
            "😔 Хороших сигналов сейчас нет.\n\nУсловия рынка не подходят — попробуй позже или снизь требования.",
            reply_markup=main_keyboard()
        )
        return

    signals.sort(key=lambda x: (x["risk_score"], x["net_profit"]), reverse=True)
    top = signals[:3]

    send_telegram(f"✅ Найдено <b>{len(signals)}</b> сигналов. Показываю топ {len(top)}:", disable_notification=True)

    for d in top:
        keyboard = {
            "inline_keyboard": [
                [{"text": f"✅ В ПОЗИЦИИ {d['symbol']}", "callback_data": f"enter_{d['symbol']}"}],
                [{"text": "🔙 ГЛАВНОЕ МЕНЮ", "callback_data": "status"}]
            ]
        }
        send_telegram(format_signal(d), reply_markup=keyboard)
        signal_history.append({
            "time": datetime.now().isoformat(),
            "symbol": d["symbol"],
            "funding": d["funding"],
            "risk_score": d["risk_score"],
            "net_profit": d["net_profit"],
        })
        time.sleep(1)
    save_data()


# ===== CALLBACKS =====
def handle_callback(data):
    if data == "status":
        send_telegram(get_status_text(), reply_markup=main_keyboard())
    elif data == "guide":
        send_telegram(get_guide_text(), reply_markup=main_keyboard())
    elif data == "scan":
        send_telegram("🔍 Сканирую рынок... (30-60 сек)", disable_notification=True)
        threading.Thread(target=run_scan, daemon=True).start()
    elif data == "enter":
        send_telegram("📌 Выберите пару:", reply_markup=pair_keyboard("enter"))
    elif data == "exit":
        if active_positions:
            send_telegram("📌 Выберите пару для выхода:", reply_markup=pair_keyboard("exit"))
        else:
            send_telegram("🟢 Нет активных позиций", reply_markup=main_keyboard())
    elif data.startswith("enter_"):
        symbol = data.split("_", 1)[1]
        send_telegram(open_position(symbol), reply_markup=main_keyboard())
    elif data.startswith("exit_"):
        symbol = data.split("_", 1)[1]
        send_telegram(close_position(symbol), reply_markup=main_keyboard())
    elif data == "history":
        if not signal_history:
            send_telegram("📭 Сигналов пока нет", reply_markup=main_keyboard())
        else:
            lines = ["📡 <b>ПОСЛЕДНИЕ СИГНАЛЫ</b>", ""]
            for s in signal_history[-10:]:
                lines.append(f"• {s.get('time','')[:16]} <b>{s.get('symbol','')}</b>: {s.get('funding',0):.4f}% | риск {s.get('risk_score',0)}/10")
            send_telegram("\n".join(lines), reply_markup=main_keyboard())
    elif data == "deals":
        if not deals_history:
            send_telegram("📭 Сделок пока нет", reply_markup=main_keyboard())
        else:
            lines = ["📋 <b>ИСТОРИЯ СДЕЛОК</b>", ""]
            total = 0
            for d in deals_history[-10:]:
                p = d.get("profit_percent", 0)
                emoji = "✅" if p > 0 else "⚠️"
                lines.append(f"  {emoji} {d.get('symbol','')}: {p:.3f}%")
                total += p
            avg = total / len(deals_history)
            lines.append(f"\n💰 Средняя прибыль: {avg:.3f}%")
            lines.append(f"📊 Всего сделок: {len(deals_history)}")
            send_telegram("\n".join(lines), reply_markup=main_keyboard())
    elif data == "cancel":
        send_telegram(get_status_text(), reply_markup=main_keyboard())


# ===== МОНИТОРИНГ =====
def monitor():
    while True:
        try:
            for symbol in list(active_positions.keys()):
                level, funding, basis = check_position_health(symbol)
                profit = calculate_profit(symbol)
                profit_str = f"{profit:.3f}%" if profit is not None else "—"
                funding_val = funding or 0
                basis_val = basis or 0
                if level == "critical" and last_alerts.get(symbol) != "critical":
                    send_telegram(
                        f"🔴 <b>СРОЧНО ВЫЙТИ: {symbol}</b>\n\nФандинг: {funding_val:.4f}%\nБазис: {basis_val:.3f}%\nПрибыль: {profit_str}\n\n👉 Закрой позиции и нажми <b>🚨 ВЫЙТИ</b>",
                        reply_markup=main_keyboard()
                    )
                    last_alerts[symbol] = "critical"
                elif level == "warning" and last_alerts.get(symbol) not in ("warning", "critical"):
                    send_telegram(
                        f"⚠️ <b>ВНИМАНИЕ: {symbol}</b>\n\nФандинг: {funding_val:.4f}% (снижается)\nБазис: {basis_val:.3f}%\nПрибыль: {profit_str}",
                        reply_markup=main_keyboard()
                    )
                    last_alerts[symbol] = "warning"
                elif level == "ok" and symbol in last_alerts:
                    del last_alerts[symbol]

            if not active_positions:
                tickers = get_all_linear_tickers()
                for ticker in tickers:
                    symbol = ticker.get("symbol", "")
                    if symbol in alerted_symbols:
                        continue
                    result = analyze_symbol(ticker)
                    if result and result["risk_score"] >= 8:
                        keyboard = {
                            "inline_keyboard": [
                                [{"text": f"✅ В ПОЗИЦИИ {symbol}", "callback_data": f"enter_{symbol}"}],
                                [{"text": "❌ Пропустить", "callback_data": "cancel"}]
                            ]
                        }
                        send_telegram(format_signal(result), reply_markup=keyboard)
                        alerted_symbols.add(symbol)
                        signal_history.append({
                            "time": datetime.now().isoformat(),
                            "symbol": symbol,
                            "funding": result["funding"],
                            "risk_score": result["risk_score"],
                            "net_profit": result["net_profit"],
                        })
                        save_data()
                        time.sleep(2)

            save_data()
        except Exception as e:
            print(f"❌ Мониторинг: {e}")
        time.sleep(MONITOR_INTERVAL)


def process_callbacks():
    offset = 0
    while True:
        try:
            data = get_updates(offset)
            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    if "callback_query" in update:
                        cb = update["callback_query"]
                        cb_data = cb.get("data")
                        if cb_data:
                            try:
                                SESSION.post(
                                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                                    json={"callback_query_id": cb["id"]},
                                    timeout=5
                                )
                            except:
                                pass
                            handle_callback(cb_data)
        except Exception as e:
            print(f"❌ Callbacks: {e}")
        time.sleep(1)


def reset_alerted_symbols():
    global alerted_symbols
    while True:
        time.sleep(8 * 3600)
        alerted_symbols.clear()
        print("🔄 Сброс оповещённых символов")


# ===== ЗАПУСК =====
if __name__ == "__main__":
    load_data()
    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=process_callbacks, daemon=True).start()
    threading.Thread(target=reset_alerted_symbols, daemon=True).start()

    send_telegram(
        "🚀 <b>АРБИТРАЖНЫЙ БОТ v2 ЗАПУЩЕН!</b>\n\nСтратегия: Funding Rate Arbitrage\nБиржа: Bybit\n\nНажми <b>🔍 СКАНИРОВАТЬ</b> для поиска сигналов\nили подожди — бот сам найдёт хорошие возможности.",
        reply_markup=main_keyboard()
    )

    print("🤖 Бот v2 запущен!")
    print("🔍 Мониторинг всех USDT пар на Bybit")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n❌ Бот остановлен")
        send_telegram("🛑 Бот остановлен")
