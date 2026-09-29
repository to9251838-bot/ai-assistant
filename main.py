import os
import io
import json
import base64
import secrets
from datetime import datetime, timedelta, timezone
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
    doc.pop("_id", None)
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
