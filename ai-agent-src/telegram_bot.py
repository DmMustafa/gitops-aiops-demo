import os
import logging
import requests

log = logging.getLogger("telegram_bot")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


def send_incident_message(incident_id: str, text: str) -> None:
    """Отправляет диагноз и предложенный фикс с кнопками подтверждения."""
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ OK, исправить", "callback_data": f"fix:{incident_id}"},
                {"text": "❌ Не трогать", "callback_data": f"cancel:{incident_id}"},
            ]]
        },
    }
    r = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=10)
    if not r.ok:
        log.error("Telegram sendMessage failed: %s", r.text)


def send_text(text: str) -> None:
    r = requests.post(f"{API_URL}/sendMessage",
                       json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                       timeout=10)
    if not r.ok:
        log.error("Telegram sendMessage failed: %s", r.text)


def answer_callback_query(callback_query_id: str, text: str) -> None:
    requests.post(f"{API_URL}/answerCallbackQuery",
                  json={"callback_query_id": callback_query_id, "text": text},
                  timeout=10)


def get_updates(offset: int | None = None, timeout: int = 20) -> list:
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{API_URL}/getUpdates", params=params, timeout=timeout + 10)
    r.raise_for_status()
    return r.json().get("result", [])
