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
CHOICE_TOKEN = os.getenv("CHOICE_TOKEN")           # брони Choice
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

BOOKING_STATUS_MAP = {
    "CREATED": "Очікує підтвердження",
    "CONFIRMED": "Підтверджено",
    "EXTERNAL_CANCELLING": "Скасування (зовнішнє)",
    "CANCELLED": "Скасовано",
    "IN_PROGRESS": "У закладі",
    "NOT_CAME": "Не зʼявився",
    "COMPLETED": "Завершено"
}

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

# ===== Брони Choice =====
def fetch_reservations():
    if not CHOICE_TOKEN:
        return []

    today = date.today()
    start = datetime.combine(today, datetime.min.time()).isoformat() + "Z"
    end = datetime.combine(today, datetime.max.time()).isoformat() + "Z"

    url = "https://api.choice.ua/bookings/list"
    params = {
        "from": start,
        "till": end,
        "periodField": "bookingDt",
        "page": 1,
        "perPage": 100
    }

    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {CHOICE_TOKEN}"},
            params=params,
            timeout=15
        )
        data = resp.json()
        reservations = []
        for r in data:
            try:
                dt = r.get("dateTime")
                time_str = "—"
                if dt:
                    try:
                        dt_parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
                        time_str = dt_parsed.strftime("%H:%M")
                    except Exception:
                        time_str = dt[:16]

                status_raw = r.get("status", "—")
                status_display = BOOKING_STATUS_MAP.get(status_raw, status_raw)

                reservations.append({
                    "num": r.get("num"),
                    "time": time_str,
                    "name": r.get("customer", {}).get("name", "—"),
                    "people": r.get("personCount", 0),
                    "comment": r.get("comment", "") or r.get("note", ""),
                    "waiter": r.get("user", {}).get("name", "—"),
                    "status": status_display,
                    "deposit": r.get("deposit", {}).get("amount", 0),
                    # предположим, что table id может быть в locationPoints[0]
                    "table": (r.get("locationPoints") or [None])[0]
                })
            except Exception:
                continue
        return reservations
    except Exception as e:
        print("ERROR reservations:", e, file=sys.stderr, flush=True)
        return []

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

# ===== Объединяем столы + брони =====
def merge_tables_and_reservations():
    tables = fetch_tables_with_waiters()
    reservations = fetch_reservations()
    res_map = {}
    for r in reservations:
        if r.get("table"):
            res_map[str(r["table"])] = r

    for zone in ("hall", "terrace"):
        for t in tables[zone]:
            rid = str(t["id"])
            if rid in res_map:
                t["reservation"] = {
                    "name": res_map[rid]["name"],
                    "people": res_map[rid]["people"]
                }
            else:
                t["reservation"] = None
    return tables

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
            "share": share, "weather": fetch_weather()
        })
        CACHE_TS = time.time()

    return jsonify(CACHE)

@app.route("/api/tables")
def api_tables():
    return jsonify(merge_tables_and_reservations())

@app.route("/api/reservations")
def api_reservations():
    return jsonify(fetch_reservations())

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
            /* ваш CSS остается, добавляем только table-resv */
            .table-resv {
                font-size: 13px;
                font-weight: 600;
                color: #ffcc00;
                margin-top: 4px;
                white-space: nowrap;
                text-overflow: ellipsis;
                overflow: hidden;
            }
        </style>
    </head>
    <body>
        <div class="dashboard">
            <!-- здесь ваш текущий layout -->
            <!-- + блок броней -->
            <div class="card top-card">
              <h2>📅 Броні сьогодні</h2>
              <div style="flex: 1; overflow: auto;">
                <table id="resv_tbl"></table>
              </div>
            </div>
        </div>

        <script>
        // ваш код refresh + добавляем бронь
        async function refreshReservations(){
            const r = await fetch('/api/reservations');
            const data = await r.json();
            const el = document.getElementById('resv_tbl');
            let html = "<tr><th>№</th><th>Час</th><th>Імʼя</th><th>Гостей</th><th>Офіціант</th><th>Статус</th><th>Комент</th><th>Депозит</th></tr>";
            data.forEach(b => {
                html += `<tr>
                    <td>${b.num || '—'}</td>
                    <td>${b.time}</td>
                    <td>${b.name}</td>
                    <td>${b.people}</td>
                    <td>${b.waiter}</td>
                    <td>${b.status}</td>
                    <td>${b.comment || ''}</td>
                    <td>${b.deposit ? b.deposit + ' ₴' : ''}</td>
                </tr>`;
            });
            el.innerHTML = html;
        }

        function renderTables(zoneId, data){
            const el = document.getElementById(zoneId);
            el.innerHTML = "";
            data.forEach(t=>{
                const div = document.createElement("div");
                div.className = "table-tile " + (t.occupied ? "occupied":"free");
                div.innerHTML = `
                    <div class="table-number">${t.name}</div>
                    <div class="table-waiter">${t.waiter}</div>
                    ${t.reservation ? 
                        `<div class="table-resv">${t.reservation.name} (${t.reservation.people})</div>` 
                        : ""}
                `;
                el.appendChild(div);
            });
        }

        refreshReservations();
        setInterval(refreshReservations, 30000);
        </script>
    </body>
    </html>
    """
    return render_template_string(template)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
