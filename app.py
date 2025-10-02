import os
import time
import requests
import sys
from datetime import date, datetime, timedelta
from flask import Flask, render_template_string, jsonify

app = Flask(__name__)

# ==== Конфиг ====
ACCOUNT_NAME = "poka-net3"
POSTER_TOKEN = os.getenv("POSTER_TOKEN")           # обязателен
CHOICE_TOKEN = os.getenv("CHOICE_TOKEN")           # опционален (бронирования)
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
    "hourly": {}, "hourly_prev": {}, "share": {}
}
CACHE_TS = 0

# ===== Helpers =====
def _get(url, **kwargs):
    r = requests.get(url, timeout=kwargs.pop("timeout", 25))
    log_snippet = r.text[:500].replace("\n", " ")
    print(f"DEBUG GET {url.split('?')[0]} -> {r.status_code} : {log_snippet}", file=sys.stderr, flush=True)
    r.raise_for_status()
    return r

# ===== Справочник товаров (минимальный: product_id -> category_id) =====
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

# ===== ДОБАВЛЕНО: полный справочник для Food Cost (product_id -> {cid, cost}) =====
PRODUCT_FULL_CACHE = {}
PRODUCT_FULL_CACHE_TS = 0

def load_products_full():
    """Возвращает { product_id: { 'cid': category_id, 'cost': float } }"""
    global PRODUCT_FULL_CACHE, PRODUCT_FULL_CACHE_TS
    if PRODUCT_FULL_CACHE and time.time() - PRODUCT_FULL_CACHE_TS < 3600:
        return PRODUCT_FULL_CACHE

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
                print("ERROR load_products_full:", e, file=sys.stderr, flush=True)
                break

            if not isinstance(data, list) or not data:
                break

            for item in data:
                try:
                    pid = int(item.get("product_id", 0))
                    cid = int(item.get("menu_category_id", 0))
                    # Poster хранит cost в копейках (как price/profit) — делим на 100.0
                    raw_cost = item.get("cost", 0) or 0
                    cost = float(raw_cost) / 100.0 if float(raw_cost) else 0.0
                    if pid and cid:
                        mapping[pid] = {"cid": cid, "cost": cost}
                except Exception:
                    continue

            if len(data) < per_page:
                break
            page += 1

    PRODUCT_FULL_CACHE = mapping
    PRODUCT_FULL_CACHE_TS = time.time()
    print(f"DEBUG products_full cached: {len(PRODUCT_FULL_CACHE)} items", file=sys.stderr, flush=True)
    return PRODUCT_FULL_CACHE

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
            if status == 2:   # закрытые пропускаем
                continue
            tname = int(trx.get("table_name", 0))
            waiter = trx.get("name", "—")
            active[tname] = waiter
        except Exception:
            continue

    def build(zone_numbers):
        out = []
        for tnum in zone_numbers:
            occupied = tnum in active
            waiter = active.get(tnum, "—")
            out.append({
                "id": tnum,
                "name": f"Стол {tnum}",
                "waiter": waiter,
                "occupied": occupied
            })
        return out

    return {"hall": build(HALL_TABLES), "terrace": build(TERRACE_TABLES)}

# ===== ДОБАВЛЕНО: агрегированный Food Cost (🔥/❄️/🍷 + общий) =====
def fetch_foodcost_summary():
    """
    Считает Food Cost на сегодня по цехам и общий:
    - себестоимость из menu.getProducts (cost) — делим на 100
    - продажи из transactions.getTransactions (product_sum) — делим на 100
    """
    products_full = load_products_full()
    target_date = date.today().strftime("%Y-%m-%d")

    per_page = 500
    page = 1

    sums = {
        "hot":  {"sales": 0.0, "cost": 0.0},
        "cold": {"sales": 0.0, "cost": 0.0},
        "bar":  {"sales": 0.0, "cost": 0.0},
    }

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
            print("ERROR foodcost summary:", e, file=sys.stderr, flush=True)
            break

        if not items:
            break

        for trx in items:
            for p in trx.get("products", []) or []:
                try:
                    pid = int(p.get("product_id", 0))
                    qty = float(p.get("num", 0))
                    # Poster: product_sum в копейках → делим на 100.0
                    sale_sum = float(p.get("product_sum", 0)) / 100.0
                except Exception:
                    continue

                info = products_full.get(pid)
                if not info:
                    continue

                cid = info["cid"]
                unit_cost = float(info["cost"] or 0.0)  # уже в гривнах (делили при загрузке)

                if cid in HOT_CATEGORIES:
                    sums["hot"]["sales"]  += sale_sum
                    sums["hot"]["cost"]   += qty * unit_cost
                elif cid in COLD_CATEGORIES:
                    sums["cold"]["sales"] += sale_sum
                    sums["cold"]["cost"]  += qty * unit_cost
                elif cid in BAR_CATEGORIES:
                    sums["bar"]["sales"]  += sale_sum
                    sums["bar"]["cost"]   += qty * unit_cost

        if per_page_resp * page >= total:
            break
        page += 1

    total_sales = sums["hot"]["sales"] + sums["cold"]["sales"] + sums["bar"]["sales"]
    total_cost  = sums["hot"]["cost"]  + sums["cold"]["cost"]  + sums["bar"]["cost"]

    def pct(sales, cost):
        return round((cost / sales * 100), 1) if sales else 0

    return {
        "hot":   pct(sums["hot"]["sales"], sums["hot"]["cost"]),
        "cold":  pct(sums["cold"]["sales"], sums["cold"]["cost"]),
        "bar":   pct(sums["bar"]["sales"], sums["bar"]["cost"]),
        "total": round((total_cost / total_sales * 100), 1) if total_sales else 0
    }

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

        # добавляем Food Cost
        foodcost_summary = fetch_foodcost_summary()

        CACHE.update({
            "hot": sums_today["hot"], "cold": sums_today["cold"],
            "hot_prev": sums_prev["hot"], "cold_prev": sums_prev["cold"],
            "hourly": hourly, "hourly_prev": prev,
            "share": share, "weather": fetch_weather(),
            "foodcost": foodcost_summary
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
                --accent-danger: #ff453a;
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
                grid-template-rows: minmax(0, 35vh) minmax(0, 58vh);
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
                min-height: 0;
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

            /* Верхний ряд блоков */
            .card.top-card { min-height: 0; }

            /* Таблицы в карточках */
            table {
                width: 100%;
                border-collapse: collapse;
                font-size: 13px;
            }

            th, td {
                padding: 5px 7px;
                text-align: right;
                border-bottom: 1px solid var(--border-color);
            }

            th:first-child, td:first-child { text-align: left; }

            th {
                color: var(--text-secondary);
                font-weight: 600;
                font-size: 11px;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }

            td { color: var(--text-primary); font-weight: 600; font-size: 13px; }

            /* Блок с распределением заказов */
            .pie-container {
                flex: 1;
                display: flex;
                align-items: center;
                justify-content: center;
                min-height: 0;
                position: relative;
                padding: 5px;
            }

            /* Время и погода */
            .time-weather {
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                text-align: center;
                flex: 1;
                padding: 5px;
                height: 100%;
                min-height: 0;
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

            .weather img { width: 100px; height: 100px; margin-bottom: 2px; }
            .temp { font-size: 36px; font-weight: 800; color: var(--text-primary); line-height: 1; }
            .desc { font-size: 15px; color: var(--text-secondary); text-align: center; font-weight: 600; }

            /* График заказов */
            .chart-card {
                grid-column: 1 / 3;
                display: flex;
                flex-direction: column;
                min-height: 0;
            }

            .chart-container {
                flex: 1;
                min-height: 0;
                position: relative;
            }

            /* Плашка Food Cost в блоке графика - ИСПРАВЛЕНО */
            .fc-inline { margin: -2px 0 6px 0; }
            .fc-inline table { width: 100%; }
            .fc-inline th {
                font-size: 11px;
                color: var(--text-secondary);
                text-transform: uppercase;
                letter-spacing: 0.5px;
                text-align: center;
                border-bottom: 1px solid var(--border-color);
                padding-bottom: 4px;
            }
            .fc-inline td {
                text-align: center;
                font-weight: 800;
                font-size: 16px;
                padding: 6px 0;
                color: var(--text-primary); /* ИЗМЕНЕНО: основной цвет текста */
            }
            .fc-val.good { color: var(--accent-success); }
            .fc-val.bad  { color: var(--accent-danger); }

            /* Столы - ИСПРАВЛЕНО: убрана прокрутка, адаптивная сетка */
            .tables-card {
                grid-column: 3 / 5;
                display: flex;
                flex-direction: column;
                min-height: 0;
            }
            .tables-content {
                flex: 1;
                display: flex;
                flex-direction: column;
                gap: 8px;
                min-height: 0;
                overflow: hidden;
            }
            .tables-zone {
                flex: 1;
                min-height: 0;
                display: flex;
                flex-direction: column;
                overflow: hidden;
            }
            .tables-zone h3 {
                font-size: 12px;
                font-weight: 600;
                margin-bottom: 6px;
                color: var(--text-secondary);
                display: flex;
                align-items: center;
                gap: 4px;
                flex-shrink: 0;
            }
            .tables-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(80px, 1fr));
                gap: 6px;
                height: 100%;
                align-content: start;
                overflow: hidden;
            }
            .table-tile {
                border-radius: 10px;
                padding: 8px 6px;
                font-weight: 700;
                text-align: center;
                font-size: 14px;
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                gap: 4px;
                transition: all 0.2s ease;
                border: 1px solid var(--border-color);
                background: var(--bg-tertiary);
                width: 100%;
                aspect-ratio: 1;
                color: var(--text-secondary);
            }
            .table-tile.occupied {
                background: linear-gradient(135deg, var(--accent-cold), #005ecb);
                color: white;
                border-color: var(--accent-cold);
                box-shadow: 0 2px 8px rgba(0, 122, 255, 0.3);
            }
            .table-number { 
                font-weight: 800; 
                font-size: 16px; 
                line-height: 1;
            }
            .table-waiter { 
                font-size: 11px; 
                font-weight: 600; 
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
            canvas { max-width: 100% !important; max-height: 100% !important; }

            /* Responsive adjustments */
            @media (max-height: 800px) {
                body { padding: 6px; }
                .dashboard { gap: 6px; grid-template-rows: minmax(0, 33vh) minmax(0, 60vh); }
                .card { padding: 8px; }
                .card h2 { font-size: 12px; margin-bottom: 6px; }
                .clock { font-size: 56px; }
                .weather img { width: 85px; height: 85px; }
                .temp { font-size: 30px; }
                table { font-size: 12px; }
                th { font-size: 10px; }
                td { font-size: 12px; }
                .tables-grid { grid-template-columns: repeat(auto-fit, minmax(70px, 1fr)); gap: 5px; }
                .table-number { font-size: 14px; }
                .table-waiter { font-size: 10px; }
            }

            @media (max-width: 1200px) {
                .tables-grid { grid-template-columns: repeat(auto-fit, minmax(75px, 1fr)); }
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

            <!-- Нижний ряд -->
            <div class="card chart-card">
                <h2>📈 Замовлення по годинам (накопич.)</h2>

                <!-- Food Cost в блоке графика -->
                <div class="fc-inline">
                    <table id="fc-inline"></table>
                </div>

                <div class="chart-container">
                    <canvas id="chart"></canvas>
                </div>
            </div>

            <div class="card tables-card">
                <h2>🍽️ Столи</h2>
                <div class="tables-content">
                    <div class="tables-zone">
                        <h3>🏛️ Зал</h3>
                        <div id="hall" class="tables-grid"></div>
                    </div>
                    <div class="tables-zone">
                        <h3>🌿 Літня тераса</h3>
                        <div id="terrace" class="tables-grid"></div>
                    </div>
                </div>
            </div>
        </div>

        <div class="logo">GRECO Tech ™</div>

        <script>
        let chart, pie;

        function cutToNow(labels, arr){
            const now = new Date();
            const curHour = now.getHours();
            let cutIndex = labels.findIndex(l => parseInt(l) > curHour);
            if(cutIndex === -1) cutIndex = labels.length;
            return arr.slice(0, cutIndex);
        }

        function renderTables(zoneId, data){
            const el = document.getElementById(zoneId);
            el.innerHTML = "";
            data.forEach(t=>{
                const div = document.createElement("div");
                div.className = "table-tile " + (t.occupied ? "occupied":"");
                div.innerHTML = `
                    <div class="table-number">${t.name}</div>
                    <div class="table-waiter">${t.waiter}</div>
                `;
                el.appendChild(div);
            });
        }

        // Форматирование FC: если >= 30% - красная стрелка вверх, если < 30% - зеленая вниз
        function fcCell(value){
            const v = parseFloat(value || 0);
            const displayValue = v.toFixed(1);
            const isGood = v < 30;
            const arrow = isGood ? '▼' : '▲';
            const cls = isGood ? 'good' : 'bad';
            return '<span class="fc-val ' + cls + '">' + arrow + ' ' + displayValue + '%</span>';
        }

        async function refresh(){
            const r = await fetch('/api/sales');
            const data = await r.json();

            function fill(id, today, prev){
                const el = document.getElementById(id);
                let html = "<tr><th>Категорії</th><th>Сьогодні</th><th>Мин. тиждень</th></tr>";
                const keys = new Set([...Object.keys(today || {}), ...Object.keys(prev || {})]);
                [...keys].sort().forEach(k => {
                    html += `<tr><td>${k}</td><td>${(today||{})[k]||0}</td><td>${(prev||{})[k]||0}</td></tr>`;
                });
                el.innerHTML = html;
            }
            fill('hot_tbl', data.hot||{}, data.hot_prev||{});
            fill('cold_tbl', data.cold||{}, data.cold_prev||{});

            // ---- PIE ----
            Chart.register(ChartDataLabels);
            const ctx2 = document.getElementById('pie').getContext('2d');
            if(pie) pie.destroy();
            pie = new Chart(ctx2,{
                type:'pie',
                data:{
                    labels:['Гар.цех','Хол.цех','Бар'],
                    datasets:[{
                        data:[data.share.hot,data.share.cold,data.share.bar],
                        backgroundColor:['#ff9500','#007aff','#af52de'],
                        borderWidth: 2,
                        borderColor: '#000'
                    }]
                },
                options:{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins:{
                        legend:{display:false},
                        tooltip:{enabled:false},
                        datalabels:{
                            color:'#fff',
                            font:{weight:'bold', size:11, family:'Inter'},
                            formatter:function(value, context){
                                const label = context.chart.data.labels[context.dataIndex];
                                return label + '\\n' + value + '%';
                            },
                            textAlign: 'center'
                        }
                    }
                }
            });

            // ---- LINE CHART ----
            let today_hot = cutToNow(data.hourly.labels, data.hourly.hot);
            let today_cold = cutToNow(data.hourly.labels, data.hourly.cold);

            const ctx = document.getElementById('chart').getContext('2d');
            if(chart) chart.destroy();
            chart = new Chart(ctx,{
                type:'line',
                data:{
                    labels:data.hourly.labels,
                    datasets:[
                        {
                            label:'Гарячий',
                            data:today_hot,
                            borderColor:'#ff9500',
                            backgroundColor:'rgba(255, 149, 0, 0.1)',
                            tension:0.4,
                            fill:false,
                            borderWidth: 2,
                            pointRadius: 3,
                            pointBackgroundColor: '#ff9500'
                        },
                        {
                            label:'Холодний',
                            data:today_cold,
                            borderColor:'#007aff',
                            backgroundColor:'rgba(0, 122, 255, 0.1)',
                            tension:0.4,
                            fill:false,
                            borderWidth: 2,
                            pointRadius: 3,
                            pointBackgroundColor: '#007aff'
                        },
                        {
                            label:'Гарячий (мин. тиждн.)',
                            data:data.hourly_prev.hot,
                            borderColor:'rgba(255, 149, 0, 0.5)',
                            borderDash:[6,4],
                            tension:0.4,
                            fill:false,
                            borderWidth: 1,
                            pointRadius: 2
                        },
                        {
                            label:'Холодний (мин. тиждн.)',
                            data:data.hourly_prev.cold,
                            borderColor:'rgba(0, 122, 255, 0.5)',
                            borderDash:[6,4],
                            tension:0.4,
                            fill:false,
                            borderWidth: 1,
                            pointRadius: 2
                        }
                    ]
                },
                options:{
                    responsive:true,
                    maintainAspectRatio: false,
                    interaction: { intersect: false, mode: 'index' },
                    plugins:{
                        legend:{
                            labels:{
                                color:'#8e8e93',
                                font: { size: 9 },
                                usePointStyle: true,
                                pointStyle: 'circle'
                            }
                        },
                        datalabels:{display:false}
                    },
                    scales:{
                        x:{
                            ticks:{color:'#8e8e93', font: { size: 9 }},
                            grid:{color:'rgba(142, 142, 147, 0.2)'},
                            border:{color:'#38383a'}
                        },
                        y:{
                            ticks:{color:'#8e8e93', font: { size: 9 }},
                            grid:{color:'rgba(142, 142, 147, 0.2)'},
                            border:{color:'#38383a'},
                            beginAtZero:true
                        }
                    }
                }
            });

            // ---- FOOD COST INLINE (в блоке графика) - ИСПРАВЛЕНО ----
            const fc = data.foodcost || {};
            const fcEl = document.getElementById('fc-inline');
            
            fcEl.innerHTML = `
                <tr>
                    <th>🔥 Гарячий</th>
                    <th>❄️ Холодний</th>
                    <th>🍷 Бар</th>
                    <th>📊 Всього</th>
                </tr>
                <tr>
                    <td>${fcCell(fc.hot)}</td>
                    <td>${fcCell(fc.cold)}</td>
                    <td>${fcCell(fc.bar)}</td>
                    <td>${fcCell(fc.total)}</td>
                </tr>
            `;

            // Update time
            const now = new Date();
            document.getElementById('clock').innerText = now.toLocaleTimeString('uk-UA',{hour:'2-digit',minute:'2-digit'});
            
            // Update weather
            const w = data.weather||{};
            const iconEl = document.getElementById('weather-icon');
            const tempEl = document.getElementById('weather-temp');
            const descEl = document.getElementById('weather-desc');
            
            if(w.icon) {
                iconEl.innerHTML = `<img src="https://openweathermap.org/img/wn/${w.icon}@2x.png" alt="weather">`;
            } else {
                iconEl.innerHTML = '';
            }
            
            tempEl.textContent = w.temp || '—';
            descEl.textContent = w.desc || '—';
        }

        async function refreshTables(){
            const r = await fetch('/api/tables');
            const data = await r.json();
            renderTables('hall', data.hall||[]);
            renderTables('terrace', data.terrace||[]);
        }

        // Запуск сразу
        refresh(); 
        refreshTables();

        // Автообновление
        setInterval(refresh, 60000);
        setInterval(refreshTables, 30000);
        </script>
    </body>
    </html>
    """
    return render_template_string(template)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
