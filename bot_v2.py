# =========================
#  BOT DE TELEGRAM — V2 (ligas + reglas en directo, min 60)
#  Requisitos:
#    pip install "python-telegram-bot[rate-limiter,job-queue]==21.4" aiosqlite pytz tzdata aiohttp
#  Entorno:
#    TELEGRAM_TOKEN (obligatoria)
#    APISPORTS_KEY (opcional; si está, usa API-FOOTBALL real; si no, mock)
# =========================

import os
import aiosqlite
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, time
from typing import Optional, List, Tuple, Set

# --- Zona horaria Europe/Madrid con fallback ---
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Madrid")
except Exception:
    import pytz
    TZ = pytz.timezone("Europe/Madrid")

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    AIORateLimiter, JobQueue
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHECK_INTERVAL_MIN = 2          # frecuencia del JobQueue (min)
NOTIFY_BEFORE_MIN = 10          # aviso previo al inicio del partido (min)
DB_PATH = "subs_v2.db"

# ---------- Reglas soportadas ----------
SUPPORTED_RULES = {
    "inicio",
    "descanso",
    "final",
    "gol-mi-equipo",
    "gol-rival",
    "gol-cualquiera",
    "roja",
    "penalti",
    "min60-igualado",
    "min60-diferencia1",
}

# ---------- Modelo de evento ----------
@dataclass
class Event:
    kind: str                  # "inicio" | "gol" | "roja" | "penalti" | "descanso" | "final"
    minute: Optional[int]
    team: Optional[str]
    home: str
    away: str
    league: str
    match_id: str
    kickoff_utc: datetime
    status: str
    home_goals: int = 0
    away_goals: int = 0

# ---------- SportsAPI: real (API-FOOTBALL) o mock ----------
USE_REAL_API = bool(os.getenv("APISPORTS_KEY"))

if USE_REAL_API:
    import aiohttp
    class SportsAPI:
        BASE = "https://v3.football.api-sports.io"
        def __init__(self, api_key: Optional[str] = None):
            self.api_key = api_key or os.getenv("APISPORTS_KEY")
            if not self.api_key:
                raise RuntimeError("Falta APISPORTS_KEY (clave de API-FOOTBALL).")
            self._headers = {"x-apisports-key": self.api_key, "Accept": "application/json"}

        async def _get(self, path: str, params: dict) -> dict:
            async with aiohttp.ClientSession(headers=self._headers) as s:
                async with s.get(f"{self.BASE}{path}", params=params, timeout=30) as r:
                    r.raise_for_status()
                    return await r.json()

        async def search_team(self, name: str) -> Optional[dict]:
            data = await self._get("/teams", {"search": name})
            items = data.get("response", [])
            if not items: return None
            t = items[0]["team"]
            return {"id": str(t["id"]), "name": t["name"]}

        async def search_league(self, name: str) -> Optional[dict]:
            data = await self._get("/leagues", {"search": name})
            items = data.get("response", [])
            if not items: return None
            lg = items[0]["league"]
            return {"id": str(lg["id"]), "name": lg["name"]}

        def _guess_season(self) -> int:
            today = datetime.now(TZ).date()
            y = today.year
            return y - 1 if today.month < 7 else y

        def _fixture_to_match(self, fx: dict) -> dict:
            f = fx["fixture"]; l = fx["league"]; h = fx["teams"]["home"]; a = fx["teams"]["away"]
            goals = fx.get("goals") or {"home": 0, "away": 0}
            kickoff_utc = datetime.fromisoformat(f["date"].replace("Z", "+00:00")).astimezone(timezone.utc)
            status = f["status"]["short"]
            status_map = {
                "NS": "SCHEDULED", "TBD": "SCHEDULED", "PST": "SCHEDULED",
                "1H": "LIVE", "HT": "PAUSED", "2H": "LIVE",
                "ET": "LIVE", "BT": "PAUSED",
                "FT": "FINISHED", "AET": "FINISHED", "PEN": "FINISHED"
            }
            norm = status_map.get(status, "LIVE" if status not in {"SCHEDULED", "FINISHED"} else status)
            return {
                "match_id": str(f["id"]),
                "home": h["name"],
                "away": a["name"],
                "league": l["name"],
                "kickoff_utc": kickoff_utc,
                "status": "LIVE" if norm in {"LIVE", "PAUSED"} else norm,
                "home_goals": goals.get("home") or 0,
                "away_goals": goals.get("away") or 0,
            }

        async def upcoming_or_live_for_team(self, team_id: str) -> List[dict]:
            out = []
            d_next = await self._get("/fixtures", {"team": team_id, "next": 10})
            for it in d_next.get("response", []):
                out.append(self._fixture_to_match(it))
            d_live = await self._get("/fixtures", {"live": "all"})
            for it in d_live.get("response", []):
                if str(it["teams"]["home"]["id"]) == team_id or str(it["teams"]["away"]["id"]) == team_id:
                    out.append(self._fixture_to_match(it))
            return out

        async def upcoming_or_live_for_league(self, league_id: str) -> List[dict]:
            season = self._guess_season()
            out = []
            today = datetime.now(timezone.utc).date().isoformat()
            in3  = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
            d_up = await self._get("/fixtures", {"league": league_id, "season": season, "from": today, "to": in3})
            for it in d_up.get("response", []):
                out.append(self._fixture_to_match(it))
            d_live = await self._get("/fixtures", {"live": "all"})
            for it in d_live.get("response", []):
                if str(it["league"]["id"]) == str(league_id):
                    out.append(self._fixture_to_match(it))
            return out

        async def live_events_for_match(self, match: dict) -> List["Event"]:
            fixture_id = match["match_id"]
            data = await self._get("/fixtures/events", {"fixture": fixture_id})
            evs = []
            status = match.get("status", "LIVE")
            league = match["league"]; home = match["home"]; away = match["away"]
            ko = match["kickoff_utc"]
            home_goals = match.get("home_goals", 0); away_goals = match.get("away_goals", 0)
            for it in data.get("response", []):
                typ = (it.get("type") or "").lower()
                det = (it.get("detail") or "").lower()
                minute = it.get("time", {}).get("elapsed")
                team_name = it.get("team", {}).get("name")
                if typ == "goal":
                    evs.append(Event("gol", minute, team_name, home, away, league, str(fixture_id), ko, status, home_goals, away_goals))
                elif typ == "card" and "red" in det:
                    evs.append(Event("roja", minute, team_name, home, away, league, str(fixture_id), ko, status, home_goals, away_goals))
                elif typ == "var" and "penalty" in det:
                    evs.append(Event("penalti", minute, team_name, home, away, league, str(fixture_id), ko, status, home_goals, away_goals))
            return evs

else:
    # MOCK para pruebas sin API key
    class SportsAPI:
        async def search_team(self, name: str) -> Optional[dict]:
            nn = name.strip().lower()
            if nn in {"barcelona", "fc barcelona", "barça"}:
                return {"id": "fcbarcelona", "name": "FC Barcelona"}
            if nn in {"real madrid", "madrid"}:
                return {"id": "realmadrid", "name": "Real Madrid"}
            return {"id": nn, "name": name.strip()}

        async def search_league(self, name: str) -> Optional[dict]:
            nl = name.strip().lower()
            if nl in {"laliga", "la liga", "liga"}:
                return {"id": "laliga", "name": "LaLiga"}
            if nl in {"premier", "premier league"}:
                return {"id": "epl", "name": "Premier League"}
            return {"id": nl, "name": name.strip()}

        async def upcoming_or_live_for_team(self, team_id: str) -> List[dict]:
            now = datetime.now(timezone.utc)
            return [{
                "match_id": f"{team_id}-demo-{now.minute}",
                "home": "FC Barcelona",
                "away": "Real Madrid",
                "league": "LaLiga",
                "kickoff_utc": now + timedelta(minutes=12),
                "status": "SCHEDULED",
                "home_goals": 0,
                "away_goals": 0,
            }]

        async def upcoming_or_live_for_league(self, league_id: str) -> List[dict]:
            now = datetime.now(timezone.utc)
            return [
                {
                    "match_id": f"{league_id}-A-{now.minute}",
                    "home": "Sevilla",
                    "away": "Valencia",
                    "league": "LaLiga" if league_id == "laliga" else league_id,
                    "kickoff_utc": now + timedelta(minutes=12),
                    "status": "SCHEDULED",
                    "home_goals": 0,
                    "away_goals": 0,
                },
                {
                    "match_id": f"{league_id}-B-{now.minute}",
                    "home": "Betis",
                    "away": "Villarreal",
                    "league": "LaLiga" if league_id == "laliga" else league_id,
                    "kickoff_utc": now - timedelta(minutes=65),
                    "status": "LIVE",
                    "home_goals": 1,
                    "away_goals": 1,
                },
            ]

        async def live_events_for_match(self, match: dict) -> List[Event]:
            now = datetime.now(timezone.utc)
            ko = match["kickoff_utc"]
            minutes = max(0, int((now - ko).total_seconds() // 60))
            evs: List[Event] = []
            if 0 <= minutes <= 1 and match["status"] in {"LIVE", "IN_PLAY"}:
                evs.append(Event("inicio", 1, None, match["home"], match["away"], match["league"],
                                 match["match_id"], ko, match["status"]))
            if minutes >= 61 and match["status"] in {"LIVE", "IN_PLAY"}:
                evs.append(Event("gol", minutes, match["home"], match["home"], match["away"], match["league"],
                                 match["match_id"], ko, match["status"],
                                 (match.get("home_goals", 1) + 1), match.get("away_goals", 1)))
            return evs

# ---------- SQL (tablas) ----------
CREATE_SQL = [
    """
    CREATE TABLE IF NOT EXISTS subscriptions (
        chat_id INTEGER NOT NULL,
        target_type TEXT NOT NULL,   -- 'team' o 'league'
        target_id TEXT NOT NULL,
        target_name TEXT NOT NULL,
        PRIMARY KEY (chat_id, target_type, target_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sent_alerts (
        chat_id INTEGER NOT NULL,
        match_id TEXT NOT NULL,
        event_key TEXT NOT NULL,
        PRIMARY KEY (chat_id, match_id, event_key)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS user_settings (
        chat_id INTEGER PRIMARY KEY,
        alerts_enabled INTEGER DEFAULT 1,
        quiet_start TEXT DEFAULT NULL,  -- '23:00'
        quiet_end   TEXT DEFAULT NULL,  -- '08:00'
        cooldown_min INTEGER DEFAULT 5
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS user_rules (
        chat_id INTEGER NOT NULL,
        rule TEXT NOT NULL,
        PRIMARY KEY (chat_id, rule)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS last_sent_ts (
        chat_id INTEGER PRIMARY KEY,
        last_ts REAL DEFAULT 0
    );
    """,
]

# ---------- Inicialización DB ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        for sql in CREATE_SQL:
            await db.execute(sql)
        await db.commit()

# ---------- Helpers de settings/regras ----------
async def ensure_user_settings(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT chat_id FROM user_settings WHERE chat_id=?", (chat_id,))
        if not await cur.fetchone():
            await db.execute("INSERT INTO user_settings (chat_id) VALUES (?)", (chat_id,))
            await db.commit()

async def get_user_settings(chat_id: int) -> dict:
    await ensure_user_settings(chat_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT alerts_enabled, quiet_start, quiet_end, cooldown_min FROM user_settings WHERE chat_id=?",
            (chat_id,)
        )
        row = await cur.fetchone()
    return {
        "alerts_enabled": bool(row[0]),
        "quiet_start": row[1],
        "quiet_end": row[2],
        "cooldown_min": int(row[3]),
    }

async def set_alerts_enabled(chat_id: int, enabled: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO user_settings (chat_id, alerts_enabled) VALUES (?, ?)",
                         (chat_id, int(enabled)))
        await db.execute("UPDATE user_settings SET alerts_enabled=? WHERE chat_id=?",
                         (int(enabled), chat_id))
        await db.commit()

async def set_quiet_hours(chat_id: int, start: Optional[str], end: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_settings SET quiet_start=?, quiet_end=? WHERE chat_id=?",
                         (start, end, chat_id))
        await db.commit()

async def set_cooldown(chat_id: int, minutes: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_settings SET cooldown_min=? WHERE chat_id=?",
                         (minutes, chat_id))
        await db.commit()

async def get_rules(chat_id: int) -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT rule FROM user_rules WHERE chat_id=? ORDER BY rule", (chat_id,))
        rows = await cur.fetchall()
    return [r[0] for r in rows]

async def add_rule(chat_id: int, rule: str) -> bool:
    if rule not in SUPPORTED_RULES:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("INSERT INTO user_rules (chat_id, rule) VALUES (?, ?)", (chat_id, rule))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return True  # ya estaba

async def remove_rule(chat_id: int, rule: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM user_rules WHERE chat_id=? AND rule=?", (chat_id, rule))
        await db.commit()
        return cur.rowcount > 0

# ---------- Suscripciones ----------
async def sub_add(chat_id: int, target_type: str, target_id: str, target_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO subscriptions (chat_id, target_type, target_id, target_name) VALUES (?,?,?,?)",
                (chat_id, target_type, target_id, target_name)
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def sub_remove(chat_id: int, target_type: str, name_like: str) -> Optional[str]:
    name_like = name_like.lower()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT target_id, target_name FROM subscriptions WHERE chat_id=? AND target_type=?",
            (chat_id, target_type)
        )
        rows = await cur.fetchall()
        for tid, tname in rows:
            if tname.lower() == name_like or name_like in tname.lower():
                await db.execute(
                    "DELETE FROM subscriptions WHERE chat_id=? AND target_type=? AND target_id=?",
                    (chat_id, target_type, tid)
                )
                await db.commit()
                return tname
    return None

async def sub_list(chat_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT target_type, target_name FROM subscriptions WHERE chat_id=? ORDER BY target_type, target_name",
            (chat_id,)
        )
        rows = await cur.fetchall()
    out = {"team": [], "league": []}
    for ttype, name in rows:
        out.setdefault(ttype, []).append(name)
    return out

# ---------- Utilidades ----------
def parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))

async def is_quiet_now(settings: dict) -> bool:
    qs = settings.get("quiet_start"); qe = settings.get("quiet_end")
    if not qs or not qe: return False
    now_local = datetime.now(TZ).time()
    tqs = parse_hhmm(qs); tqe = parse_hhmm(qe)
    if tqs <= tqe:
        return tqs <= now_local <= tqe
    return now_local >= tqs or now_local <= tqe  # cruza medianoche

# Cooldown persistente por chat
async def get_last_sent_ts(chat_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT last_ts FROM last_sent_ts WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0

async def set_last_sent_ts(chat_id: int, ts: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO last_sent_ts (chat_id, last_ts) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET last_ts=excluded.last_ts",
            (chat_id, ts)
        )
        await db.commit()

async def passes_cooldown(chat_id: int, cooldown_min: int) -> bool:
    last = await get_last_sent_ts(chat_id)
    now_ts = datetime.now(timezone.utc).timestamp()
    return (now_ts - last) >= (cooldown_min * 60)

# ---------- Comandos ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user_settings(update.effective_chat.id)
    await update.message.reply_text(
        "¡Hola! Soy tu bot de alertas deportivas (V2).\n\n"
        "Suscripciones:\n"
        "• /subscribe <equipo>\n• /unsubscribe <equipo>\n"
        "• /subscribe_league <liga>\n• /unsubscribe_league <liga>\n"
        "• /subscriptions\n\n"
        "Alertas en directo:\n"
        "• /alerts on | /alerts off\n"
        "• /when add <regla> | /when remove <regla> | /when list\n"
        "Reglas: inicio, gol-mi-equipo, gol-rival, gol-cualquiera, roja, penalti, min60-igualado, min60-diferencia1\n\n"
        "Silencio y frecuencia:\n"
        "• /quiet 23:00-08:00\n• /cooldown 5"
    )

async def subscribe_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /subscribe <equipo>")
        return
    name = " ".join(context.args)
    api: SportsAPI = context.application.bot_data["sports_api"]
    t = await api.search_team(name)
    if not t:
        await update.message.reply_text("No encontré ese equipo 🤔")
        return
    ok = await sub_add(update.effective_chat.id, "team", t["id"], t["name"])
    await update.message.reply_text("✅ Suscrito a equipo: " + t["name"] if ok else "Ya estabas suscrito a ese equipo")

async def unsubscribe_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /unsubscribe <equipo>")
        return
    removed = await sub_remove(update.effective_chat.id, "team", " ".join(context.args))
    await update.message.reply_text("❌ Cancelada: " + removed if removed else "No encontré esa suscripción")

async def subscribe_league(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /subscribe_league <liga>")
        return
    name = " ".join(context.args)
    api: SportsAPI = context.application.bot_data["sports_api"]
    l = await api.search_league(name)
    if not l:
        await update.message.reply_text("No encontré esa liga 🤔")
        return
    ok = await sub_add(update.effective_chat.id, "league", l["id"], l["name"])
    await update.message.reply_text("✅ Suscrito a liga: " + l["name"] if ok else "Ya estabas suscrito a esa liga")

async def unsubscribe_league(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /unsubscribe_league <liga>")
        return
    removed = await sub_remove(update.effective_chat.id, "league", " ".join(context.args))
    await update.message.reply_text("❌ Cancelada: " + removed if removed else "No encontré esa suscripción")

async def list_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs = await sub_list(update.effective_chat.id)
    teams = "\n".join(f"• {n}" for n in subs.get("team", [])) or "(ninguno)"
    leagues = "\n".join(f"• {n}" for n in subs.get("league", [])) or "(ninguna)"
    await update.message.reply_text(f"🔔 Equipos:\n{teams}\n\n🏆 Ligas:\n{leagues}")

async def alerts_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_alerts_enabled(update.effective_chat.id, True)
    await update.message.reply_text("🔔 Alertas en directo: ACTIVADAS")

async def alerts_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_alerts_enabled(update.effective_chat.id, False)
    await update.message.reply_text("🔕 Alertas en directo: DESACTIVADAS")

async def when_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /when add <regla>")
        return
    rule = context.args[0].strip().lower()
    if rule not in SUPPORTED_RULES:
        await update.message.reply_text("Regla no reconocida. Usa /when list para ver opciones.")
        return
    ok = await add_rule(update.effective_chat.id, rule)
    await update.message.reply_text("✅ Regla añadida" if ok else "La regla ya existía")

async def when_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /when remove <regla>")
        return
    rule = context.args[0].strip().lower()
    ok = await remove_rule(update.effective_chat.id, rule)
    await update.message.reply_text("❌ Regla eliminada" if ok else "No tenías esa regla")

async def when_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules = await get_rules(update.effective_chat.id)
    if not rules:
        await update.message.reply_text("No tienes reglas. Ejemplos: inicio, gol-mi-equipo, min60-igualado")
        return
    await update.message.reply_text("📐 Tus reglas:\n" + "\n".join(f"• {r}" for r in rules))

async def when_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Permite /when add|remove|list ...
    if not context.args:
        await when_list(update, context)
        return
    subcmd = context.args[0].lower()
    context.args = context.args[1:]
    if subcmd == "add":
        await when_add(update, context)
    elif subcmd == "remove":
        await when_remove(update, context)
    elif subcmd == "list":
        await when_list(update, context)
    else:
        await update.message.reply_text("Usa: /when add <regla> | /when remove <regla> | /when list")

async def quiet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or "-" not in context.args[0]:
        await update.message.reply_text("Uso: /quiet HH:MM-HH:MM (ej. 23:00-08:00)")
        return
    span = context.args[0]
    start, end = span.split("-")
    await set_quiet_hours(update.effective_chat.id, start, end)
    await update.message.reply_text(f"😴 Horario silencioso: {start}-{end}")

async def cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /cooldown <minutos>")
        return
    try:
        mins = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Debes poner un número de minutos")
        return
    await set_cooldown(update.effective_chat.id, mins)
    await update.message.reply_text(f"⏱️ Enfriamiento entre avisos: {mins} min")

async def subscribe_league_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("Uso: /subscribe_league_id <ID>")
        return

    league_id = context.args[0]

    # Intentamos buscar la liga en la API para confirmar
    api: SportsAPI = context.application.bot_data["sports_api"]
    try:
        data = await api._get("/leagues", {"id": league_id})
        leagues = data.get("response", [])
        if not leagues:
            await update.message.reply_text(f"No encontré ninguna liga con ID {league_id}.")
            return
        league_name = leagues[0]["league"]["name"]
        country = leagues[0]["country"]["name"]
    except Exception:
        await update.message.reply_text("Error al consultar la API. Verifica el ID.")
        return

    # Guardamos en la base de datos
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO subscriptions (chat_id, target_type, target_id, target_name) VALUES (?, 'league', ?, ?)",
            (chat_id, league_id, f"{league_name} ({country})"),
        )
        await db.commit()

    await update.message.reply_text(f"✅ Suscrito a la liga: {league_name} ({country}) [ID {league_id}]")


# ---------- Evaluación de reglas ----------
def minute_based_checks(ev: Event, rules: Set[str]) -> bool:
    if ev.minute is None or ev.minute < 60:
        return False
    diff = abs(ev.home_goals - ev.away_goals)
    if "min60-igualado" in rules and ev.home_goals == ev.away_goals:
        return True
    if "min60-diferencia1" in rules and diff == 1:
        return True
    return False

async def should_send(ev: Event, rules: Set[str], my_teams: Set[str]) -> Tuple[bool, str]:
    if ev.kind == "inicio" and "inicio" in rules:
        return True, "🔔 Empieza el partido"
    if ev.kind == "descanso" and "descanso" in rules:
        return True, "⏸️ Descanso"
    if ev.kind == "final" and "final" in rules:
        return True, "🏁 Final del partido"
    if ev.kind == "roja" and "roja" in rules:
        return True, "🟥 Tarjeta roja"
    if ev.kind == "penalti" and "penalti" in rules:
        return True, "⚪ Penalti"

    if ev.kind == "gol":
        if "gol-cualquiera" in rules:
            return True, "⚽ Gol"
        if ev.team in my_teams and "gol-mi-equipo" in rules:
            return True, "⚽ Gol de tu equipo"
        if ev.team and ev.team not in my_teams and "gol-rival" in rules:
            return True, "⚽ Gol del rival"

    if minute_based_checks(ev, rules):
        return True, "⏱️ Momento caliente (min 60+)"
    return False, ""

# ---------- Duplicados y cooldown ----------
async def already_sent(chat_id: int, match_id: str, event_key: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM sent_alerts WHERE chat_id=? AND match_id=? AND event_key=?",
            (chat_id, match_id, event_key)
        )
        return (await cur.fetchone()) is not None

async def mark_sent(chat_id: int, match_id: str, event_key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO sent_alerts (chat_id, match_id, event_key) VALUES (?,?,?)",
            (chat_id, match_id, event_key)
        )
        await db.commit()

# ---------- Recolección de partidos ----------
async def fetch_user_matches(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> List[dict]:
    api: SportsAPI = context.application.bot_data["sports_api"]
    out: List[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT target_type, target_id FROM subscriptions WHERE chat_id=?",
            (chat_id,)
        )
        for ttype, tid in await cur.fetchall():
            if ttype == "team":
                out.extend(await api.upcoming_or_live_for_team(tid))
            elif ttype == "league":
                out.extend(await api.upcoming_or_live_for_league(tid))
    return out

# ---------- Tarea periódica ----------
async def check_matches(context: ContextTypes.DEFAULT_TYPE):
    api: SportsAPI = context.application.bot_data["sports_api"]

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT chat_id FROM subscriptions")
        chats = [r[0] for r in await cur.fetchall()]

    for chat_id in chats:
        settings = await get_user_settings(chat_id)
        if not settings.get("alerts_enabled", True):
            continue
        if await is_quiet_now(settings):
            continue

        # Cooldown
        if not await passes_cooldown(chat_id, settings.get("cooldown_min", 5)):
            continue

        # Equipos propios para distinguir el gol
        my_teams: Set[str] = set()
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT target_name FROM subscriptions WHERE chat_id=? AND target_type='team'",
                (chat_id,)
            )
            my_teams = {r[0] for r in await cur.fetchall()}

        rules_set: Set[str] = set(await get_rules(chat_id))
        matches = await fetch_user_matches(context, chat_id)

        something_sent = False

        for m in matches:
            kickoff_utc = m["kickoff_utc"]
            status = m["status"]
            now_utc = datetime.now(timezone.utc)
            minutes_to = (kickoff_utc - now_utc).total_seconds() / 60.0

            # Aviso previo al inicio
            if status == "SCHEDULED" and 0 <= minutes_to <= NOTIFY_BEFORE_MIN:
                ek = "prestart"
                if not await already_sent(chat_id, m["match_id"], ek):
                    ko_local = kickoff_utc.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
                    text = (
                        f"⏰ Empieza pronto\n"
                        f"🏟️ {m['home']} vs {m['away']} ({m['league']})\n"
                        f"🗓️ {ko_local}"
                    )
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=text)
                        await mark_sent(chat_id, m["match_id"], ek)
                        something_sent = True
                    except Exception:
                        pass

            # Eventos en vivo
            if status in {"LIVE", "IN_PLAY", "PAUSED"}:
                events = await api.live_events_for_match(m)
                for ev in events:
                    event_key = f"{ev.kind}-{ev.minute or 0}-{ev.team or ''}"
                    if await already_sent(chat_id, ev.match_id, event_key):
                        continue
                    ok, label = await should_send(ev, rules_set, my_teams)
                    if not ok:
                        continue
                    msg = (
                        f"{label}\n"
                        f"🏟️ {ev.home} vs {ev.away} ({ev.league})\n"
                        f"⏱️ min {ev.minute or '-'}   ⚖️ {ev.home_goals}-{ev.away_goals}"
                    )
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=msg)
                        await mark_sent(chat_id, ev.match_id, event_key)
                        something_sent = True
                    except Exception:
                        pass

        if something_sent:
            await set_last_sent_ts(chat_id, datetime.now(timezone.utc).timestamp())

# ---------- Startup ----------
async def on_startup(app):
    me = await app.bot.get_me()
    print(f"El bot que está arrancando es: @{me.username}")

    await init_db()
    # Instancia la API real si hay key; si no, mock
    if USE_REAL_API:
        app.bot_data["sports_api"] = SportsAPI(os.getenv("APISPORTS_KEY"))
        print("[startup] Usando API-FOOTBALL real.")
    else:
        app.bot_data["sports_api"] = SportsAPI()
        print("[startup] Usando MOCK (sin APISPORTS_KEY).")

    if app.job_queue is None:
        jq = JobQueue()
        jq.set_application(app)
        app.job_queue = jq

    app.job_queue.run_repeating(check_matches, interval=CHECK_INTERVAL_MIN * 60, first=5)
    print("[startup] Bot iniciado y JobQueue programada.")

# ---------- MAIN ----------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta TELEGRAM_TOKEN en variables de entorno.")
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("subscribe", subscribe_team))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_team))
    application.add_handler(CommandHandler("subscribe_league", subscribe_league))
    application.add_handler(CommandHandler("unsubscribe_league", unsubscribe_league))
    application.add_handler(CommandHandler("subscriptions", list_subs))
    application.add_handler(CommandHandler("alerts", alerts_on))      # /alerts on/off -> usamos alias abajo
    application.add_handler(CommandHandler("alerts_on", alerts_on))
    application.add_handler(CommandHandler("alerts_off", alerts_off))
    application.add_handler(CommandHandler("when_add", when_add))     # alias directo
    application.add_handler(CommandHandler("when_remove", when_remove))
    application.add_handler(CommandHandler("when", when_router))      # /when add|remove|list

    application.add_handler(CommandHandler("quiet", quiet))
    application.add_handler(CommandHandler("cooldown", cooldown))
    application.add_handler(CommandHandler("subscribe_league_id", subscribe_league_id))


    if application.job_queue is None:
        jq = JobQueue()
        jq.set_application(application)
        application.job_queue = jq

    application.post_init = on_startup
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
