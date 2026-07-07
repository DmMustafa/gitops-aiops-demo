import os
import time
import uuid
import logging
import threading

from flask import Flask, request, jsonify

import k8s_tools
import chroma_client
import llm_client
import mattermost_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hermes-agent")

NAMESPACE = os.environ.get("TARGET_NAMESPACE", "gitlab")
DEPLOYMENT = os.environ.get("TARGET_DEPLOYMENT", "gitlab")
CONTAINER = os.environ.get("TARGET_CONTAINER", "gitlab")
LOG_TAIL_LINES = int(os.environ.get("LOG_TAIL_LINES", "200"))

app = Flask(__name__)

# incident_id -> {"alert", "analysis", "pod_name", "status": "open|fixed|cancelled"}
open_incidents: dict[str, dict] = {}
_lock = threading.Lock()


def _pick_pod_name(pod_status: list) -> str | None:
    if not pod_status:
        return None
    running = [p for p in pod_status if p["phase"] == "Running"]
    return (running[0] if running else pod_status[0])["name"]


def format_chat_message(alert: dict, analysis: dict, incident_id: str) -> str:
    labels = alert.get("labels", {})
    remediation = analysis.get("remediation", {})
    return (
        f"### 🚨 {labels.get('alertname', 'Alert')} ({labels.get('severity', 'unknown')})\n\n"
        f"**Причина:** {analysis.get('root_cause')}\n\n"
        f"**Диагноз:** {analysis.get('diagnosis')}\n\n"
        f"**Что предлагается:** {analysis.get('human_summary')}\n\n"
        f"_Действие: `{remediation.get('type')}` | уверенность: {analysis.get('confidence')}_\n\n"
        f"`incident_id: {incident_id}`"
    )


def handle_alert(alert: dict) -> None:
    try:
        log.info("Обрабатываю алерт: %s", alert.get("labels", {}).get("alertname"))

        pod_status = k8s_tools.get_pods_status(NAMESPACE)
        events = k8s_tools.get_events(NAMESPACE)
        pod_name = _pick_pod_name(pod_status)

        logs = ""
        ctl_status = ""
        if pod_name:
            logs = k8s_tools.get_logs(NAMESPACE, pod_name, CONTAINER, LOG_TAIL_LINES)
            ctl_status = k8s_tools.gitlab_ctl_status(NAMESPACE, pod_name, CONTAINER)

        query_text = f"{alert.get('labels', {}).get('alertname')} {alert.get('annotations', {}).get('description', '')}"
        similar = chroma_client.query_similar(query_text)

        analysis = llm_client.analyze_incident(alert, pod_status, events, logs, ctl_status, similar)

        incident_id = str(uuid.uuid4())[:8]
        with _lock:
            open_incidents[incident_id] = {
                "alert": alert,
                "analysis": analysis,
                "pod_name": pod_name,
                "status": "open",
            }

        mattermost_bot.send_incident_message(incident_id, format_chat_message(alert, analysis, incident_id))
        log.info("Инцидент %s создан, ждём подтверждения в Mattermost", incident_id)

    except Exception as e:
        log.exception("Ошибка при обработке алерта")
        mattermost_bot.send_text(f"⚠️ Ошибка агента Hermes при обработке алерта: {e}")


def remediate(incident_id: str) -> str:
    with _lock:
        incident = open_incidents.get(incident_id)
    if not incident or incident["status"] != "open":
        msg = f"Инцидент {incident_id} не найден или уже закрыт."
        mattermost_bot.send_text(msg)
        return msg

    remediation = incident["analysis"].get("remediation", {})
    action_type = remediation.get("type", "manual_only")
    pod_name = incident.get("pod_name")
    result_text = ""

    try:
        if action_type == "restart_gitlab_service" and remediation.get("service") and pod_name:
            result_text = k8s_tools.gitlab_ctl_restart_service(NAMESPACE, pod_name, CONTAINER, remediation["service"])
        elif action_type == "restart_deployment":
            result_text = k8s_tools.restart_deployment(NAMESPACE, remediation.get("deployment") or DEPLOYMENT)
        elif action_type == "delete_pod":
            target_pod = remediation.get("pod") or pod_name
            result_text = k8s_tools.delete_pod(NAMESPACE, target_pod) if target_pod else "Не найден под для удаления"
        elif action_type == "scale_deployment":
            result_text = k8s_tools.scale_deployment(
                NAMESPACE, remediation.get("deployment") or DEPLOYMENT, remediation.get("replicas") or 1
            )
        else:
            result_text = "Автоматический фикс недоступен - нужно вмешательство человека."

        with _lock:
            incident["status"] = "fixed"

        chroma_client.add_incident(
            description=f"{incident['alert'].get('labels', {}).get('alertname')}: {incident['analysis'].get('root_cause')}",
            metadata={
                "remediation_type": action_type,
                "service": remediation.get("service") or "",
                "outcome": result_text,
            },
        )

        msg = f"✅ Инцидент {incident_id} обработан.\n{result_text}"
        mattermost_bot.send_text(msg)
        log.info("Инцидент %s исправлен: %s", incident_id, result_text)
        return msg

    except Exception as e:
        log.exception("Ошибка при выполнении фикса")
        msg = f"⚠️ Не удалось исправить инцидент {incident_id}: {e}"
        mattermost_bot.send_text(msg)
        return msg


def cancel(incident_id: str) -> str:
    with _lock:
        incident = open_incidents.get(incident_id)
        if incident:
            incident["status"] = "cancelled"
    msg = f"Инцидент {incident_id} оставлен без автоисправления."
    mattermost_bot.send_text(msg)
    return msg


def _most_recent_open_incident() -> str | None:
    with _lock:
        open_ids = [iid for iid, inc in open_incidents.items() if inc["status"] == "open"]
    return open_ids[-1] if open_ids else None


# --------------------------------------------------------------------------
# HTTP-эндпоинты
# --------------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/alert", methods=["POST"])
def alert_webhook():
    """Webhook от Alertmanager (см. monitoring-stack.yaml)."""
    payload = request.get_json(force=True, silent=True) or {}
    for a in payload.get("alerts", []):
        if a.get("status") == "firing":
            threading.Thread(target=handle_alert, args=(a,), daemon=True).start()
        else:
            log.info("Алерт resolved: %s", a.get("labels", {}).get("alertname"))
    return jsonify({"received": len(payload.get("alerts", []))})


@app.route("/mm_action", methods=["POST"])
def mattermost_action():
    """Колбэк от кнопок в сообщении Mattermost (props.attachments.actions.integration.url)."""
    body = request.get_json(force=True, silent=True) or {}
    context = body.get("context", {})
    incident_id = context.get("incident_id")
    action = context.get("action")

    if action == "fix" and incident_id:
        threading.Thread(target=remediate, args=(incident_id,), daemon=True).start()
        update_text = "⏳ Выполняю фикс..."
    elif action == "cancel" and incident_id:
        cancel(incident_id)
        update_text = "❌ Отменено пользователем."
    else:
        update_text = "Неизвестное действие."

    # Mattermost ожидает JSON с (опционально) обновлением исходного поста
    return jsonify({"update": {"message": update_text}})


# --------------------------------------------------------------------------
# Polling Mattermost: обрабатываем текстовую команду "ok" прямо в канале
# --------------------------------------------------------------------------

def mattermost_polling_loop():
    since_ms = int(time.time() * 1000)
    log.info("Стартовал polling Mattermost")
    while True:
        try:
            new_posts, since_ms = mattermost_bot.poll_new_posts(since_ms)
        except Exception:
            log.exception("Ошибка получения сообщений Mattermost")
            time.sleep(5)
            continue

        for post in new_posts:
            text = (post.get("message") or "").strip().lower()
            if text in ("ok", "ок", "окей"):
                incident_id = _most_recent_open_incident()
                if incident_id:
                    threading.Thread(target=remediate, args=(incident_id,), daemon=True).start()
                else:
                    mattermost_bot.send_text("Открытых инцидентов нет.")

        time.sleep(3)


if __name__ == "__main__":
    threading.Thread(target=mattermost_polling_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
