import os
import time
import requests
import sys
from datetime import date, datetime, timedelta
from flask import Flask, render_template_string, jsonify

app = Flask(__name__)

# ==== Конфіг ====
ACCOUNT_NAME = "poka-net3"
POSTER_TOKEN = os.getenv("POSTER_TOKEN")           # обязательный
CHOICE_TOKEN = os.getenv("CHOICE_TOKEN")           # опциональный (бронирования)
WEATHER_KEY = os.getenv("WEATHER_KEY", "")         # API ключ OpenWeather

# Категории POS ID
HOT_CATEGORIES  = {4, 13, 15, 46, 33}
COLD_CATEGORIES = {7, 8, 11, 16, 18, 19, 29, 32, 36, 44}
BAR_CATEGORIES  = {9,14,27,28,34,41,42,47,22,24,25,26,39,30}

# Кэш
PRODUCT_CACHE = {}
PRODUCT_CACHE_TS = 0
CACHE = {
    "hot": {}, "cold": {}, "hot_prev": {}, "cold_prev": {},
    "hourly": {}, "hourly_prev": {}, "share": {},
    "bookings": []
}
CACHE_TS = 0

# ===== Helpers =====
def _get(url, **kwargs):
    r = requests.get(url, timeout=kwargs.pop("timeout", 25))
    log_snippet = r.text[:500].replace("\n", " ")
    print(f"DEBUG GET {url.split('?')[0]} -> {r.status_code} : {log_snippet}", file=sys.stderr, flush=True)
    r.raise_for_status()
    return r

# ===== Справочник товаров =====
def load_products():
    global PRODUCT_CACHE, PRODUCT_CACHE_TS
    if PRODUCT_CACHE and time.time() - PRODUCT_CACHE_TS < 3600:
        return PRODUCT_CACHE

    mapping = {}
    per_page = 500
    for ptype in ("products", "batchtickets"):
        page = 1
        while True:
            url = (
                f"https://{ACCOUNT_NAME}.joinposter.com/api/menu.getProducts"
                f"?token={POSTER_TOKEN}&type={ptype}&per_page={per_page}&page={page}"
            )
            try:
                resp = _get(url)
                data = resp.json().get("response", [])
            except Exception as e:
                print("ERROR load_products:", e, file=sys.stderr, flush=True)
                break

            if not isinstance(data, list) or not data:
                break

            for item in data:
                try:
                    pid = int(item.get("product_id", 0))
                    cid = int(item.get("menu_category_id", 0))
                    if pid and cid:
                        mapping[pid] = cid
                except Exception:
                    continue

            if len(data) < per_page:
                break
            page += 1

    PRODUCT_CACHE = mapping
    PRODUCT_CACHE_TS = time.time()
    print(f"DEBUG products cached: {len(PRODUCT_CACHE)} items", file=sys.stderr, flush=True)
    return PRODUCT_CACHE

# ===== Сводные продажи =====
def fetch_category_sales(day_offset=0):
    target_date = (date.today() - timedelta(days=day_offset)).strftime("%Y-%m-%d")
    url = (
        f"https://{ACCOUNT_NAME}.joinposter.com/api/dash.getCategoriesSales"
        f"?token={POSTER_TOKEN}&dateFrom={target_date}&dateTo={target_date}"
    )
    try:
        resp = _get(url)
        rows = resp.json().get("response", [])
    except Exception as e:
        print("ERROR categories:", e, file=sys.stderr, flush=True)
        return {"hot": {}, "cold": {}, "bar": {}}

    hot, cold, bar = {}, {}, {}
    for row in rows:
        try:
            cid = int(row.get("category_id", 0))
            name = row.get("category_name", "").strip()
            qty = int(float(row.get("count", 0)))
        except Exception:
            continue

        if cid in HOT_CATEGORIES:
            hot[name] = hot.get(name, 0) + qty
        elif cid in COLD_CATEGORIES:
            cold[name] = cold.get(name, 0) + qty
        elif cid in BAR_CATEGORIES:
            bar[name] = bar.get(name, 0) + qty

    hot = dict(sorted(hot.items(), key=lambda x: x[0]))
    cold = dict(sorted(cold.items(), key=lambda x: x[0]))
    bar = dict(sorted(bar.items(), key=lambda x: x[0]))
    return {"hot": hot, "cold": cold, "bar": bar}

# ===== Почасовая диаграмма =====
def fetch_transactions_hourly(day_offset=0):
    products = load_products()
    target_date = (date.today() - timedelta(days=day_offset)).strftime("%Y-%m-%d")

    per_page = 500
    page = 1
    hours = list(range(10, 23))
    hot_by_hour = [0] * len(hours)
    cold_by_hour = [0] * len(hours)

    while True:
        url = (
            f"https://{ACCOUNT_NAME}.joinposter.com/api/transactions.getTransactions"
            f"?token={POSTER_TOKEN}&date_from={target_date}&date_to={target_date}"
            f"&per_page={per_page}&page={page}"
        )
        try:
            resp = _get(url)
            body = resp.json().get("response", {})
            items = body.get("data", []) or []
            total = int(body.get("count", 0))
            page_info = body.get("page", {}) or {}
            per_page_resp = int(page_info.get("per_page", per_page) or per_page)
        except Exception as e:
            print("ERROR transactions:", e, file=sys.stderr, flush=True)
            break

        if not items:
            break

        for trx in items:
            dt_str = trx.get("date_close")
            try:
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                hour = dt.hour
                if hour not in hours:
                    continue
                idx = hours.index(hour)
            except Exception:
                continue

            for p in trx.get("products", []) or []:
                try:
                    pid = int(p.get("product_id", 0))
                    qty = int(float(p.get("num", 0)))
                except Exception:
                    continue
                cid = products.get(pid, 0)
                if cid in HOT_CATEGORIES:
                    hot_by_hour[idx] += qty
                elif cid in COLD_CATEGORIES:
                    cold_by_hour[idx] += qty

        if per_page_resp * page >= total:
            break
        page += 1

    hot_cum, cold_cum = [], []
    th, tc = 0, 0
    for h, c in zip(hot_by_hour, cold_by_hour):
        th += h; tc += c
        hot_cum.append(th)
        cold_cum.append(tc)

    labels = [f"{h:02d}:00" for h in hours]
    return {"labels": labels, "hot": hot_cum, "cold": cold_cum}

# ===== Погода =====
def fetch_weather():
    if not WEATHER_KEY:
        return {"temp": "Н/Д", "desc": "Н/Д", "icon": ""}
    try:
        url = f"https://api.openweathermap.org/data/2.5/weather?lat=50.395&lon=30.355&appid={WEATHER_KEY}&units=metric&lang=uk"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        temp = round(data["main"]["temp"])
        desc = data["weather"][0]["description"].capitalize()
        icon = data["weather"][0]["icon"]
        return {"temp": f"{temp}°C", "desc": desc, "icon": icon}
    except Exception as e:
        print("ERROR weather:", e, file=sys.stderr, flush=True)
        return {"temp": "Н/Д", "desc": "Н/Д", "icon": ""}

# ===== Столы =====
HALL_TABLES = [1,2,3,4,5,6,8]
TERRACE_TABLES = [7,10,11,12,13]

def fetch_tables_with_waiters():
    target_date = date.today().strftime("%Y%m%d")
    url = (
        f"https://{ACCOUNT_NAME}.joinposter.com/api/dash.getTransactions"
        f"?token={POSTER_TOKEN}&dateFrom={target_date}&dateTo={target_date}"
    )
    try:
        resp = _get(url)
        rows = resp.json().get("response", [])
    except Exception as e:
        print("ERROR tables_with_waiters:", e, file=sys.stderr, flush=True)
        rows = []

    active = {}
    for trx in rows:
        try:
            status = int(trx.get("status", 0))
            if status == 2:   # закрытые пропускаемые
                continue
            tname = int(trx.get("table_name", 0))
            waiter = trx.get("name", "–")
            active[tname] = waiter
        except Exception:
            continue

    def build(zone_numbers):
        out = []
        for tnum in zone_numbers:
            occupied = tnum in active
            waiter = active.get(tnum, "–")
            out.append({
                "id": tnum,
                "name": f"Стол {tnum}",
                "waiter": waiter,
                "occupied": occupied
            })
        return out

    return {"hall": build(HALL_TABLES), "terrace": build(TERRACE_TABLES)}

# ===== Бронирования (Choice) =====
def fetch_bookings():
    if not CHOICE_TOKEN:
        return []
    
    try:
        today = date.today().strftime("%Y-%m-%d")
        tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        
        # API Choice для бронирований - наконец сегодня и завтра
        url = f"https://open-api.choiceqr.com/v2/restaurants/bookings"
        headers = {
            "Authorization": f"Bearer {CHOICE_TOKEN}",
            "Content-Type": "application/json"
        }
        params = {
            "date_from": today,
            "date_to": tomorrow,
            "status": "confirmed"  # Только подтвержденные брони
        }
        
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        
        data = resp.json()
        bookings = data.get("data", []) if isinstance(data, dict) else data
        
        if not isinstance(bookings, list):
            bookings = []
        
        # Форматируем данные
        formatted = []
        for booking in bookings[:10]:  # Показываем max 10 ближайших бронирований
            try:
                time_str = booking.get("time", booking.get("booking_time", ""))
                guests = booking.get("guests_count", booking.get("num_guests", 0))
                name = booking.get("guest_name", booking.get("name", "Гость"))
                phone = booking.get("phone", "")
                notes = booking.get("notes", booking.get("special_requests", ""))
                
                # Парсим время
                if isinstance(time_str, str) and time_str:
                    try:
                        if "T" in time_str:
                            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                        else:
                            dt = datetime.strptime(time_str, "%H:%M")
                        time_display = dt.strftime("%H:%M")
                    except:
                        time_display = time_str[:5] if len(time_str) >= 5 else time_str
                else:
                    time_display = "N/A"
                
                formatted.append({
                    "time": time_display,
                    "guests": int(guests) if guests else 0,
                    "name": str(name).strip() or "Гость",
                    "phone": str(phone).strip() or "",
                    "notes": str(notes).strip() or ""
                })
            except Exception as e:
                print(f"ERROR parsing booking: {e}", file=sys.stderr, flush=True)
                continue
        
        return formatted
        
    except Exception as e:
        print("ERROR fetch_bookings:", e, file=sys.stderr, flush=True)
        return []

# ===== API =====
@app.route("/api/sales")
def api_sales():
    global CACHE, CACHE_TS
    if time.time() - CACHE_TS > 60:
        sums_today = fetch_category_sales(0)
        sums_prev = fetch_category_sales(7)
        hourly = fetch_transactions_hourly(0)
        prev = fetch_transactions_hourly(7)

        total_hot = sum(sums_today["hot"].values())
        total_cold = sum(sums_today["cold"].values())
        total_bar = sum(sums_today["bar"].values())
        total_sum = total_hot + total_cold + total_bar
        share = {
            "hot": round(total_hot/total_sum*100) if total_sum else 0,
            "cold": round(total_cold/total_sum*100) if total_sum else 0,
            "bar": round(total_bar/total_sum*100) if total_sum else 0,
        }

        CACHE.update({
            "hot": sums_today["hot"], "cold": sums_today["cold"],
            "hot_prev": sums_prev["hot"], "cold_prev": sums_prev["cold"],
            "hourly": hourly, "hourly_prev": prev,
            "share": share, "weather": fetch_weather(),
            "bookings": fetch_bookings()
        })
        CACHE_TS = time.time()

    return jsonify(CACHE)

@app.route("/api/tables")
def api_tables():
    return jsonify(fetch_tables_with_waiters())

# ===== UI =====
@app.route("/")
def index():
    template = """
    <!DOCTYPE html>
    <html lang="uk">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Kitchen Dashboard</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2"></script>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }

            :root {
                --bg-primary: #000000;
                --bg-secondary: #1c1c1e;
                --bg-tertiary: #2c2c2e;
                --text-primary: #ffffff;
                --text-secondary: #8e8e93;
                --accent-hot: #ff9500;
                --accent-cold: #007aff;
                --accent-bar: #af52de;
                --accent-success: #30d158;
                --accent-warning: #ff9500;
                --border-color: #38383a;
                --shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
            }

            body {
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: var(--bg-primary);
                color: var(--text-primary);
                overflow: hidden;
                height: 100vh;
                padding: 8px;
            }

            .dashboard {
                display: grid;
                grid-template-columns: 1fr 1fr 1fr 1fr;
                grid-template-rows: minmax(0, 35vh) minmax(0, 30vh) minmax(0, 32vh);
                gap: 8px;
                height: calc(100vh - 25px);
                max-height: calc(100vh - 25px);
                padding: 0;
            }

            .card {
                background: var(--bg-secondary);
                border-radius: 12px;
                padding: 10px;
                border: 1px solid var(--border-color);
                box-shadow: var(--shadow);
                overflow: hidden;
                display: flex;
                flex-direction: column;
            }

            .card h2 {
                font-size: 14px;
                font-weight: 600;
                margin-bottom: 8px;
                display: flex;
                align-items: center;
                gap: 6px;
                color: var(--text-primary);
            }

            .card.hot h2 { color: var(--accent-hot); }
            .card.cold h2 { color: var(--accent-cold); }
            .card.share h2 { color: var(--accent-bar); }
            .card.bookings h2 { color: #34c759; }

            /* Верхний ряд блоков */
            .card.top-card {
                min-height: 0;
            }

            /* Таблицы в карточках - оптимизированный шрифт */
            table {
                width: 100%;
                border-collapse: collapse;
                font-size: 13px;
                margin-top: auto;
            }

            th, td {
                padding: 5px 7px;
                text-align: right;
                border-bottom: 1px solid var(--border-color);
            }

            th:first-child, td:first-child {
                text-align: left;
            }

            th {
                color: var(--text-secondary);
                font-weight: 600;
                font-size: 11px;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }

            td {
                color: var(--text-primary);
                font-weight: 600;
                font-size: 13px;
            }

            /* Блок с распределением заказов - компактный пирог */
            .pie-container {
                flex: 1;
                display: flex;
                align-items: center;
                justify-content: center;
                min-height: 0;
                position: relative;
                padding: 5px;
            }

            /* Блок время и погоды - МАКСИМАЛЬНО УВЕЛИЧЕН */
            .time-weather {
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                text-align: center;
                flex: 1;
                padding: 5px;
                height: 100%;
            }

            .clock {
                font-size: 68px;
                font-weight: 900;
                color: var(--text-primary);
                font-variant-numeric: tabular-nums;
                margin-bottom: 8px;
                line-height: 0.85;
            }

            .weather {
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 4px;
                flex: 1;
            }

            .weather img {
                width: 100px;
                height: 100px;
                margin-bottom: 2px;
            }

            .temp {
                font-size: 36px;
                font-weight: 800;
                color: var(--text-primary);
                line-height: 1;
            }

            .desc {
                font-size: 15px;
                color: var(--text-secondary);
                text-align: center;
                font-weight: 600;
            }

            /* График заказов */
            .chart-card {
                grid-column: 1 / 3;
                display: flex;
                flex-direction: column;
            }

            .chart-container {
                flex: 1;
                min-height: 0;
                position: relative;
            }

            /* Бронирования - новый блок */
            .bookings-card {
                grid-column: 3 / 5;
                grid-row: 2;
                display: flex;
                flex-direction: column;
            }

            .bookings-list {
                flex: 1;
                overflow-y: auto;
                display: flex;
                flex-direction: column;
                gap: 6px;
                min-height: 0;
            }

            .booking-item {
                background: var(--bg-tertiary);
                border: 1px solid var(--border-color);
                border-radius: 8px;
                padding: 8px;
                font-size: 12px;
                line-height: 1.4;
            }

            .booking-time {
                font-weight: 700;
                color: #34c759;
                font-size: 13px;
                margin-bottom: 4px;
            }

            .booking-name {
                color: var(--text-primary);
                font-weight: 600;
                margin-bottom: 2px;
            }

            .booking-guests {
                color: var(--text-secondary);
                font-size: 11px;
                margin-bottom: 2px;
            }

            .booking-phone {
                color: var(--text-secondary);
                font-size: 11px;
                word-break: break-all;
                margin-bottom: 2px;
            }

            .booking-notes {
                color: var(--text-secondary);
                font-size: 11px;
                font-style: italic;
                margin-top: 4px;
                padding-top: 4px;
                border-top: 1px solid var(--border-color);
            }

            .no-bookings {
                display: flex;
                align-items: center;
                justify-content: center;
                color: var(--text-secondary);
                height: 100%;
                font-size: 12px;
                text-align: center;
            }

            /* Столы */
            .tables-card {
                grid-column: 1 / 5;
                grid-row: 3;
                display: flex;
                flex-direction: column;
            }

            .tables-content {
                flex: 1;
                display: flex;
                flex-direction: row;
                gap: 20px;
                min-height: 0;
                padding-right: 8px;
            }

            .tables-zone {
                flex: 1;
                min-height: 0;
                display: flex;
                flex-direction: column;
            }

            .tables-zone h3 {
                font-size: 12px;
                font-weight: 600;
                margin-bottom: 6px;
                color: var(--text-secondary);
                display: flex;
                align-items: center;
                gap: 4px;
            }

            .tables-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
                gap: 8px;
                flex: 1;
                align-content: start;
            }

            .table-tile {
                border-radius: 12px;
                padding: 15px 10px;
                font-weight: 700;
                text-align: center;
                font-size: 16px;
                display: flex;
                flex-direction: column;
                justify-content: center;
                gap: 6px;
                transition: all 0.2s ease;
                border: 1px solid var(--border-color);
                height: 105px;
                width: 130px;
                justify-self: center;
            }

            .table-tile.occupied {
                background: linear-gradient(135deg, var(--accent-cold), #005ecb);
                color: white;
                border-color: var(--accent-cold);
                box-shadow: 0 2px 8px rgba(0, 122, 255, 0.3);
            }

            .table-tile.free {
                background: var(--bg-tertiary);
                color: var(--text-secondary);
                border-color: var(--border-color);
            }

            .table-number {
                font-weight: 800;
                font-size: 18px;
                margin-bottom: 4px;
            }

            .table-waiter {
                font-size: 14px;
                font-weight: 700;
                opacity: 0.95;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
                max-width: 100%;
                line-height: 1.2;
            }

            /* Logo - компактный */
            .logo {
                position: fixed;
                right: 15px;
                bottom: 5px;
                font-family: 'Inter', sans-serif;
                font-weight: 800;
                font-size: 14px;
                color: #ffffff;
                z-index: 1000;
                background: var(--bg-secondary);
                padding: 4px 8px;
                border-radius: 6px;
                border: 1px solid var(--border-color);
            }

            /* Canvas styling */
            canvas {
                max-width: 100% !important;
                max-height: 100% !important;
            }

            /* Responsive adjustments для очень маленьких экранов */
            @media (max-height: 800px) {
                body {
                    padding: 6px;
                }
                
                .dashboard {
                    gap: 6px;
                    grid-template-rows: minmax(0, 33vh) minmax(0, 30vh) minmax(0, 34vh);
                }
                
                .card {
                    padding: 8px;
                }
                
                .card h2 {
                    font-size: 12px;
                    margin-bottom: 6px;
                }
                
                .clock {
                    font-size: 56px;
                }
                
                .weather img {
                    width: 85px;
                    height: 85px;
                }
                
                .temp {
                    font-size: 30px;
                }
                
                table {
                    font-size: 12px;
                }
                
                th {
                    font-size: 10px;
                }
                
                td {
                    font-size: 12px;
                }
                
                .table-tile {
                    height: 90px;
                    width: 115px;
                    padding: 12px 8px;
                }
                
                .table-number {
                    font-size: 16px;
                }
                
                .table-waiter {
                    font-size: 13px;
                }
            }

            @media (max-width: 1200px) {
                .tables-grid {
                    grid-template-columns: repeat(auto-fit, minmax(115px, 1fr));
                }
                
                .table-tile {
                    width: 115px;
                    height: 95px;
                    font-size: 15px;
                }
                
                .table-number {
                    font-size: 17px;
                }
                
                .table-waiter {
                    font-size: 13px;
                }
            }
        </style>
    </head>
    <body>
        <div class="dashboard">
            <!-- Верхний ряд -->
            <div class="card hot top-card">
                <h2>🔥 Гарячий цех</h2>
                <div style="flex: 1; overflow: hidden;">
                    <table id="hot_tbl"></table>
                </div>
            </div>

            <div class="card cold top-card">
                <h2>❄️ Холодний цех</h2>
                <div style="flex: 1; overflow: hidden;">
                    <table id="cold_tbl"></table>
                </div>
            </div>

            <div class="card share top-card">
                <h2>📊 Розподіл замовлень</h2>
                <div class="pie-container">
                    <canvas id="pie" width="180" height="180"></canvas>
                </div>
            </div>

            <div class="card top-card">
                <h2>🕐 Час і погода</h2>
                <div class="time-weather">
                    <div id="clock" class="clock"></div>
                    <div class="weather">
                        <div id="weather-icon"></div>
                        <div id="weather-temp" class="temp"></div>
                        <div id="weather-desc" class="desc"></div>
                    </div>
                </div>
            </div>

            <!--
