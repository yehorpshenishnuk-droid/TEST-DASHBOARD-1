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
                    cost = float(item.get("cost", 0))
                    cname = item.get("category_name", "").strip()
                    if pid and cid:
                        mapping[pid] = {"cid": cid, "cost": cost, "cname": cname}
                except Exception:
                    continue

            if len(data) < per_page:
                break
            page += 1

    PRODUCT_CACHE = mapping
    PRODUCT_CACHE_TS = time.time()
    print(f"DEBUG products cached: {len(PRODUCT_CACHE)} items", file=sys.stderr, flush=True)
    return PRODUCT_CACHE

# ===== FoodCost по категориям =====
def fetch_foodcost_by_subcategories():
    products = load_products()
    target_date = date.today().strftime("%Y-%m-%d")
    per_page, page = 500, 1
    data = {"hot": {}, "cold": {}}

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
            print("ERROR foodcost:", e, file=sys.stderr, flush=True)
            break

        if not items:
            break

        for trx in items:
            for p in trx.get("products", []) or []:
                try:
                    pid = int(p.get("product_id", 0))
                    qty = float(p.get("num", 0))
                    sale_sum = float(p.get("product_sum", 0))
                except Exception:
                    continue

                info = products.get(pid)
                if not info:
                    continue

                cid = info["cid"]
                cname = info["cname"] or f"Категорія {cid}"
                cost = info["cost"]

                if cid in HOT_CATEGORIES:
                    entry = data["hot"].setdefault(cname, {"sales": 0, "cost": 0})
                    entry["sales"] += sale_sum
                    entry["cost"] += qty * cost
                elif cid in COLD_CATEGORIES:
                    entry = data["cold"].setdefault(cname, {"sales": 0, "cost": 0})
                    entry["sales"] += sale_sum
                    entry["cost"] += qty * cost

        if per_page_resp * page >= total:
            break
        page += 1

    # Рассчитываем %
    for zone in ("hot", "cold"):
        for cname, vals in data[zone].items():
            s, c = vals["sales"], vals["cost"]
            vals["percent"] = round((c / s * 100) if s else 0, 1)

    return data

# ===== Сводные продажи =====
# (остальные функции fetch_category_sales, fetch_transactions_hourly, fetch_weather, fetch_tables_with_waiters — БЕЗ изменений, оставляем как есть)
# ...

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
            "foodcost_categories": fetch_foodcost_by_subcategories()
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
            /* (оставляем все твои стили без изменений) */
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

            <!-- Новый блок Food Cost -->
            <div class="card top-card" style="grid-column: 1 / 3;">
                <h2>💰 Food Cost по цехах</h2>
                <div style="flex: 1; overflow: hidden;">
                    <table id="fc-tbl"></table>
                </div>
            </div>

            <!-- Нижний ряд -->
            <div class="card chart-card">
                <h2>📈 Замовлення по годинам (накопич.)</h2>
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

        function fillFCmerged(id, hot, cold){
            const el = document.getElementById(id);
            let html = "<tr><th>🔥 Гарячий цех</th><th>%</th><th>❄️ Холодний цех</th><th>%</th></tr>";

            const hotKeys = Object.keys(hot);
            const coldKeys = Object.keys(cold);
            const maxLen = Math.max(hotKeys.length, coldKeys.length);

            for (let i = 0; i < maxLen; i++){
                const hk = hotKeys[i] || "";
                const ck = coldKeys[i] || "";
                const hv = hk ? (hot[hk].percent || 0) + "%" : "";
                const cv = ck ? (cold[ck].percent || 0) + "%" : "";
                html += `<tr>
                           <td>${hk}</td><td>${hv}</td>
                           <td>${ck}</td><td>${cv}</td>
                         </tr>`;
            }
            el.innerHTML = html;
        }

        async function refresh(){
            const r = await fetch('/api/sales');
            const data = await r.json();

            // твои таблицы как раньше
            function fill(id, today, prev){
                const el = document.getElementById(id);
                let html = "<tr><th>Категорі</th><th>Сьогодні</th><th>Мин. тиждень</th></tr>";
                const keys = new Set([...Object.keys(today), ...Object.keys(prev)]);
                keys.forEach(k => {
                    html += `<tr><td>${k}</td><td>${today[k]||0}</td><td>${prev[k]||0}</td></tr>`;
                });
                el.innerHTML = html;
            }
            fill('hot_tbl', data.hot||{}, data.hot_prev||{});
            fill('cold_tbl', data.cold||{}, data.cold_prev||{});

            // FoodCost таблица
            fillFCmerged('fc-tbl', data.foodcost_categories.hot || {}, data.foodcost_categories.cold || {});

            // (остальной твой код refresh с графиками, погодой, временем — оставляем без изменений)
        }

        async function refreshTables(){
            const r = await fetch('/api/tables');
            const data = await r.json();
            renderTables('hall', data.hall||[]);
            renderTables('terrace', data.terrace||[]);
        }

        refresh();
        refreshTables();
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
