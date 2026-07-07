"""
Вместо облачного Claude - открытая модель Hermes 3 (Nous Research, на базе Llama 3.1),
раздаваемая локально через Ollama (manifests/ollama). Ollama отдаёт OpenAI-совместимый
и собственный /api/chat эндпоинт; используем последний с параметром format="json",
чтобы модель гарантированно вернула валидный JSON.
"""
import os
import json
import logging
import requests

log = logging.getLogger("llm_client")

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama.ollama.svc.cluster.local:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "hermes3")

SYSTEM_PROMPT = """Ты - Hermes, AIOps-агент, который диагностирует проблемы в self-hosted GitLab,
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
  "human_summary": "текст для отправки человеку в чат: что случилось и что предлагается сделать",
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

    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "format": "json",   # просим Ollama форсировать валидный JSON на выходе
                "stream": False,
                "options": {"temperature": 0.2},
            },
            # CPU-инференс 8B-модели может занимать десятки секунд - минуты
            timeout=600,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
    except Exception as e:
        log.exception("Ошибка обращения к Ollama/Hermes")
        return _fallback(f"не удалось обратиться к Hermes ({e})")

    cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("Hermes вернул невалидный JSON: %s", content[:500])
        return _fallback(content)


def _fallback(raw_text: str) -> dict:
    return {
        "root_cause": "не удалось получить структурированный ответ от модели",
        "diagnosis": raw_text,
        "human_summary": raw_text,
        "remediation": {"type": "manual_only", "service": None, "pod": None,
                         "deployment": None, "replicas": None},
        "confidence": "low",
    }
