"""
Mattermost вместо Telegram: open-source, self-hosted чат (см. manifests/chat).
Используем REST API v4 бота: отправка сообщений с интерактивными кнопками
(props.attachments.actions - штатный механизм Mattermost, плагин не нужен)
и polling новых постов в канале для команды "ok".
"""
import os
import logging
import requests

log = logging.getLogger("mattermost_bot")

MATTERMOST_URL = os.environ.get("MATTERMOST_URL", "http://mattermost.chat.svc.cluster.local:8065").rstrip("/")
BOT_TOKEN = os.environ["MATTERMOST_BOT_TOKEN"]
CHANNEL_ID = os.environ["MATTERMOST_CHANNEL_ID"]
AGENT_SELF_URL = os.environ.get("AGENT_SELF_URL", "http://hermes-agent.hermes-agent.svc.cluster.local:8080").rstrip("/")

_session = requests.Session()
_session.headers.update({"Authorization": f"Bearer {BOT_TOKEN}"})

_bot_user_id = None


def _get_bot_user_id() -> str:
    global _bot_user_id
    if _bot_user_id is None:
        r = _session.get(f"{MATTERMOST_URL}/api/v4/users/me", timeout=10)
        r.raise_for_status()
        _bot_user_id = r.json()["id"]
    return _bot_user_id


def send_incident_message(incident_id: str, text: str) -> None:
    """Отправляет диагноз Hermes с кнопками подтверждения фикса."""
    payload = {
        "channel_id": CHANNEL_ID,
        "message": text,
        "props": {
            "attachments": [
                {
                    "text": "Ответь **ok** текстом в канал или нажми кнопку:",
                    "actions": [
                        {
                            "id": "fix",
                            "name": "✅ OK, исправить",
                            "integration": {
                                "url": f"{AGENT_SELF_URL}/mm_action",
                                "context": {"incident_id": incident_id, "action": "fix"},
                            },
                        },
                        {
                            "id": "cancel",
                            "name": "❌ Не трогать",
                            "integration": {
                                "url": f"{AGENT_SELF_URL}/mm_action",
                                "context": {"incident_id": incident_id, "action": "cancel"},
                            },
                        },
                    ],
                }
            ]
        },
    }
    r = _session.post(f"{MATTERMOST_URL}/api/v4/posts", json=payload, timeout=10)
    if not r.ok:
        log.error("Mattermost sendMessage failed: %s", r.text)


def send_text(text: str) -> None:
    r = _session.post(
        f"{MATTERMOST_URL}/api/v4/posts",
        json={"channel_id": CHANNEL_ID, "message": text},
        timeout=10,
    )
    if not r.ok:
        log.error("Mattermost sendMessage failed: %s", r.text)


def poll_new_posts(since_ms: int) -> tuple[list[dict], int]:
    """
    Возвращает список новых постов (не от бота) начиная с since_ms (unix millis),
    и новое значение since_ms для следующего вызова.
    """
    bot_id = _get_bot_user_id()
    r = _session.get(
        f"{MATTERMOST_URL}/api/v4/channels/{CHANNEL_ID}/posts",
        params={"since": since_ms},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    posts = data.get("posts", {})

    new_posts = []
    max_ts = since_ms
    for post_id in data.get("order", []):
        p = posts[post_id]
        if p.get("user_id") == bot_id:
            continue
        new_posts.append(p)
        max_ts = max(max_ts, p.get("create_at", since_ms))

    # posts возвращаются в порядке от новых к старым - сортируем по времени
    new_posts.sort(key=lambda p: p.get("create_at", 0))
    return new_posts, max_ts + 1
