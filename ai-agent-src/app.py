import os
import uuid
import logging
import threading
import time

from flask import Flask, request, jsonify

import k8s_tools
import chroma_client
import llm_client
import telegram_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ai-agent")

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
    # Предпочитаем под, который сейчас Running, иначе первый попавшийся
    running = [p for p in pod_status if p["phase"] == "Running"]
    return (running[0] if running else pod_status[0])["name"]


def format_telegram_message(alert: dict, analysis: dict, incident_id: str) -> str:
    labels = alert.get("labels", {})
    remediation = analysis.get("remediation", {})
    return (
        f"🚨 <b>{labels.get('alertname', 'Alert')}</b> ({labels.get('severity', 'unknown')})\n\n"
        f"<b>Причина:</b> {analysis.get('root_cause')}\n\n"
        f"<b>Диагноз:</b> {analysis.get('diagnosis')}\n\n"
        f"<b>Что предлагается:</b> {analysis.get('human_summary')}\n\n"
        f"<i>Действие: {remediation.get('type')} | уверенность: {analysis.get('confidence')}</i>\n\n"
        f"Ответь <b>ok</b> в чат или нажми кнопку ниже, чтобы я исправил это сам.\n"
        f"<code>incident_id: {incident_id}</code>"
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

        telegram_bot.send_incident_message(incident_id, format_telegram_message(alert, analysis, incident_id))
        log.info("Инцидент %s создан, ждём подтверждения в Telegram", incident_id)

    except Exception as e:
        log.exception("Ошибка при обработке алерта")
        telegram_bot.send_text(f"⚠️ Ошибка агента при обработке алерта: {e}")


def remediate(incident_id: str) -> None:
    with _lock:
        incident = open_incidents.get(incident_id)
    if not incident or incident["status"] != "open":
        telegram_bot.send_text(f"Инцидент {incident_id} не найден или уже закрыт.")
        return

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

        # Сохраняем инцидент+фикс в базу знаний, чтобы агент "учился" на будущее
        chroma_client.add_incident(
            description=f"{incident['alert'].get('labels', {}).get('alertname')}: {incident['analysis'].get('root_cause')}",
            metadata={
                "remediation_type": action_type,
                "service": remediation.get("service") or "",
                "outcome": result_text,
            },
        )

        telegram_bot.send_text(f"✅ Инцидент {incident_id} обработан.\n{result_text}")
        log.info("Инцидент %s исправлен: %s", incident_id, result_text)

    except Exception as e:
        log.exception("Ошибка при выполнении фикса")
        telegram_bot.send_text(f"⚠️ Не удалось исправить инцидент {incident_id}: {e}")


def cancel(incident_id: str) -> None:
    with _lock:
        incident = open_incidents.get(incident_id)
        if incident:
            incident["status"] = "cancelled"
    telegram_bot.send_text(f"Инцидент {incident_id} оставлен без автоисправления.")


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
    """Webhook от Alertmanager (см. манифест monitoring-stack.yaml)."""
    payload = request.get_json(force=True, silent=True) or {}
    for a in payload.get("alerts", []):
        if a.get("status") == "firing":
            threading.Thread(target=handle_alert, args=(a,), daemon=True).start()
        else:
            log.info("Алерт resolved: %s", a.get("labels", {}).get("alertname"))
    return jsonify({"received": len(payload.get("alerts", []))})


# --------------------------------------------------------------------------
# Long polling Telegram: обрабатываем кнопки и текстовую команду "ok"
# --------------------------------------------------------------------------

def telegram_polling_loop():
    offset = None
    log.info("Стартовал polling Telegram")
    while True:
        try:
            updates = telegram_bot.get_updates(offset)
        except Exception:
            log.exception("Ошибка получения обновлений Telegram")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1

            if "callback_query" in update:
                cq = update["callback_query"]
                data = cq.get("data", "")
                if data.startswith("fix:"):
                    incident_id = data.split(":", 1)[1]
                    telegram_bot.answer_callback_query(cq["id"], "Выполняю фикс...")
                    threading.Thread(target=remediate, args=(incident_id,), daemon=True).start()
                elif data.startswith("cancel:"):
                    incident_id = data.split(":", 1)[1]
                    telegram_bot.answer_callback_query(cq["id"], "Отменено")
                    cancel(incident_id)

            elif "message" in update:
                text = (update["message"].get("text") or "").strip().lower()
                if text in ("ok", "ок", "окей", "/ok"):
                    incident_id = _most_recent_open_incident()
                    if incident_id:
                        threading.Thread(target=remediate, args=(incident_id,), daemon=True).start()
                    else:
                        telegram_bot.send_text("Открытых инцидентов нет.")


if __name__ == "__main__":
    threading.Thread(target=telegram_polling_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
