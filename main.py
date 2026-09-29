from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import sqlite3
from typing import List, Optional
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel

# ==================== КОНФИГУРАЦИЯ ====================

STACK_THRESHOLD = 10
MSK_TZ = ZoneInfo("Europe/Moscow")

BBOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
CHAT_ID = "ТВОЙ_CHAT_ID"  # Замени на ID чата / группы

stack_alert_sent_today = False


# ==================== CRON & ОЧИСТКА БД ====================


def clear_daily_slots():
  """Очищает базу данных от слотов в 04:00 по МСК"""
  global stack_alert_sent_today
  conn = sqlite3.connect("database.db")
  cursor = conn.cursor()
  cursor.execute("DELETE FROM availability")
  conn.commit()
  conn.close()

  stack_alert_sent_today = False
  print("[CRON] 🧹 База данных сброшена на новый день (04:00 МСК).")


scheduler = BackgroundScheduler(timezone=MSK_TZ)
scheduler.add_job(clear_daily_slots, "cron", hour=4, minute=0)


@asynccontextmanager
async def lifespan(app: FastAPI):
  scheduler.start()
  yield
  scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="public"), name="static")


# Обход экранов ngrok
@app.middleware("http")
async def add_ngrok_skip_header(request: Request, call_next):
  response = await call_next(request)
  response.headers["ngrok-skip-browser-warning"] = "true"
  return response


# ==================== ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ====================


def init_db():
  conn = sqlite3.connect("database.db")
  cursor = conn.cursor()

  # Включаем WAL режим для избежания "database is locked"
  cursor.execute("PRAGMA journal_mode=WAL;")

  cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            timezone TEXT
        )
    """)

  cursor.execute("""
        CREATE TABLE IF NOT EXISTS availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            start_utc TEXT,
            end_utc TEXT,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    """)

  conn.commit()
  conn.close()


init_db()

# ==================== HELPER: ОТПРАВКА АЛЕРТА ====================


async def send_telegram_alert(text: str):
  if not BOT_TOKEN or CHAT_ID == "ТВОЙ_CHAT_ID":
    print("[ALERT SKIPPED] CHAT_ID или BOT_TOKEN не заполнены.")
    return

  url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
  payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}

  async with httpx.AsyncClient() as client:
    try:
      response = await client.post(url, json=payload)
      response.raise_for_status()
    except Exception as e:
      print(f"[ALERT ERROR] {e}")


# ==================== PYDANTIC МОДЕЛИ ====================


class UserInitSchema(BaseModel):
  user_id: int
  username: str
  timezone: str


class SlotSchema(BaseModel):
  user_id: int
  start_utc: str
  end_utc: str


# ==================== ЭНДПОИНТЫ API ====================


@app.get("/")
async def read_root(request: Request):
  return FileResponse("public/index.html")


@app.post("/api/user/init")
async def init_user(data: UserInitSchema):
  conn = sqlite3.connect("database.db")
  cursor = conn.cursor()
  cursor.execute(
      """
        INSERT INTO users (user_id, username, timezone)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            timezone = excluded.timezone
    """,
      (data.user_id, data.username, data.timezone),
  )
  conn.commit()
  conn.close()
  return {"status": "ok"}


@app.post("/api/slots/set")
async def set_slots(user_id: int = Query(...), slots: List[SlotSchema] = []):
  global stack_alert_sent_today

  conn = sqlite3.connect("database.db")
  cursor = conn.cursor()

  # Стираем старый выбор пользователя
  cursor.execute("DELETE FROM availability WHERE user_id = ?", (user_id,))

  for slot in slots:
    cursor.execute(
        """
            INSERT INTO availability (user_id, start_utc, end_utc)
            VALUES (?, ?, ?)
        """,
        (slot.user_id, slot.start_utc, slot.end_utc),
    )

  conn.commit()
  conn.close()

  # Проверяем, собран ли стак
  analytics = await get_best_time()

  if analytics["is_full_stack"] and not stack_alert_sent_today:
    stack_alert_sent_today = True
    best_time = analytics["best_time_msk"]
    players_list = ", ".join(
        [f"@{p['username']}" for p in analytics["ready_players_detailed"]]
    )

    alert_text = (
        f"🎉 <b>СТЕК ИЗ 10 ЧЕЛОВЕК СОБРАН!</b>\n\n"
        f"⏰ <b>Время старта:</b> {best_time} (МСК)\n"
        f"🎮 <b>Участники:</b> {players_list}\n\n"
        f"🚀 Залетайте в игру!"
    )
    await send_telegram_alert(alert_text)

  return {"status": "ok"}


@app.get("/api/slots/get")
async def get_user_slots(user_id: int = Query(...)):
  """Возвращает текущие слоты пользователя для отображения в интерфейсе"""
  conn = sqlite3.connect("database.db")
  cursor = conn.cursor()
  cursor.execute(
      "SELECT start_utc, end_utc FROM availability WHERE user_id = ?",
      (user_id,),
  )
  rows = cursor.fetchall()
  conn.close()

  return [{"user_id": user_id, "start_utc": r[0], "end_utc": r[1]} for r in rows]


@app.get("/api/analytics/best-time")
async def get_best_time():
  conn = sqlite3.connect("database.db")
  cursor = conn.cursor()

  cursor.execute("""
        SELECT a.user_id, u.username, a.start_utc, a.end_utc 
        FROM availability a
        JOIN users u ON a.user_id = u.user_id
    """)
  rows = cursor.fetchall()
  conn.close()

  if not rows:
    return {
        "threshold": STACK_THRESHOLD,
        "best_count": 0,
        "is_full_stack": False,
        "best_time_msk": None,
        "missing_count": STACK_THRESHOLD,
        "ready_players_detailed": [],
        "declined_players": [],
    }

  parsed_slots = []
  ready_players_detailed = []
  declined_players = []
  seen_declined = set()
  seen_ready = set()

  for u_id, uname, s_utc, e_utc in rows:
    if s_utc == e_utc:
      if uname not in seen_declined:
        declined_players.append(uname)
        seen_declined.add(uname)
    else:
      start_dt_utc = datetime.fromisoformat(s_utc.replace("Z", "+00:00"))
      start_dt_msk = start_dt_utc.astimezone(MSK_TZ)
      end_dt_utc = datetime.fromisoformat(e_utc.replace("Z", "+00:00"))

      if uname not in seen_ready:
        ready_players_detailed.append({
            "username": uname,
            "start_time_msk": start_dt_msk.strftime("%H:%M"),
        })
        seen_ready.add(uname)

      parsed_slots.append({
          "user_id": u_id,
          "username": uname,
          "start": start_dt_utc,
          "end": end_dt_utc,
      })

  best_count = 0
  best_time_start_msk = None

  if parsed_slots:
    min_time = min(s["start"] for s in parsed_slots)
    max_time = max(s["end"] for s in parsed_slots)
    current = min_time

    while current < max_time:
      slot_end = current + timedelta(hours=1)
      active_count = sum(
          1
          for item in parsed_slots
          if item["start"] <= current and item["end"] >= slot_end
      )

      if active_count > best_count:
        best_count = active_count
        best_time_start_msk = current.astimezone(MSK_TZ).strftime("%H:%M")

      current += timedelta(hours=1)

  return {
      "threshold": STACK_THRESHOLD,
      "best_count": best_count,
      "is_full_stack": best_count >= STACK_THRESHOLD,
      "best_time_msk": best_time_start_msk,
      "missing_count": max(0, STACK_THRESHOLD - best_count),
      "ready_players_detailed": ready_players_detailed,
      "declined_players": declined_players,
  }