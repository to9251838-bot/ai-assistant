import os
import io
import json
import base64
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from functools import wraps

import bcrypt
import jwt
import requests
from flask import Flask, request, jsonify, send_from_directory
from pymongo import MongoClient, ASCENDING
from groq import Groq

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=None)

# -----------------------------
# Configuration
# -----------------------------
MONGODB_URI = os.getenv("MONGODB_URI", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")

# Non-secret settings stay in code.
JWT_SECRET = os.getenv("JWT_SECRET") or secrets.token_hex(32)
GROQ_CHAT_MODEL = "openai/gpt-oss-120b"
GROQ_STT_MODEL = "whisper-large-v3"
GROQ_TTS_MODEL = "canopylabs/orpheus-v1-english"
GROQ_TTS_VOICE = "hannah"
CLOUDFLARE_IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

mongo_client = None
db = None
users = None
conversations = None

if MONGODB_URI:
    try:
        mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command("ping")
        db_name = mongo_client.get_default_database()
        if db_name is None:
            db_name = mongo_client["ai_orb"]
        db = db_name
        users = db["users"]
        conversations = db["conversations"]
        users.create_index([("email", ASCENDING)], unique=True)
        conversations.create_index([("user_id", ASCENDING), ("updated_at", -1)])
        print("MongoDB connected.")
    except Exception as exc:
        print(f"MongoDB connection failed: {exc}")
        mongo_client = db = users = conversations = None
else:
    print("MONGODB_URI is not set. Authentication/history will be unavailable.")

# -----------------------------
# Helpers
# -----------------------------
def now():
    return datetime.now(timezone.utc)

def token_for_user(user_id):
    payload = {
        "sub": str(user_id),
        "exp": now() + timedelta(days=30),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def current_user_id():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload["sub"]
    except Exception:
        return None

def auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        uid = current_user_id()
        if not uid:
            return jsonify({"error": "Требуется авторизация"}), 401
        return fn(uid, *args, **kwargs)
    return wrapper

def clean_doc(doc):
    if not doc:
        return None
    doc = dict(doc)
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc

def require_service_config():
    missing = []
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")
    if missing:
        raise RuntimeError("Не настроен: " + ", ".join(missing))

def route_request(text):
    """Ask Groq to classify a request and return strict JSON."""
    require_service_config()
    prompt = f"""
Ты — маршрутизатор русско- и англоязычного AI-ассистента.
Определи намерение пользователя.

Верни ТОЛЬКО JSON без markdown:
{{
  "intent": "chat" | "weather" | "image",
  "language": "ru" | "en",
  "reply": "короткий естественный ответ, если intent=chat",
  "city": "город или пустая строка, если weather",
  "image_prompt": "подробное описание изображения НА АНГЛИЙСКОМ, если image"
}}

Правила:
- Погода, температура, прогноз/состояние погоды -> weather.
- Просьба нарисовать, создать, сгенерировать изображение -> image.
- Всё остальное -> chat.
- Для image обязательно переведи и уточни описание на английском.
- Для weather извлеки название города.
- Не придумывай город, если его нет.

Текст пользователя:
{text}
"""
    response = groq_client.chat.completions.create(
        model=GROQ_CHAT_MODEL,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "Ты точный JSON router."},
            {"role": "user", "content": prompt},
        ],
    )
    raw = response.choices[0].message.content
    return json.loads(raw)

def chat_response(text):
    response = groq_client.chat.completions.create(
        model=GROQ_CHAT_MODEL,
        temperature=0.7,
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты полезный голосовой AI-ассистент. Отвечай естественно и "
                    "кратко, если вопрос простой. Отвечай на языке пользователя."
                ),
            },
            {"role": "user", "content": text},
        ],
    )
    return response.choices[0].message.content.strip()

def weather_response(city):
    if not OPENWEATHER_API_KEY:
        raise RuntimeError("Не настроен OPENWEATHER_API_KEY")

    geo = requests.get(
        "https://api.openweathermap.org/geo/1.0/direct",
        params={"q": city, "limit": 1, "appid": OPENWEATHER_API_KEY},
        timeout=12,
    )
    geo.raise_for_status()
    locations = geo.json()
    if not locations:
        return {"text": f"Я не нашёл город «{city}».", "data": None}

    loc = locations[0]
    weather = requests.get(
        "https://api.openweathermap.org/data/2.5/weather",
        params={
            "lat": loc["lat"],
            "lon": loc["lon"],
            "appid": OPENWEATHER_API_KEY,
            "units": "metric",
            "lang": "ru",
        },
        timeout=12,
    )
    weather.raise_for_status()
    data = weather.json()

    temp = round(data["main"]["temp"])
    feels = round(data["main"]["feels_like"])
    description = data["weather"][0]["description"]
    humidity = data["main"]["humidity"]

    text = (
        f"Сейчас в {loc.get('name', city)}: {temp}°C, {description}. "
        f"Ощущается как {feels}°C, влажность {humidity}%."
    )
    return {
        "text": text,
        "data": {
            "city": loc.get("name", city),
            "country": loc.get("country", ""),
            "temperature": temp,
            "feels_like": feels,
            "description": description,
            "humidity": humidity,
        },
    }


def geocode_locations(query, limit=5):
    if not OPENWEATHER_API_KEY:
        raise RuntimeError("Не настроен OPENWEATHER_API_KEY")
    q = (query or "").strip()
    if not q:
        return []
    response = requests.get(
        "https://api.openweathermap.org/geo/1.0/direct",
        params={"q": q, "limit": min(max(int(limit), 1), 5), "appid": OPENWEATHER_API_KEY},
        timeout=12,
    )
    response.raise_for_status()
    return response.json()

def reverse_geocode(lat, lon, limit=5):
    if not OPENWEATHER_API_KEY:
        raise RuntimeError("Не настроен OPENWEATHER_API_KEY")
    response = requests.get(
        "https://api.openweathermap.org/geo/1.0/reverse",
        params={"lat": lat, "lon": lon, "limit": min(max(int(limit), 1), 5), "appid": OPENWEATHER_API_KEY},
        timeout=12,
    )
    response.raise_for_status()
    return response.json()

def location_label(loc):
    parts = [loc.get("name", "")]
    state = loc.get("state")
    country = loc.get("country")
    if state:
        parts.append(state)
    if country:
        parts.append(country)
    return ", ".join([p for p in parts if p])

def weather_bundle(lat, lon, lang="ru"):
    if not OPENWEATHER_API_KEY:
        raise RuntimeError("Не настроен OPENWEATHER_API_KEY")

    current_response = requests.get(
        "https://api.openweathermap.org/data/2.5/weather",
        params={
            "lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY,
            "units": "metric", "lang": lang if lang in {"ru", "en"} else "ru",
        },
        timeout=12,
    )
    current_response.raise_for_status()
    current = current_response.json()

    forecast_response = requests.get(
        "https://api.openweathermap.org/data/2.5/forecast",
        params={
            "lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY,
            "units": "metric", "lang": lang if lang in {"ru", "en"} else "ru",
        },
        timeout=12,
    )
    forecast_response.raise_for_status()
    forecast = forecast_response.json()

    city = current.get("name") or "Точка"
    country = current.get("sys", {}).get("country", "")
    timezone_offset = current.get("timezone", 0)
    now_local = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + timezone_offset, tz=timezone.utc)
    sunrise = current.get("sys", {}).get("sunrise")
    sunset = current.get("sys", {}).get("sunset")
    now_utc_ts = datetime.now(timezone.utc).timestamp()
    is_day = bool(sunrise and sunset and sunrise <= now_utc_ts <= sunset)

    daily = {}
    for item in forecast.get("list", []):
        dt = item.get("dt", 0) + timezone_offset
        day = datetime.fromtimestamp(dt, tz=timezone.utc).date().isoformat()
        entry = {
            "dt": item.get("dt"),
            "time": datetime.fromtimestamp(dt, tz=timezone.utc).strftime("%H:%M"),
            "temp": round(item.get("main", {}).get("temp", 0)),
            "feels_like": round(item.get("main", {}).get("feels_like", 0)),
            "description": item.get("weather", [{}])[0].get("description", ""),
            "main": item.get("weather", [{}])[0].get("main", "Clear"),
            "icon": item.get("weather", [{}])[0].get("icon", "01d"),
            "humidity": item.get("main", {}).get("humidity", 0),
            "wind": item.get("wind", {}).get("speed", 0),
            "pop": round((item.get("pop", 0) or 0) * 100),
        }
        daily.setdefault(day, []).append(entry)

    days = []
    for day, items in list(daily.items())[:5]:
        temps = [x["temp"] for x in items]
        midday = min(items, key=lambda x: abs(int(x["time"].split(":")[0]) - 13))
        days.append({
            "date": day,
            "min": min(temps),
            "max": max(temps),
            "description": midday["description"],
            "main": midday["main"],
            "icon": midday["icon"],
            "pop": max(x["pop"] for x in items),
            "humidity": round(sum(x["humidity"] for x in items) / len(items)),
        })

    return {
        "location": {
            "name": city,
            "country": country,
            "label": ", ".join([x for x in [city, country] if x]),
            "lat": float(current.get("coord", {}).get("lat", lat)),
            "lon": float(current.get("coord", {}).get("lon", lon)),
        },
        "current": {
            "temp": round(current.get("main", {}).get("temp", 0)),
            "feels_like": round(current.get("main", {}).get("feels_like", 0)),
            "description": current.get("weather", [{}])[0].get("description", ""),
            "main": current.get("weather", [{}])[0].get("main", "Clear"),
            "icon": current.get("weather", [{}])[0].get("icon", "01d"),
            "humidity": current.get("main", {}).get("humidity", 0),
            "pressure": current.get("main", {}).get("pressure", 0),
            "wind": round(current.get("wind", {}).get("speed", 0), 1),
            "visibility": round((current.get("visibility", 0) or 0) / 1000, 1),
            "sunrise": sunrise,
            "sunset": sunset,
            "is_day": is_day,
        },
        "timezone_offset": timezone_offset,
        "local_time": now_local.strftime("%H:%M"),
        "forecast": days,
        "updated_at": now().isoformat(),
    }

def parse_weather_location_query(text):
    require_service_config()
    prompt = f"""
Определи местоположение из запроса пользователя для приложения погоды.
Верни ТОЛЬКО JSON:
{{
  "mode": "place" | "coordinates",
  "query": "название места для поиска или пустая строка",
  "lat": number | null,
  "lon": number | null
}}
Правила:
- Если пользователь назвал город, регион, страну, аэропорт или другое место — mode=place.
- Если явно дал координаты — mode=coordinates и извлеки lat/lon.
- Не придумывай координаты.
- Удали фразы вроде «покажи погоду в» и оставь только название места.
Запрос: {text}
"""
    response = groq_client.chat.completions.create(
        model=GROQ_CHAT_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": "Ты точный географический JSON parser."}, {"role": "user", "content": prompt}],
    )
    return json.loads(response.choices[0].message.content)

def resolve_weather_query(text, lang="ru"):
    parsed = parse_weather_location_query(text)
    if parsed.get("mode") == "coordinates":
        try:
            lat = float(parsed.get("lat"))
            lon = float(parsed.get("lon"))
        except (TypeError, ValueError):
            raise ValueError("Не удалось распознать координаты.")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Координаты вне допустимого диапазона.")
        places = reverse_geocode(lat, lon, 5)
        if places:
            loc = places[0]
            label = location_label(loc)
        else:
            label = f"{lat:.4f}, {lon:.4f}"
        return {"places": [{"name": label, "lat": lat, "lon": lon, "country": places[0].get("country", "") if places else ""}], "auto": True}

    query = (parsed.get("query") or "").strip()
    places = geocode_locations(query, 5)
    return {
        "places": [
            {"name": location_label(loc), "city": loc.get("name", ""), "state": loc.get("state", ""), "country": loc.get("country", ""), "lat": loc.get("lat"), "lon": loc.get("lon")}
            for loc in places
        ],
        "auto": len(places) == 1,
    }

def generate_image(prompt):
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        raise RuntimeError(
            "Не настроены CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN"
        )

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{CLOUDFLARE_ACCOUNT_ID}/ai/run/{CLOUDFLARE_IMAGE_MODEL}"
    )
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"prompt": prompt},
        timeout=90,
    )
    response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = response.json()
        result = payload.get("result", payload)
        image_b64 = result.get("image") if isinstance(result, dict) else None
        if not image_b64:
            raise RuntimeError("Cloudflare не вернул изображение.")
    else:
        image_b64 = base64.b64encode(response.content).decode("ascii")

    return f"data:image/png;base64,{image_b64}"

def english_tts(text):
    """Return a data URL for Groq TTS. Groq's documented voice is English."""
    if not groq_client:
        return None
    try:
        result = groq_client.audio.speech.create(
            model=GROQ_TTS_MODEL,
            voice=GROQ_TTS_VOICE,
            input=text[:3000],
            response_format="wav",
        )
        audio_bytes = result.read()
        encoded = base64.b64encode(audio_bytes).decode("ascii")
        return f"data:audio/wav;base64,{encoded}"
    except Exception as exc:
        print(f"TTS unavailable: {exc}")
        return None

def process_text(text, language_hint=None):
    text = (text or "").strip()
    if not text:
        raise ValueError("Пустой запрос.")

    routed = route_request(text)
    intent = routed.get("intent", "chat")
    language = routed.get("language") or language_hint or "ru"

    if intent == "weather":
        city = (routed.get("city") or "").strip()
        if not city:
            answer = "Назови город, для которого проверить погоду."
            return {"text": answer, "intent": intent, "audio": english_tts(answer) if language == "en" else None}
        result = weather_response(city)
        answer = result["text"]
        return {
            "text": answer,
            "intent": intent,
            "weather": result["data"],
            "audio": english_tts(answer) if language == "en" else None,
        }

    if intent == "image":
        prompt = (routed.get("image_prompt") or text).strip()
        image = generate_image(prompt)
        narration = (
            "Я создал изображение по твоему описанию."
            if language != "en"
            else "I created the image from your description."
        )
        return {
            "text": narration,
            "intent": intent,
            "image": image,
            "audio": english_tts(narration) if language == "en" else None,
        }

    answer = chat_response(text)
    return {
        "text": answer,
        "intent": "chat",
        "audio": english_tts(answer) if language == "en" else None,
    }

def save_exchange(user_id, user_text, result):
    if conversations is None:
        return

    timestamp = now()
    message_user = {
        "role": "user",
        "content": user_text,
        "created_at": timestamp,
    }
    message_assistant = {
        "role": "assistant",
        "content": result.get("text", ""),
        "intent": result.get("intent", "chat"),
        "image": result.get("image"),
        "weather": result.get("weather"),
        "created_at": timestamp,
    }

    conversations.update_one(
        {"user_id": user_id},
        {
            "$push": {
                "messages": {
                    "$each": [message_user, message_assistant],
                    "$slice": -100,
                }
            },
            "$set": {"updated_at": timestamp},
            "$setOnInsert": {"user_id": user_id, "created_at": timestamp},
        },
        upsert=True,
    )

# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.get("/app.js")
def javascript():
    return send_from_directory(BASE_DIR, "app.js", mimetype="application/javascript")

@app.get("/style.css")
def stylesheet():
    return send_from_directory(BASE_DIR, "style.css", mimetype="text/css")

@app.get("/weather")
def weather_page():
    return send_from_directory(BASE_DIR, "weather.html")

@app.get("/api/weather/search")
def weather_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"places": []})
    try:
        if "," in query:
            parts = [p.strip() for p in query.split(",")]
            if len(parts) >= 2:
                try:
                    lat, lon = float(parts[0]), float(parts[1])
                    if -90 <= lat <= 90 and -180 <= lon <= 180:
                        places = reverse_geocode(lat, lon, 5)
                        return jsonify({"places": [{"name": location_label(x), "city": x.get("name", ""), "state": x.get("state", ""), "country": x.get("country", ""), "lat": x.get("lat", lat), "lon": x.get("lon", lon)} for x in places] or [{"name": f"{lat:.5f}, {lon:.5f}", "lat": lat, "lon": lon, "country": ""}]})
                except ValueError:
                    pass
        places = geocode_locations(query, 5)
        return jsonify({"places": [{"name": location_label(x), "city": x.get("name", ""), "state": x.get("state", ""), "country": x.get("country", ""), "lat": x.get("lat"), "lon": x.get("lon")} for x in places]})
    except Exception as exc:
        print(f"/api/weather/search error: {exc}")
        return jsonify({"error": str(exc)}), 500

@app.get("/api/weather")
def weather_api():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
        lang = request.args.get("lang", "ru")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Некорректные координаты.")
        return jsonify(weather_bundle(lat, lon, lang))
    except Exception as exc:
        print(f"/api/weather error: {exc}")
        return jsonify({"error": str(exc)}), 500

@app.post("/api/weather/ai")
def weather_ai():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    lang = body.get("language") or "ru"
    if not text:
        return jsonify({"error": "Пустой запрос."}), 400
    try:
        result = resolve_weather_query(text, lang)
        if result["auto"] and result["places"]:
            p = result["places"][0]
            result["weather"] = weather_bundle(p["lat"], p["lon"], lang)
        return jsonify(result)
    except Exception as exc:
        print(f"/api/weather/ai error: {exc}")
        return jsonify({"error": str(exc)}), 500

@app.post("/api/weather/voice")
def weather_voice():
    if "audio" not in request.files:
        return jsonify({"error": "Аудиофайл не найден."}), 400
    try:
        require_service_config()
        language_hint = request.form.get("language") or "ru"
        audio = request.files["audio"]
        transcription = groq_client.audio.transcriptions.create(
            file=("weather.webm", audio.read(), "audio/webm"),
            model=GROQ_STT_MODEL,
            response_format="json",
        )
        text = (transcription.text or "").strip()
        if not text:
            return jsonify({"error": "Не удалось распознать речь."}), 400
        result = resolve_weather_query(text, language_hint)
        if result["auto"] and result["places"]:
            p = result["places"][0]
            result["weather"] = weather_bundle(p["lat"], p["lon"], language_hint)
        result["transcript"] = text
        return jsonify(result)
    except Exception as exc:
        print(f"/api/weather/voice error: {exc}")
        return jsonify({"error": str(exc)}), 500

@app.get("/api/weather/saved")
@auth_required
def weather_saved(user_id):
    if db is None:
        return jsonify({"locations": []})
    docs = list(db["weather_locations"].find({"user_id": user_id}).sort("created_at", ASCENDING))
    return jsonify({"locations": [clean_doc(x) for x in docs]})

@app.post("/api/weather/saved")
@auth_required
def weather_saved_add(user_id):
    if db is None:
        return jsonify({"error": "MongoDB недоступна."}), 503
    body = request.get_json(silent=True) or {}
    try:
        lat, lon = float(body.get("lat")), float(body.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "Некорректные координаты."}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "Некорректные координаты."}), 400
    doc = {
        "user_id": user_id,
        "name": (body.get("name") or "Точка").strip(),
        "country": (body.get("country") or "").strip(),
        "lat": lat,
        "lon": lon,
        "created_at": now(),
    }
    existing = db["weather_locations"].find_one({"user_id": user_id, "lat": lat, "lon": lon})
    if existing:
        db["weather_locations"].update_one({"_id": existing["_id"]}, {"$set": {"name": doc["name"], "country": doc["country"]}})
        doc["_id"] = existing["_id"]
    else:
        doc["_id"] = db["weather_locations"].insert_one(doc).inserted_id
    return jsonify({"location": clean_doc(doc)})

@app.delete("/api/weather/saved/<path:location_id>")
@auth_required
def weather_saved_delete(user_id, location_id):
    if db is None:
        return jsonify({"error": "MongoDB недоступна."}), 503
    try:
        from bson import ObjectId
        result = db["weather_locations"].delete_one({"_id": ObjectId(location_id), "user_id": user_id})
        return jsonify({"ok": result.deleted_count == 1})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "mongodb": bool(db is not None),
        "groq": bool(GROQ_API_KEY),
        "openweather": bool(OPENWEATHER_API_KEY),
        "cloudflare": bool(CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN),
    })

@app.post("/api/auth/register")
def register():
    if users is None:
        return jsonify({"error": "MongoDB не настроена или недоступна."}), 503

    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    if len(email) < 5 or "@" not in email:
        return jsonify({"error": "Укажи корректный email."}), 400
    if len(password) < 6:
        return jsonify({"error": "Пароль должен содержать минимум 6 символов."}), 400

    if users.find_one({"email": email}):
        return jsonify({"error": "Пользователь с таким email уже существует."}), 409

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    result = users.insert_one({
        "email": email,
        "password_hash": password_hash,
        "created_at": now(),
    })
    return jsonify({
        "token": token_for_user(result.inserted_id),
        "email": email,
    }), 201

@app.post("/api/auth/login")
def login():
    if users is None:
        return jsonify({"error": "MongoDB не настроена или недоступна."}), 503

    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    user = users.find_one({"email": email})
    if not user or not bcrypt.checkpw(
        password.encode(), user["password_hash"].encode()
    ):
        return jsonify({"error": "Неверный email или пароль."}), 401

    return jsonify({
        "token": token_for_user(user["_id"]),
        "email": email,
    })

@app.get("/api/conversation")
@auth_required
def get_conversation(user_id):
    if conversations is None:
        return jsonify({"messages": []})
    doc = conversations.find_one({"user_id": user_id})
    if not doc:
        return jsonify({"messages": []})
    return jsonify({
        "messages": [
            clean_doc(message) for message in doc.get("messages", [])
        ]
    })

@app.post("/api/text")
@auth_required
def text_endpoint(user_id):
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    language = body.get("language") or "ru"

    try:
        result = process_text(text, language)
        save_exchange(user_id, text, result)
        return jsonify(result)
    except Exception as exc:
        print(f"/api/text error: {exc}")
        return jsonify({"error": str(exc)}), 500

@app.post("/api/voice")
@auth_required
def voice_endpoint(user_id):
    if "audio" not in request.files:
        return jsonify({"error": "Аудиофайл не найден."}), 400

    audio = request.files["audio"]
    language_hint = request.form.get("language") or "ru"

    try:
        require_service_config()
        transcription = groq_client.audio.transcriptions.create(
            file=("recording.webm", audio.read(), "audio/webm"),
            model=GROQ_STT_MODEL,
            response_format="json",
        )
        text = (transcription.text or "").strip()
        if not text:
            return jsonify({"error": "Не удалось распознать речь."}), 400

        result = process_text(text, language_hint)
        result["transcript"] = text
        save_exchange(user_id, text, result)
        return jsonify(result)
    except Exception as exc:
        print(f"/api/voice error: {exc}")
        return jsonify({"error": str(exc)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
