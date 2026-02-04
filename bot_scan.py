import os
import asyncio
import aiohttp
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ============ CONFIG ============
API_KEY = "ee6bfc898ab1b8e6c0efb14ccc219814"
BOT_TOKEN = "8436143101:AAG9T9Z0AmFYs8js2jvWVG_3OSDBdvTjDto"


API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

# Sécurité quota/Telegram: limite le nombre de matchs détaillés envoyés par scan
MAX_MATCHES_PER_SCAN = 25  # ajuste (10-30 conseillé)

if not API_KEY or not BOT_TOKEN:
    raise SystemExit("❌ Missing env vars: API_SPORTS_KEY and/or TELEGRAM_BOT_TOKEN")

# ============ HELPERS ============
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

def is_draw(fx: Dict[str, Any]) -> bool:
    goals = fx.get("goals") or {}
    h = goals.get("home")
    a = goals.get("away")
    try:
        return int(h) == int(a)
    except Exception:
        return False

def phase(fx: Dict[str, Any]) -> str:
    return (((fx.get("fixture") or {}).get("status") or {}).get("short") or "").upper().strip()

def is_target_phase(fx: Dict[str, Any]) -> bool:
    return phase(fx) in {"HT", "2H"}

async def fetch_json(session: aiohttp.ClientSession, url: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    async with session.get(url, headers=HEADERS, params=params, timeout=25) as r:
        # Si quota dépassé, ici tu auras souvent un HTTP error -> on remonte l’exception
        r.raise_for_status()
        return await r.json()

async def scan_once_all_draws() -> Tuple[List[str], int, int, int]:
    """
    Retourne:
      - messages: liste de messages (1 par match) à envoyer
      - total_live: nb matchs live
      - draw_candidates: nb matchs nuls HT/2H
      - stats_calls: nb d'appels statistics effectués
    """
    messages: List[str] = []
    stats_calls = 0

    async with aiohttp.ClientSession() as session:
        live = await fetch_json(session, f"{API_BASE}/fixtures", params={"live": "all"})
        fixtures = live.get("response", []) or []
        total_live = len(fixtures)

        candidates = [fx for fx in fixtures if is_target_phase(fx) and is_draw(fx)]
        draw_candidates = len(candidates)

        # limite pour éviter spam + quota
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

            # Stats (1 req par match)
            stats = await fetch_json(session, f"{API_BASE}/fixtures/statistics", params={"fixture": str(fixture_id)})
            stats_calls += 1
            resp = stats.get("response", []) or []
            if len(resp) < 2:
                # parfois pas de stats live dispo -> on note quand même le match
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

            a_poss = get_stat(a_stats, "Ball Possession")
            b_poss = get_stat(b_stats, "Ball Possession")
            a_sot = get_stat(a_stats, "Shots on Goal")
            b_sot = get_stat(b_stats, "Shots on Goal")
            a_cor = get_stat(a_stats, "Corner Kicks")
            b_cor = get_stat(b_stats, "Corner Kicks")

            # Normalisation (corners parfois vides -> 0)
            a_poss_i = to_int(a_poss)
            b_poss_i = to_int(b_poss)
            a_sot_i = to_int(a_sot)
            b_sot_i = to_int(b_sot)
            a_cor_i = to_int(a_cor)
            b_cor_i = to_int(b_cor)

            messages.append(
                f"⚖️ NUL ({st}) : {home} vs {away}\n"
                f"Score: {sh}-{sa}\n"
                f"Possession: {a_poss_i}% / {b_poss_i}%\n"
                f"Tirs cadrés: {a_sot_i} / {b_sot_i}\n"
                f"Corners: {a_cor_i} / {b_cor_i}\n"
                f"Fixture ID: {fixture_id}"
            )

    return messages, total_live, draw_candidates, stats_calls

# ============ TELEGRAM ============
def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Scanner maintenant", callback_data="scan_now")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "OK ✅\nClique sur le bouton pour lancer un scan immédiat.\n"
        "Je liste tous les matchs NULS (HT ou 2H) avec possession / tirs cadrés / corners.",
        reply_markup=keyboard()
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "scan_now":
        await query.edit_message_text("Scan lancé… (ça peut prendre quelques secondes)")
        await do_scan_and_reply(update, context)

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Scan lancé…", reply_markup=keyboard())
    await do_scan_and_reply(update, context)

async def do_scan_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        msgs, total_live, draw_count, stats_calls = await scan_once_all_draws()

        # Résumé
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📊 Scan terminé.\nMatchs live: {total_live}\nNuls (HT/2H): {draw_count}\nStats consultées: {stats_calls}\n"
                 f"(Limite d’envoi: {MAX_MATCHES_PER_SCAN} matchs max par scan)"
        )

        if not msgs:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Rien à afficher pour l’instant (aucun nul HT/2H).",
                reply_markup=keyboard()
            )
            return

        # Envoi des matchs (un message par match)
        for m in msgs:
            await context.bot.send_message(chat_id=chat_id, text=m)

        await context.bot.send_message(chat_id=chat_id, text="✅ Fin de la liste.", reply_markup=keyboard())

    except Exception as e:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ Erreur pendant le scan: {e}\n"
                 f"Si tu es sur le plan gratuit, vérifie aussi ton quota API-Sports.",
            reply_markup=keyboard()
        )

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CallbackQueryHandler(on_button))
    app.run_polling()

if __name__ == "__main__":
    main()


