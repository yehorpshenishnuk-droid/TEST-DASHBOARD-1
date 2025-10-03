import os
import time
import requests
import sys
from datetime import date, datetime, timedelta
from flask import Flask, render_template_string, jsonify
from typing import Dict, List, Any

app = Flask(__name__)

# ==== Конфиг ====
# NOTE: В идеале эти значения должны быть в .env, но для простоты их оставили тут.
ACCOUNT_NAME = "poka-net3"
POSTER_TOKEN = os.getenv("POSTER_TOKEN")           # обязателен
CHOICE_TOKEN = os.getenv("CHOICE_TOKEN")           # опционален (бронирования)
WEATHER_KEY = os.getenv("WEATHER_KEY", "")         # API ключ OpenWeather
WEATHER_CITY_ID = os.getenv("WEATHER_CITY_ID", "703448") # Kyiv ID, можно изменить

POSTER_BASE_URL = f"https://{ACCOUNT_NAME}.joinposter.com/api"
CHOICE_BASE_URL = "https://admin.choiceqr.com/api/v1" 

# Категории POS ID (для Горячего, Холодного цехов и Бара)
HOT_CATEGORIES  = {4, 13, 15, 46, 33}
COLD_CATEGORIES = {7, 8, 11, 16, 18, 19, 29, 32, 36, 44}
BAR_CATEGORIES  = {9,14,27,28,34,41,42,47,22,24,25,26,39,30}

# Кэш
PRODUCT_CACHE: Dict[int, Any] = {}
PRODUCT_CACHE_TS = 0
CACHE: Dict[str, Any] = {
    "hot": {}, "cold": {}, "hot_prev": {}, "cold_prev": {},\
    "hourly": {}, "hourly_prev": {}, "share": {},\
    "weather": {}, "tables": {"hall": [], "terrace": []},\
    "bookings": [] # <-- ДОБАВЛЕНО: Кэш для бронирований
}
CACHE_TS = 0

# ===== Helpers =====

def _get(url, **kwargs):
    """Хелпер для запросов к Poster API"""
    if not POSTER_TOKEN:
        print("POSTER_TOKEN is not set. Skipping Poster API request.", file=sys.stderr)
        return {"response": []}

    params = kwargs.pop("params", {})
    params['token'] = POSTER_TOKEN
    
    try:
        r = requests.get(url, params=params, timeout=kwargs.pop("timeout", 25))
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        print(f"Poster API error on {url}: {e}", file=sys.stderr)
        return {"response": []}

def _choice_get(path, **kwargs):
    """Хелпер для запросов к Choice API"""
    if not CHOICE_TOKEN:
        print("CHOICE_TOKEN is not set. Skipping Choice API request.", file=sys.stderr)
        return []

    headers = {"Authorization": CHOICE_TOKEN}
    url = f"{CHOICE_BASE_URL}{path}"
    
    try:
        r = requests.get(url, headers=headers, timeout=kwargs.pop("timeout", 25), **kwargs)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        print(f"Choice API error on {url}: {e}", file=sys.stderr)
        return []


def cutToNow(data: Dict[str, float], day_offset: int = 0) -> Dict[str, float]:
    """Обрезает почасовые данные до текущего часа (или часа со смещением)."""
    now = datetime.now() - timedelta(days=day_offset)
    current_hour = now.hour
    
    if day_offset > 0:
        return data

    filtered = {}
    for hour, value in data.items():
        if int(hour) <= current_hour:
            filtered[hour] = value
    return filtered


# ===== Data Fetching: Poster =====

def fetch_product_list():
    """Загружает список продуктов для определения цехов."""
    global PRODUCT_CACHE, PRODUCT_CACHE_TS
    
    # Обновляем кэш продуктов не чаще, чем раз в 6 часов
    if time.time() - PRODUCT_CACHE_TS < 6 * 3600:
        return PRODUCT_CACHE

    res = _get(f"{POSTER_BASE_URL}/menu.getProducts")
    products = res.get("response", [])

    product_map = {}
    for p in products:
        category_id = int(p.get("menu_category_id"))
        
        if category_id in HOT_CATEGORIES:
            product_map[int(p.get("product_id"))] = "hot"
        elif category_id in COLD_CATEGORIES:
            product_map[int(p.get("product_id"))] = "cold"
        elif category_id in BAR_CATEGORIES:
            product_map[int(p.get("product_id"))] = "bar"
        else:
            product_map[int(p.get("product_id"))] = "other"

    PRODUCT_CACHE = product_map
    PRODUCT_CACHE_TS = time.time()
    return PRODUCT_CACHE

def fetch_transactions_hourly(day_offset: int = 0):
    """Получает почасовую статистику продаж."""
    # Получаем сегодняшнюю дату или дату со смещением
    day = (datetime.now() - timedelta(days=day_offset)).strftime("%Y%m%d")

    res = _get(
        f"{POSTER_BASE_URL}/transactions.getTransactions",
        params={
            "dateFrom": day,
            "dateTo": day
        }
    )
    transactions = res.get("response", [])
    product_map = fetch_product_list()
    
    hourly_sales: Dict[str, int] = {str(h): 0 for h in range(24)}
    
    for t in transactions:
        # Учитываем только закрытые чеки
        if t.get("status") != "CLOSED":
            continue

        try:
            # Парсим время закрытия чека
            close_time = datetime.strptime(t.get("closed_at"), "%Y-%m-%d %H:%M:%S")
            hour = str(close_time.hour)
            
            # Считаем количество проданных позиций (не сумму)
            for product in t.get("products", []):
                product_id = int(product.get("product_id"))
                category = product_map.get(product_id, "other")
                
                if category in ("hot", "cold"):
                    quantity = float(product.get("count", 0))
                    hourly_sales[hour] += int(quantity)

        except Exception as e:
            print(f"Error processing transaction: {e}", file=sys.stderr)
            continue
    
    # Накопительный итог
    cumulative_sales: Dict[str, float] = {}
    current_sum = 0
    for hour in sorted(hourly_sales.keys(), key=int):
        current_sum += hourly_sales[hour]
        cumulative_sales[hour] = current_sum
        
    return cumulative_sales


def fetch_data(day_offset: int = 0):
    """Получает сводные данные по продажам за день."""
    day = (datetime.now() - timedelta(days=day_offset)).strftime("%Y%m%d")
    
    res = _get(
        f"{POSTER_BASE_URL}/dash.getCategoriesSales",
        params={
            "dateFrom": day,
            "dateTo": day
        }
    )
    sales = res.get("response", {}).get("categories", [])
    
    hot_sales, cold_sales, bar_sales = 0, 0, 0
    
    # 1. Сводная продажа по цехам (количество)
    for s in sales:
        category_id = int(s.get("category_id"))
        count = int(s.get("count", 0))

        if category_id in HOT_CATEGORIES:
            hot_sales += count
        elif category_id in COLD_CATEGORIES:
            cold_sales += count
        elif category_id in BAR_CATEGORIES:
            bar_sales += count
    
    # 2. Почасовые данные
    hourly_data = fetch_transactions_hourly(day_offset)
    
    return {
        "hot_count": hot_sales,
        "cold_count": cold_sales,
        "bar_count": bar_sales,
        "hourly_data": hourly_data
    }

def fetch_tables():
    """Получает текущий статус столов."""
    res = _get(f"{POSTER_BASE_URL}/dash.getTransactions")
    tables = res.get("response", {}).get("tables", [])

    hall, terrace = [], []

    for t in tables:
        # Учитываем только занятые столы
        if int(t.get("status")) == 0:
            continue
        
        table_data = {
            "name": t.get("name"),
            "status": "Busy",
            "time": t.get("time_diff"), # Время с момента открытия
            "officer": t.get("officer_name") or "—",
            "guests": t.get("guests_count") or 0
        }
        
        # Разделение по зонам (предполагаем, что имя зоны указано в имени стола)
        if "Терраса" in t.get("name", "") or "Terrace" in t.get("name", ""):
            terrace.append(table_data)
        else:
            hall.append(table_data)
            
    return {"hall": hall, "terrace": terrace}

# ===== Data Fetching: OpenWeatherMap =====

def fetch_weather():
    """Получает текущую погоду."""
    if not WEATHER_KEY:
        print("WEATHER_KEY is not set. Skipping weather request.", file=sys.stderr)
        return {"temp": "—", "desc": "—", "icon": None}

    url = "https://api.openweathermap.org/data/2.5/weather"
    
    try:
        r = requests.get(
            url, 
            params={
                "id": WEATHER_CITY_ID,
                "units": "metric",
                "lang": "ru",
                "appid": WEATHER_KEY
            },
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        
        temp = f"{round(data['main']['temp'])}°C"
        desc = data['weather'][0]['description'].capitalize()
        icon = data['weather'][0]['icon']
        
        return {"temp": temp, "desc": desc, "icon": icon}

    except requests.exceptions.RequestException as e:
        print(f"Weather API error: {e}", file=sys.stderr)
        return {"temp": "—", "desc": "—", "icon": None}


# ===== Data Fetching: Choice (Бронирование) =====

def fetch_bookings() -> List[Dict[str, Any]]:
    """Получает только будущие бронирования на сегодня и завтра."""
    now = datetime.now()
    
    # 1. Запрашиваем бронирования, начиная с текущей секунды
    # Формат: UTC ISODate
    from_dt = now.isoformat() + 'Z' 
    # 2. Ограничиваем до конца следующего дня
    till_dt = (now + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0).isoformat() + 'Z' 

    path = "/bookings/list"
    params = {
        "from": from_dt,
        "till": till_dt,
        "periodField": "bookingDt", # Фильтруем по времени бронирования
        "perPage": 100 
    }
    
    raw_bookings = _choice_get(path, params=params)
    
    bookings = []
    if raw_bookings:
        offset_seconds = now.astimezone().utcoffset().total_seconds()

        for b in raw_bookings:
            status = b.get("status")
            
            # Показываем только созданные или подтвержденные бронирования
            if status in ("created", "confirmed"):
                booking_dt_str = b.get("dateTime")
                if not booking_dt_str: continue 

                # Парсим UTC время
                booking_time_utc = datetime.fromisoformat(booking_dt_str.replace('Z', '+00:00'))
                
                # Конвертируем в локальное время для отображения
                local_time = booking_time_utc + timedelta(seconds=offset_seconds)

                # Выделяем бронирование, которое приходится на текущий час
                is_current = (local_time.hour == now.hour) and (local_time.date() == now.date())

                bookings.append({
                    "time": local_time.strftime("%H:%M"),
                    "name": b.get("customer", {}).get("name") or "Гість",
                    "guests": b.get("personCount", 0),
                    "status": status,
                    "is_current": is_current
                })

        # Сортируем по времени
        bookings.sort(key=lambda x: datetime.strptime(x["time"], "%H:%M"))
    
    CACHE["bookings"] = bookings
    return bookings


# ===== Cache Update Logic =====

def update_cache():
    """Обновляет весь кэш данных (кроме таблиц)."""
    global CACHE_TS

    # Сначала обновляем список продуктов
    fetch_product_list()

    # Основные данные (сегодня и 7 дней назад)
    today_data = fetch_data(day_offset=0)
    last_week_data = fetch_data(day_offset=7)
    
    CACHE["hot"] = today_data["hot_count"]
    CACHE["cold"] = today_data["cold_count"]
    CACHE["hot_prev"] = last_week_data["hot_count"]
    CACHE["cold_prev"] = last_week_data["cold_count"]
    
    # Почасовой график (сегодня обрезан до текущего часа)
    CACHE["hourly"] = today_data["hourly_data"]
    CACHE["hourly_prev"] = last_week_data["hourly_data"]
    
    # Распределение заказов
    total_sales = today_data["hot_count"] + today_data["cold_count"] + today_data["bar_count"]
    if total_sales > 0:
        CACHE["share"] = {
            "hot": round(today_data["hot_count"] / total_sales * 100, 1),
            "cold": round(today_data["cold_count"] / total_sales * 100, 1),
            "bar": round(today_data["bar_count"] / total_sales * 100, 1)
        }
    else:
        CACHE["share"] = {"hot": 0, "cold": 0, "bar": 0}

    # Бронирования
    if CHOICE_TOKEN:
        fetch_bookings()

    # Погода
    CACHE["weather"] = fetch_weather()
    
    CACHE_TS = time.time()
    print("Cache updated successfully.", file=sys.stderr)


# ===== API Endpoints =====

@app.route("/api/data")
def api_data():
    """API endpoint для сводных продаж и почасового графика."""
    if time.time() - CACHE_TS > 60:
        update_cache()
        
    return jsonify(
        hot=CACHE["hot"], cold=CACHE["cold"], 
        hot_prev=CACHE["hot_prev"], cold_prev=CACHE["cold_prev"],
        hourly=cutToNow(CACHE["hourly"]), hourly_prev=cutToNow(CACHE["hourly_prev"], day_offset=7),
        share=CACHE["share"],
        weather=CACHE["weather"]
    )

@app.route("/api/tables")
def api_tables():
    """API endpoint для статуса столов."""
    tables_data = fetch_tables()
    CACHE["tables"] = tables_data
    return jsonify(hall=tables_data["hall"], terrace=tables_data["terrace"])

@app.route("/api/bookings")
def api_bookings():
    """API endpoint для бронирований."""
    # Бронирования обновляются вместе с основным кэшем, но можно обновить их отдельно
    if time.time() - CACHE_TS > 60: 
        fetch_bookings()
    return jsonify(bookings=CACHE["bookings"])


# ===== Main App Route =====

@app.route("/")
def index():
    # Первый вызов для заполнения кэша при старте
    if CACHE_TS == 0:
        update_cache()

    template = f"""
    <!DOCTYPE html>
    <html lang="uk">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Кухонний Дашборд</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
        <style>
            /* Сброс и базовые стили */
            body {{
                font-family: 'Inter', sans-serif;
                margin: 0;
                background-color: #121212;
                color: #e0e0e0;
                height: 100vh;
                overflow: hidden;
            }}

            /* Общие стили для карточек */
            .card {{
                background-color: #1e1e1e;
                border-radius: 8px;
                padding: 15px;
                box-shadow: 0 4px 8px rgba(0, 0, 0, 0.2);
                display: flex;
                flex-direction: column;
            }}
            .card h2 {{
                margin-top: 0;
                font-size: 1.5em;
                color: #ff9800; 
                border-bottom: 2px solid #333;
                padding-bottom: 5px;
            }}
            h3 {{
                margin-top: 0;
                font-size: 1.2em;
                color: #ccc;
            }}

            /* ГРИД - ОСНОВА МАКЕТА */
            .dashboard {{
                display: grid;
                /* 4 равные колонки */
                grid-template-columns: 1fr 1fr 1fr 1fr; 
                /* 2 ряда: 35% высоты для верхнего, 58% для нижнего */
                grid-template-rows: minmax(0, 35vh) minmax(0, 58vh); 
                gap: 15px;
                padding: 15px;
                height: 100vh;
                box-sizing: border-box;
                overflow: hidden;
            }}
            
            /* ВЕРХНИЙ РЯД: 3 блока (Продажи Г/Х, Продажи Бар, Время/Погода) */
            .sales-hot-cold-card {{
                grid-column: 1 / 3; /* 2 колонки */
                grid-row: 1;
            }}
            .sales-bar-card {{
                grid-column: 3 / 4; /* 1 колонка */
                grid-row: 1;
            }}
            .time-weather-card {{
                grid-column: 4 / 5; /* 1 колонка */
                grid-row: 1;
                display: flex;
                flex-direction: column;
                justify-content: space-between;
                text-align: right;
            }}

            /* НИЖНИЙ РЯД: График (1/4), Бронирование (1/4), Столы (2/4) */
            .chart-card {{
                grid-column: 1 / 2; /* <-- 1 колонка */
                grid-row: 2;
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
            }}

            .bookings-card {{
                grid-column: 2 / 3; /* <-- 1 колонка */
                grid-row: 2;
                display: flex;
                flex-direction: column;
                overflow: hidden; 
            }}

            .tables-card {{
                grid-column: 3 / 5; /* <-- 2 колонки */
                grid-row: 2;
                display: flex;
                flex-direction: column;
            }}

            /* СТИЛИ ДЛЯ ВНУТРЕННИХ ЭЛЕМЕНТОВ */

            /* ... (Продажи) ... */
            .data-table table {{
                width: 100%;
                border-collapse: collapse;
                font-size: 1.1em;
                margin-top: 5px;
            }}
            .data-table th, .data-table td {{
                padding: 8px;
                text-align: left;
            }}
            .data-table th {{
                color: #999;
                font-weight: normal;
                border-bottom: 1px solid #333;
            }}
            .data-table td {{
                border-bottom: 1px solid #222;
            }}
            .data-table tr:last-child td {{
                border-bottom: none;
            }}
            .data-table .count-val {{
                font-size: 1.5em;
                font-weight: bold;
                color: #e0e0e0;
                text-align: right;
            }}
            .data-table .prev-val {{
                font-size: 0.8em;
                color: #999;
                text-align: right;
                display: block;
            }}

            /* ... (Время и Погода) ... */
            .time-large {{
                font-size: 3.5em;
                font-weight: bold;
                color: #ff9800;
                line-height: 1.1;
            }}
            .temp-large {{
                font-size: 2.5em;
                font-weight: bold;
                color: #e0e0e0;
                line-height: 1;
            }}
            .desc-small {{
                font-size: 1em;
                color: #999;
            }}
            #weather-container img {{
                width: 60px;
                height: 60px;
                margin-right: 10px;
            }}

            /* ... (Столы) ... */
            .table-area h3 {{
                color: #ff9800;
            }}
            .tables-grid {{
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
                padding-bottom: 10px;
            }}
            .table-tile {{
                padding: 10px;
                border-radius: 5px;
                font-size: 0.9em;
                width: 120px;
                min-height: 80px;
                box-shadow: 0 2px 4px rgba(0, 0, 0, 0.3);
                transition: background-color 0.3s;
            }}
            .table-tile.busy {{
                background-color: #442a2a; /* Темно-красный */
                border: 1px solid #ff4d4d;
            }}
            .table-tile.busy .table-info {{
                font-weight: bold;
                color: #ff9800;
            }}
            .table-tile .table-info {{
                color: #ccc;
            }}

            /* СТИЛИ ДЛЯ БРОНИРОВАНИЯ (ОБНОВЛЕННЫЕ) */
            .bookings-list {{
                list-style: none;
                padding: 0;
                margin: 0;
                overflow-y: auto; 
                flex-grow: 1;
                font-size: 1.1em;
            }}
            .booking-item {{
                padding: 8px 10px;
                border-bottom: 1px solid #333;
                display: grid;
                /* Ім'я (3 частини), Час (1 частина), Гості (1 частина) */
                grid-template-columns: 1fr 60px 40px; 
                gap: 5px;
                align-items: center;
                line-height: 1.2;
            }}
            .booking-item:first-child {{
                /* Стиль для заголовка, который мы добавляем через JS */
                font-size: 0.9em; 
                color: #999; 
                border-bottom: 2px solid #555 !important;
                padding-bottom: 5px;
                font-weight: normal;
            }}
            .booking-item:last-child {{
                border-bottom: none;
            }}

            /* Статусы */
            .booking-item.confirmed {{
                background-color: #333d33; /* Мягкий зеленый/серый фон */
            }}
            .booking-item.created {{
                background-color: #2a2a44; /* Мягкий синий/серый фон для нового */
            }}
            /* Выделение текущего часа */
            .booking-item.is_current {{
                background-color: #442A2A; 
                font-weight: bold;
                border: 1px solid #ff9800;
            }}
            .booking-time {{
                font-weight: bold;
                color: #ff9800; /* Оранжевый */
            }}
            .booking-name {{
                font-weight: bold; 
                overflow: hidden;
                white-space: nowrap;
                text-overflow: ellipsis;
                color: #e0e0e0;
            }}
            .booking-guests {{
                text-align: right;
                color: #ccc;
            }}

        </style>
    </head>
    <body>
        <div class="dashboard">
            <div class="card sales-hot-cold-card">
                <h2>Продажі по цехах (шт.)</h2>
                <div id="sales-hot-cold-table" class="data-table"></div>
            </div>

            <div class="card sales-bar-card">
                <h2>Розподіл замовлень</h2>
                <div style="flex-grow: 1; display: flex; align-items: center; justify-content: center;">
                    <canvas id="sales-pie-chart"></canvas>
                </div>
            </div>

            <div class="card time-weather-card">
                <div id="clock-container"><span id="clock" class="time-large">--:--</span></div>
                <div id="weather-container" style="display: flex; align-items: center; justify-content: flex-end;">
                    <div id="weather-icon"></div>
                    <div>
                        <div id="weather-temp" class="temp-large">—</div>
                        <div id="weather-desc" class="desc-small">—</div>
                    </div>
                </div>
            </div>

            <div class="card chart-card">
                <h2>Динаміка замовлень (по годинах)</h2>
                <div style="height: 100%; width: 100%; padding: 0 5px;">
                    <canvas id="hourly-chart"></canvas>
                </div>
            </div>

            <div class="card bookings-card">
                <h2>Бронювання</h2>
                <ul id="bookings-list" class="bookings-list">
                    <li class="booking-item" style="padding-bottom: 5px; border-bottom: 2px solid #555; font-size: 0.9em; color: #999; font-weight: normal;">
                        <div>Ім'я</div>
                        <div>Час</div>
                        <div style="text-align: right;">Гості</div>
                    </li>
                    <li style="text-align: center; color: #999;">Завантаження бронювань...</li>
                </ul>
            </div>

            <div class="card tables-card">
                <h2>Статус столів</h2>
                <div style="flex-grow: 1; display: flex; flex-direction: column; gap: 10px; overflow-y: auto;">
                    <div class="table-area">
                        <h3>Зал</h3>
                        <div id="hall-tables" class="tables-grid"></div>
                    </div>
                    <div class="table-area">
                        <h3>Тераса</h3>
                        <div id="terrace-tables" class="tables-grid"></div>
                    </div>
                </div>
            </div>

        </div>

        <script>
        // Глобальная переменная для графика Chart.js
        let hourlyChart;
        
        // ======== Рендер столов ========
        function renderTables(area, tables) {{
            const gridEl = document.getElementById(`${{area}}-tables`);
            gridEl.innerHTML = '';

            if (tables.length === 0) {{
                gridEl.innerHTML = '<span style="color: #666;">Немає зайнятих столів.</span>';
                return;
            }}

            tables.forEach(t => {{
                const tile = document.createElement('div');
                tile.className = `table-tile busy`;
                tile.innerHTML = `
                    <h3>${{t.name}}</h3>
                    <div class="table-info">${{t.time}} хв.</div>
                    <div class="table-info">${{t.officer}}</div>
                    <div class="table-info">${{t.guests}} чол.</div>
                `;
                gridEl.appendChild(tile);
            }});
        }}

        // ======== Рендер основных данных и графиков ========
        function renderSalesTable(hot, cold, hot_prev, cold_prev) {{
            const tableHtml = `
                <table>
                    <thead>
                        <tr>
                            <th>Цех</th>
                            <th style="text-align: right;">Сьогодні</th>
                            <th style="text-align: right;">Минулий тиждень</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td>Гарячий цех</td>
                            <td class="count-val">${{hot}}</td>
                            <td class="prev-val">${{hot_prev}}</td>
                        </tr>
                        <tr>
                            <td>Холодний цех</td>
                            <td class="count-val">${{cold}}</td>
                            <td class="prev-val">${{cold_prev}}</td>
                        </tr>
                    </tbody>
                </table>
            `;
            document.getElementById('sales-hot-cold-table').innerHTML = tableHtml;
        }}

        function renderPieChart(share) {{
            const ctx = document.getElementById('sales-pie-chart').getContext('2d');
            const data = [share.hot, share.cold, share.bar];
            
            if (hourlyChart) {{
                hourlyChart.destroy();
            }}

            hourlyChart = new Chart(ctx, {{
                type: 'pie',
                data: {{
                    labels: ['Гарячий цех', 'Холодний цех', 'Бар'],
                    datasets: [{{
                        data: data,
                        backgroundColor: ['#ff9800', '#2196f3', '#4CAF50'],
                        borderColor: '#1e1e1e'
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        legend: {{
                            labels: {{
                                color: '#e0e0e0',
                                font: {{ size: 14 }}
                            }}
                        }},
                        tooltip: {{
                            callbacks: {{
                                label: function(context) {{
                                    return context.label + ': ' + context.formattedValue + '%';
                                }}
                            }}
                        }}
                    }}
                }}
            }});
        }}

        function renderHourlyChart(hourly, hourly_prev) {{
            const ctx = document.getElementById('hourly-chart').getContext('2d');
            
            // Объединение ключей для осей X
            const allHours = new Set([...Object.keys(hourly), ...Object.keys(hourly_prev)]);
            const labels = Array.from(allHours).sort((a, b) => parseInt(a) - parseInt(b)).map(h => `${{h}}:00`);

            const dataToday = labels.map(label => hourly[parseInt(label)] || 0);
            const dataPrev = labels.map(label => hourly_prev[parseInt(label)] || 0);

            if (hourlyChart) {{
                hourlyChart.destroy();
            }}

            hourlyChart = new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels: labels,
                    datasets: [
                        {{
                            label: 'Сьогодні',
                            data: dataToday,
                            borderColor: '#ff9800',
                            backgroundColor: 'rgba(255, 152, 0, 0.2)',
                            fill: true,
                            tension: 0.3
                        }},
                        {{
                            label: 'Минулий тиждень',
                            data: dataPrev,
                            borderColor: '#2196f3',
                            backgroundColor: 'transparent',
                            borderDash: [5, 5],
                            tension: 0.3
                        }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {{
                        y: {{
                            beginAtZero: true,
                            title: {{ display: true, text: 'Кількість позицій', color: '#e0e0e0' }},
                            ticks: {{ color: '#e0e0e0' }},
                            grid: {{ color: 'rgba(255, 255, 255, 0.1)' }}
                        }},
                        x: {{
                            title: {{ display: true, text: 'Час', color: '#e0e0e0' }},
                            ticks: {{ color: '#e0e0e0' }},
                            grid: {{ display: false }}
                        }}
                    }},
                    plugins: {{
                        legend: {{
                            labels: {{ color: '#e0e0e0' }}
                        }}
                    }}
                }}
            }});
        }}

        // ======== Рендер бронирований ========
        function renderBookings(bookings) {{
            const listEl = document.getElementById('bookings-list');
            // Очищаем все, кроме первой строки заголовков (заголовок имеет класс booking-item)
            while (listEl.children.length > 1) {{
                listEl.removeChild(listEl.lastChild);
            }}
            
            if (bookings.length === 0) {{
                listEl.innerHTML += '<li style="text-align: center; color: #999; padding: 20px;">Наразі немає майбутніх бронювань.</li>';
                return;
            }}

            bookings.forEach(b => {{
                const item = document.createElement('li');
                
                let classes = ['booking-item', b.status];
                if (b.is_current) {{
                    classes.push('is_current');
                }}
                item.className = classes.join(' ');
                
                item.innerHTML = `
                    <div class="booking-name">${{b.name}}</div>
                    <div class="booking-time">${{b.time}}</div>
                    <div class="booking-guests">${{b.guests}} чол.</div>
                `;
                listEl.appendChild(item);
            }});
        }}

        // ======== Запросы к API ========
        async function refresh() {{
            const r = await fetch('/api/data');
            const data = await r.json();
            
            // Update Sales Tables
            renderSalesTable(data.hot, data.cold, data.hot_prev, data.cold_prev);
            
            // Update Hourly Chart
            renderHourlyChart(data.hourly, data.hourly_prev);

            // Update Pie Chart
            renderPieChart(data.share);

            // Update time
            const now = new Date();
            document.getElementById('clock').innerText = now.toLocaleTimeString('uk-UA',{hour:'2-digit',minute:'2-digit'});
            
            // Update weather
            const w = data.weather||{{}};
            const iconEl = document.getElementById('weather-icon');
            const tempEl = document.getElementById('weather-temp');
            const descEl = document.getElementById('weather-desc');
            
            if(w.icon) {{
                iconEl.innerHTML = `<img src="https://openweathermap.org/img/wn/${{w.icon}}@2x.png" alt="weather">`;
            }} else {{
                iconEl.innerHTML = '';
            }}
            
            tempEl.textContent = w.temp || '—';
            descEl.textContent = w.desc || '—';
        }}

        async function refreshTables(){{
            const r = await fetch('/api/tables');
            const data = await r.json();
            renderTables('hall', data.hall||[]);
            renderTables('terrace', data.terrace||[]);
        }}

        async function refreshBookings() {{
            const r = await fetch('/api/bookings');
            const data = await r.json();
            renderBookings(data.bookings || []);
        }}

        // Запуск сразу
        refresh(); 
        refreshTables();
        refreshBookings();
        
        // Автообновление
        setInterval(refresh, 60000);
        setInterval(refreshTables, 30000);
        setInterval(refreshBookings, 60000);
        </script>
    </body>
    </html>
    """
    return render_template_string(template)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    # Включаем отладку только при локальном запуске
    debug_mode = os.getenv("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
