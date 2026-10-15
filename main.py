import os
import time
import json
import re
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

APP_TITLE = "World Explorer"
BASE_DIR = Path(__file__).resolve().parent

# Existing World Explorer keys/routes are preserved.
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "").strip()
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
CLOUDFLARE_IMAGE_MODEL = os.getenv(
    "CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell"
).strip()

# Dedicated keys for /travel-messanger.
TRAVEL_GROQ_API_KEY = os.getenv("TRAVEL_GROQ_API_KEY", "").strip()
TRAVEL_CLOUDFLARE_ACCOUNT_ID = os.getenv("TRAVEL_CLOUDFLARE_ACCOUNT_ID", "").strip()
TRAVEL_CLOUDFLARE_API_TOKEN = os.getenv("TRAVEL_CLOUDFLARE_API_TOKEN", "").strip()
TRAVEL_OPENWEATHER_API_KEY = os.getenv("TRAVEL_OPENWEATHER_API_KEY", "").strip()
TRAVEL_CLOUDFLARE_IMAGE_MODEL = os.getenv(
    "TRAVEL_CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell"
).strip()
TRAVEL_GROQ_MODEL = os.getenv("TRAVEL_GROQ_MODEL", "llama-3.3-70b-versatile").strip()

app = FastAPI(title=APP_TITLE)
_sky_cache: dict[str, tuple[float, dict]] = {}
_travel_sky_cache: dict[str, tuple[float, dict]] = {}


class SkyRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    weather: str = Field(default="clear sky", max_length=160)
    period: str = Field(default="day", max_length=20)


class TravelChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[dict] = Field(default_factory=list, max_length=16)


class TravelSkyRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    weather: str = Field(default="clear sky", max_length=160)
    period: str = Field(default="day", max_length=20)
    local_time: str = Field(default="unknown", max_length=80)
    place_name: str = Field(default="unknown place", max_length=160)


def read_html(filename: str) -> str:
    path = BASE_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(500, f"{filename} is missing from the repository root")


def _period_from_weather(data: dict) -> str:
    now = int(time.time()) + int(data.get("timezone", 0) or 0)
    local_hour = time.gmtime(now).tm_hour
    if local_hour < 5 or local_hour >= 21:
        return "night"
    if local_hour < 7:
        return "dawn"
    if local_hour >= 18:
        return "dusk"
    return "day"


def _parse_coordinates(text: str) -> Optional[tuple[float, float]]:
    match = re.search(
        r"(?<![\d.])(-?\d{1,2}(?:\.\d+)?)\s*[,;]\s*(-?\d{1,3}(?:\.\d+)?)(?![\d.])",
        text.strip(),
    )
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


async def _get_weather(lat: float, lon: float, api_key: str) -> dict:
    if not api_key:
        raise HTTPException(503, "Не настроен OpenWeather API key для этого раздела")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={"lat": lat, "lon": lon, "appid": api_key, "units": "metric", "lang": "ru"},
            )
        if response.status_code != 200:
            print("OpenWeather error:", response.status_code, response.text[:300])
            raise HTTPException(502, "OpenWeather не смог получить погоду для этой точки")
        data = response.json()
        weather_item = (data.get("weather") or [{}])[0]
        return {
            "name": data.get("name") or "Точка на карте",
            "lat": lat,
            "lon": lon,
            "temp": round(data.get("main", {}).get("temp", 0)),
            "feels_like": round(data.get("main", {}).get("feels_like", 0)),
            "description": weather_item.get("description", "неизвестно"),
            "icon": weather_item.get("icon", "01d"),
            "weather_id": weather_item.get("id", 800),
            "humidity": data.get("main", {}).get("humidity"),
            "wind": data.get("wind", {}).get("speed"),
            "timezone": data.get("timezone", 0),
            "sunrise": data.get("sys", {}).get("sunrise"),
            "sunset": data.get("sys", {}).get("sunset"),
            "observed_at": int(time.time()),
        }
    except httpx.TimeoutException:
        raise HTTPException(504, "OpenWeather не ответил вовремя")


@app.get("/", response_class=HTMLResponse)
async def home():
    return read_html("index.html")


@app.get("/navigator", response_class=HTMLResponse)
async def navigator():
    return read_html("navigator.html")


@app.get("/travel-messanger", response_class=HTMLResponse)
async def travel_messanger():
    return read_html("travel-messanger.html")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "openweather_configured": bool(OPENWEATHER_API_KEY),
        "cloudflare_configured": bool(CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN),
        "cloudflare_model": CLOUDFLARE_IMAGE_MODEL,
        "navigator_available": (BASE_DIR / "navigator.html").exists(),
        "travel_messanger_available": (BASE_DIR / "travel-messanger.html").exists(),
        "travel_groq_configured": bool(TRAVEL_GROQ_API_KEY),
        "travel_openweather_configured": bool(TRAVEL_OPENWEATHER_API_KEY),
        "travel_cloudflare_configured": bool(TRAVEL_CLOUDFLARE_ACCOUNT_ID and TRAVEL_CLOUDFLARE_API_TOKEN),
    }


# Existing weather API stays compatible with the home page and navigator.
@app.get("/api/weather")
async def weather(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180)):
    return await _get_weather(lat, lon, OPENWEATHER_API_KEY)


# Existing Cloudflare sky API stays compatible with the home page and navigator.
@app.post("/api/sky")
async def generate_sky(request: SkyRequest):
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        raise HTTPException(503, "Добавьте CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN в Render Environment")
    period = request.period.lower()
    if period not in {"dawn", "day", "dusk", "night", "morning", "evening"}:
        period = "day"
    cache_key = f"{round(request.lat, 1)}:{round(request.lon, 1)}:{request.weather.lower()}:{period}"
    cached = _sky_cache.get(cache_key)
    if cached and time.time() - cached[0] < 3600:
        return {**cached[1], "cached": True}
    prompt = (
        "Create a beautiful photorealistic ultra-wide cinematic sky panorama above a real 3D map. "
        f"Weather: {request.weather}. Local time of day: {period}. Coordinates: {request.lat:.3f}, {request.lon:.3f}. "
        "Natural clouds, physically plausible lighting, realistic sky gradient and distant atmospheric horizon. "
        "Only sky and distant atmospheric horizon: no foreground, buildings, mountains, people, text or logos. "
        "Wide landscape composition, high realism."
    )
    endpoint = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{CLOUDFLARE_IMAGE_MODEL}"
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}", "Content-Type": "application/json"},
                json={"prompt": prompt, "steps": 4},
            )
        if response.status_code != 200:
            print("Cloudflare sky error:", response.status_code, response.text[:400])
            raise HTTPException(502, f"Cloudflare не смог сгенерировать небо (HTTP {response.status_code})")
        body = response.json()
        image_data = (body.get("result") or {}).get("image")
        if not image_data:
            raise HTTPException(502, "Cloudflare вернул ответ без изображения")
        payload = {"ok": True, "image": "data:image/jpeg;base64," + image_data, "model": CLOUDFLARE_IMAGE_MODEL, "cached": False}
        _sky_cache[cache_key] = (time.time(), payload)
        return payload
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(504, "Генерация неба заняла слишком много времени")
    except Exception as exc:
        print("Cloudflare sky request failed:", repr(exc))
        raise HTTPException(502, "Не удалось связаться с Cloudflare Workers AI")


@app.post("/api/travel/chat")
async def travel_chat(request: TravelChatRequest):
    if not TRAVEL_GROQ_API_KEY:
        raise HTTPException(503, "Добавьте TRAVEL_GROQ_API_KEY в Render Environment")
    system_prompt = """Ты — дружелюбный AI-проводник World Explorer. Отвечай по-русски, если пользователь пишет по-русски, иначе используй язык пользователя. Помогай исследовать места и объясняй, что можно увидеть.
Если пользователь просит показать место, найти точку, открыть локацию или присылает координаты, обязательно добавляй в конце ответа отдельную строку строго в формате:
[SHOW_LOCATION: название места]
Для координат используй формат:
[SHOW_COORDINATES: широта, долгота]
Примеры: [SHOW_LOCATION: Eiffel Tower, Paris] или [SHOW_COORDINATES: 40.7128, -74.0060]
Не создавай эти маркеры, если пользователь просто обсуждает место и не просит его открыть/показать. Если координаты уже указаны в сообщении — используй их. Никогда не выдумывай координаты. Не утверждай, что карта уже открыта, пока клиент не подтвердил это. Маркер должен быть последней строкой. Не используй markdown-код вокруг маркера."""
    messages = [{"role": "system", "content": system_prompt}]
    for item in request.history[-12:]:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and isinstance(content, str):
            messages.append({"role": role, "content": content[:3000]})
    messages.append({"role": "user", "content": request.message})
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {TRAVEL_GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": TRAVEL_GROQ_MODEL, "messages": messages, "temperature": 0.4, "max_tokens": 700},
            )
        if response.status_code != 200:
            print("Groq travel chat error:", response.status_code, response.text[:500])
            if response.status_code in (401, 403):
                raise HTTPException(502, "Groq отклонил ключ. Проверьте TRAVEL_GROQ_API_KEY.")
            raise HTTPException(502, f"Groq не смог обработать запрос (HTTP {response.status_code})")
        body = response.json()
        answer = body["choices"][0]["message"]["content"].strip()
        show_location = None
        show_coordinates = None
        loc_match = re.search(r"\[SHOW_LOCATION:\s*(.*?)\s*\]", answer, flags=re.I)
        coord_match = re.search(r"\[SHOW_COORDINATES:\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]", answer, flags=re.I)
        if loc_match:
            show_location = loc_match.group(1).strip()[:180]
            answer = answer[:loc_match.start()].rstrip()
        if coord_match:
            try:
                lat, lon = float(coord_match.group(1)), float(coord_match.group(2))
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    show_coordinates = {"lat": lat, "lon": lon}
                    answer = answer[:coord_match.start()].rstrip()
            except ValueError:
                pass
        # Safety fallback: recognize coordinate pairs directly from the user's message.
        direct_coords = _parse_coordinates(request.message)
        # A coordinate pair alone is a valid destination request in this travel chat.
        if direct_coords and (re.fullmatch(r"\s*-?\d{1,2}(?:\.\d+)?\s*[,;]\s*-?\d{1,3}(?:\.\d+)?\s*", request.message) or any(
            word in request.message.lower()
            for word in ("покажи", "открой", "найди", "координат", "точка", "show", "open", "find", "map", "location")
        )):
            show_coordinates = {"lat": direct_coords[0], "lon": direct_coords[1]}
            show_location = None
        return {"reply": answer or "Конечно! Что хочешь исследовать?", "show_location": show_location, "show_coordinates": show_coordinates}
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(504, "ИИ-проводник долго отвечает. Попробуй ещё раз.")
    except Exception as exc:
        print("Groq travel chat request failed:", repr(exc))
        raise HTTPException(502, "Не удалось связаться с Groq")


@app.get("/api/travel/geocode")
async def travel_geocode(q: str = Query(min_length=2, max_length=220)):
    """Resolve a place name to coordinates using OpenStreetMap Nominatim."""
    try:
        async with httpx.AsyncClient(timeout=18, headers={"User-Agent": "WorldExplorerTravelMessenger/1.0 (location search)"}) as client:
            response = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "jsonv2", "limit": 5, "addressdetails": 1},
            )
        if response.status_code != 200:
            raise HTTPException(502, "Сервис поиска мест временно недоступен")
        results = response.json()
        return {
            "results": [
                {
                    "name": item.get("display_name", q),
                    "lat": float(item["lat"]),
                    "lon": float(item["lon"]),
                    "type": item.get("type"),
                }
                for item in results
            ]
        }
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(504, "Поиск места занял слишком много времени")
    except Exception as exc:
        print("Nominatim error:", repr(exc))
        raise HTTPException(502, "Не удалось выполнить поиск места")


@app.get("/api/travel/weather")
async def travel_weather(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180)):
    data = await _get_weather(lat, lon, TRAVEL_OPENWEATHER_API_KEY)
    data["period"] = _period_from_weather(data)
    # Human-readable local time estimate based on OpenWeather's UTC offset.
    local_epoch = int(time.time()) + int(data.get("timezone", 0) or 0)
    data["local_time"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime(local_epoch))
    return data


@app.post("/api/travel/sky")
async def travel_generate_sky(request: TravelSkyRequest):
    if not TRAVEL_CLOUDFLARE_ACCOUNT_ID or not TRAVEL_CLOUDFLARE_API_TOKEN:
        raise HTTPException(503, "Добавьте TRAVEL_CLOUDFLARE_ACCOUNT_ID и TRAVEL_CLOUDFLARE_API_TOKEN в Render Environment")
    period = request.period.lower()
    if period not in {"dawn", "day", "dusk", "night", "morning", "evening"}:
        period = "day"
    cache_key = f"{round(request.lat, 1)}:{round(request.lon, 1)}:{request.weather.lower()}:{period}"
    cached = _travel_sky_cache.get(cache_key)
    if cached and time.time() - cached[0] < 3600:
        return {**cached[1], "cached": True}
    prompt = (
        "Generate a breathtaking photorealistic wide cinematic sky and atmospheric horizon for a travel exploration map. "
        f"Place: {request.place_name}. Coordinates: {request.lat:.4f}, {request.lon:.4f}. "
        f"Real weather conditions: {request.weather}. Local time at this location: {request.local_time}; "
        f"time-of-day mood: {period}. Reflect the weather accurately with plausible cloud cover, haze, light, "
        "sun angle and sky colors. Beautiful natural realism, detailed clouds, atmospheric perspective, wide panorama. "
        "Sky and distant horizon only; do not include foreground terrain, buildings, people, text or logos."
    )
    endpoint = f"https://api.cloudflare.com/client/v4/accounts/{TRAVEL_CLOUDFLARE_ACCOUNT_ID}/ai/run/{TRAVEL_CLOUDFLARE_IMAGE_MODEL}"
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {TRAVEL_CLOUDFLARE_API_TOKEN}", "Content-Type": "application/json"},
                json={"prompt": prompt, "steps": 4},
            )
        if response.status_code != 200:
            print("Travel Cloudflare error:", response.status_code, response.text[:500])
            if response.status_code in (401, 403):
                raise HTTPException(502, "Cloudflare отклонил travel-токен. Проверьте TRAVEL_CLOUDFLARE_API_TOKEN.")
            raise HTTPException(502, f"Cloudflare не смог сгенерировать небо (HTTP {response.status_code})")
        body = response.json()
        image_data = (body.get("result") or {}).get("image")
        if not image_data:
            raise HTTPException(502, "Cloudflare вернул ответ без изображения")
        payload = {"ok": True, "image": "data:image/jpeg;base64," + image_data, "model": TRAVEL_CLOUDFLARE_IMAGE_MODEL, "cached": False}
        _travel_sky_cache[cache_key] = (time.time(), payload)
        return payload
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(504, "Генерация неба заняла слишком много времени")
    except Exception as exc:
        print("Travel Cloudflare request failed:", repr(exc))
        raise HTTPException(502, "Не удалось связаться с Cloudflare Workers AI")
    
