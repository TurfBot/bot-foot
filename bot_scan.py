import os
import asyncio
import threading
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from flask import Flask

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes


# =======================
# CONFIG
# =======================
API_KEY = os.getenv("API_SPORTS_KEY", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

# Pour éviter spam Telegram + exploser le quota :
# (1 scan = 1 requête live + N requêtes stats, N = nb matchs nuls HT/2H)
MAX_MATCHES_PER_SCAN = 25  # mets 10-25 conseillé


# =======================
# Render Free hack: open a web port
# =======================
web = Flask(__name__)

@web.get("/")
def home():
    return "OK - Bot is running"

@web.get("/health")
def health():
    return "healthy"

def run_web_server():
    # Render fournit PORT en env var
    port = int(os.environ.get("PORT", "10000"))
    # IMPORTANT: pas de reloader, sinon double process
    web.run(host="0.0.0.0", port=port, use_reloader=False)


# =======================
# Helpers
# =======================
def to_int(v: Any) -> int:
    if v is None:
        return 0
    s = str(v).strip().replace("%", "")
    if not s:
        return 0
    try:
        return int(float(s))
    except Exception:
        return 0

def get_stat(stats_list: List[Dict[str, Any]], stat_name: str) -> Any:
    for s in stats_list:
        if str(s.get("type", "")).strip().lower() == stat_name.strip().lower():
            return s.get("value")
    return None

def phase(fx: Dict[str, Any]) -> str:
    return (((fx.get("fixture") or {}).get("status") or {}).get("short") or "").upper().strip()

def is_target_phase(fx: Dict[str, Any]) -> bool:
    # tu as demandé : mi-temps OU 2e période
    return phase(fx) in {"HT", "2H"}

def is_draw(fx: Dict[str, Any]) -> bool:
    goals = fx.get("goals") or {}
    h = goals.get("home")
    a = goals.get("away")
    try:
        return int(h) == int(a)
    except Exception:
        return False

async def fetch_json(session: aiohttp.ClientSession, url: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    async with session.get(url, headers=HEADERS, params=params, timeout=25) as r:
        r.raise_for_status()
        return await r.json()

async def scan_once_all_draws() -> Tuple[List[str], int, int, int]:
    """
    Retourne:
      - messages: 1 message par match (nul HT/2H) avec stats
      - total_live: nb matchs live
      - draw_candidates: nb matchs nuls HT/2H
      - stats_calls: nb appels statistics faits
    """
    messages: List[str] = []
    stats_calls = 0

    async with aiohttp.ClientSession() as session:
        live = await fetch_json(session, f"{API_BASE}/fixtures", params={"live": "all"})
        fixtures = live.get("response", []) or []
        total_live = len(fixtures)

        candidates = [fx for fx in fixtures if is_target_phase(fx) and is_draw(fx)]
        draw_candidates = len(candidates)

        # limite de sécurité
        candidates = candidates[:MAX_MATCHES_PER_SCAN]

        for fx in candidates:
            fixture_id = ((fx.get("fixture") or {}).get("id"))
            if not fixture_id:
                continue

            home = ((fx.get("teams") or {}).get("home") or {}).get("name", "Home")
            away = ((fx.get("teams") or {}).get("away") or {}).get("name", "Away")
            st = phase(fx)
            goals = fx.get("goals") or {}
            sh = goals.get("home", 0)
            sa = goals.get("away", 0)

            # 1 requête par match
            stats = await fetch_json(session, f"{API_BASE}/fixtures/statistics", params={"fixture": str(fixture_id)})
            stats_calls += 1

            resp = stats.get("response", []) or []
            if len(resp) < 2:
                messages.append(
                    f"⚖️ NUL ({st}) : {home} vs {away}\n"
                    f"Score: {sh}-{sa}\n"
                    f"Stats: indisponibles pour l’instant\n"
                    f"Fixture ID: {fixture_id}"
                )
                continue

            a, b = resp[0], resp[1]
            a_stats = a.get("statistics", []) or []
            b_stats = b.get("statistics", []) or []

            a_poss = to_int(get_stat(a_stats, "Ball Possession"))
            b_poss = to_int(get_stat(b_stats, "Ball Possession"))
            a_sot  = to_int(get_stat(a_stats, "Shots on Goal"))
            b_sot  = to_int(get_stat(b_stats, "Shots on Goal"))
            a_cor  = to_int(get_stat(a_stats, "Corner Kicks"))
            b_cor  = to_int(get_stat(b_stats, "Corner Kicks"))

            messages.append(
                f"⚖️ NUL ({st}) : {home} vs {away}\n"
                f"Score: {sh}-{sa}\n"
                f"Possession: {a_poss}% / {b_poss}%\n"
                f"Tirs cadrés: {a_sot} / {b_sot}\n"
                f"Corners: {a_cor} / {b_cor}\n"
                f"Fixture ID: {fixture_id}"
            )

    return messages, total_live, draw_candidates, stats_calls


# =======================
# Telegram UI
# =======================
def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Scanner maintenant", callback_data="scan_now")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "✅ Bot prêt.\n"
        "Je liste tous les matchs NULS (HT ou 2H) avec : possession / tirs cadrés / corners.\n"
        "Clique sur le bouton pour lancer un scan.",
        reply_markup=keyboard()
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Scan lancé… (ça peut prendre quelques secondes)", reply_markup=keyboard())
    await do_scan_and_reply(update, context)

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "scan_now":
        # On édite le message du bouton pour indiquer que ça bosse
        await query.edit_message_text("Scan lancé… (ça peut prendre quelques secondes)")
        await do_scan_and_reply(update, context)

async def do_scan_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        msgs, total_live, draw_count, stats_calls = await scan_once_all_draws()

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"📊 Scan terminé.\n"
                f"Matchs live: {total_live}\n"
                f"Nuls (HT/2H): {draw_count}\n"
                f"Stats consultées: {stats_calls}\n"
                f"(Limite d’envoi: {MAX_MATCHES_PER_SCAN} matchs max par scan)"
            )
        )

        if not msgs:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Rien à afficher pour l’instant (aucun nul HT/2H).",
                reply_markup=keyboard()
            )
            return

        # 1 message par match
        for m in msgs:
            await context.bot.send_message(chat_id=chat_id, text=m)

        await context.bot.send_message(chat_id=chat_id, text="✅ Fin de la liste.", reply_markup=keyboard())

    except aiohttp.ClientResponseError as e:
        # typique quota dépassé / clé invalide
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ Erreur API (HTTP {e.status}).\n"
                 f"Si tu es en plan gratuit, vérifie ton quota API-Sports.\n"
                 f"Détails: {e.message}",
            reply_markup=keyboard()
        )
    except Exception as e:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ Erreur pendant le scan: {e}",
            reply_markup=keyboard()
        )


def main() -> None:
    if not API_KEY or not BOT_TOKEN:
        raise SystemExit("❌ Missing env vars: API_SPORTS_KEY and/or TELEGRAM_BOT_TOKEN")

    # Lancer le mini serveur web (Render Free)
    threading.Thread(target=run_web_server, daemon=True).start()

    # Lancer le bot Telegram
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CallbackQueryHandler(on_button))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
