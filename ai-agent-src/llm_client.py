import os
import json
import anthropic

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """Ты - AIOps-агент, который диагностирует проблемы в self-hosted GitLab,
развёрнутом в Kubernetes (namespace "gitlab"). Тебе дают алерт от Prometheus/Alertmanager,
текущее состояние подов, последние события Kubernetes, логи контейнера, вывод `gitlab-ctl status`
и список похожих инцидентов из прошлого (с их фиксами) из векторной базы знаний.

Твоя задача:
1. Определить вероятную причину проблемы.
2. Предложить конкретное действие для исправления.
3. Вернуть ТОЛЬКО JSON без каких-либо пояснений вокруг, строго такой структуры:

{
  "root_cause": "короткое описание причины",
  "diagnosis": "подробный анализ на 2-4 предложения",
  "human_summary": "текст для отправки человеку в Telegram: что случилось и что предлагается сделать",
  "remediation": {
    "type": "restart_gitlab_service | restart_deployment | delete_pod | scale_deployment | manual_only",
    "service": "имя сервиса gitlab-ctl (postgresql/redis/puma/sidekiq/gitaly/nginx), если type=restart_gitlab_service, иначе null",
    "pod": "имя конкретного пода, если применимо, иначе null",
    "deployment": "имя deployment, если применимо, иначе null",
    "replicas": null
  },
  "confidence": "low | medium | high"
}

Если однозначного автоматического фикса нет - используй type = "manual_only" и объясни, что нужно
сделать человеку руками."""


def analyze_incident(alert: dict, pod_status: list, events: list, logs: str,
                      ctl_status: str, similar_incidents: list) -> dict:
    user_content = f"""АЛЕРТ:
{json.dumps(alert, ensure_ascii=False, indent=2)}

СОСТОЯНИЕ ПОДОВ:
{json.dumps(pod_status, ensure_ascii=False, indent=2)}

ПОСЛЕДНИЕ EVENTS:
{json.dumps(events, ensure_ascii=False, indent=2)}

ВЫВОД gitlab-ctl status:
{ctl_status}

ЛОГИ (последние строки):
{logs[-4000:]}

ПОХОЖИЕ ИНЦИДЕНТЫ ИЗ БАЗЫ ЗНАНИЙ (ChromaDB):
{json.dumps(similar_incidents, ensure_ascii=False, indent=2)}
"""

    resp = _client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    text = "".join(block.text for block in resp.content if block.type == "text")

    # LLM иногда оборачивает JSON в ```json ... ``` - подчищаем
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "root_cause": "не удалось распарсить ответ модели",
            "diagnosis": text,
            "human_summary": text,
            "remediation": {"type": "manual_only", "service": None, "pod": None,
                             "deployment": None, "replicas": None},
            "confidence": "low",
        }
