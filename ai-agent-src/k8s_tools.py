"""
Обёртка над Kubernetes Python client.
Внутри кластера используется in-cluster config (ServiceAccount ai-agent),
для локального запуска/дебага - fallback на kubeconfig
(с minikube это будет то, что настраивает `./minikube kubectl -- config view`,
но сам агент внутри кластера НЕ использует minikube-обёртку, он ходит напрямую в API).
"""
import logging
from datetime import datetime, timezone

from kubernetes import client, config
from kubernetes.stream import stream

log = logging.getLogger("k8s_tools")

try:
    config.load_incluster_config()
    log.info("Загружен in-cluster kubeconfig")
except Exception:
    config.load_kube_config()
    log.info("Загружен локальный kubeconfig (режим разработки)")

core_v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()


def get_pods_status(namespace: str) -> list[dict]:
    pods = core_v1.list_namespaced_pod(namespace)
    result = []
    for p in pods.items:
        container_statuses = []
        for cs in (p.status.container_statuses or []):
            state = "unknown"
            reason = None
            if cs.state.running:
                state = "running"
            elif cs.state.waiting:
                state = "waiting"
                reason = cs.state.waiting.reason
            elif cs.state.terminated:
                state = "terminated"
                reason = cs.state.terminated.reason
            container_statuses.append({
                "name": cs.name,
                "ready": cs.ready,
                "restart_count": cs.restart_count,
                "state": state,
                "reason": reason,
            })
        result.append({
            "name": p.metadata.name,
            "phase": p.status.phase,
            "node": p.spec.node_name,
            "containers": container_statuses,
        })
    return result


def get_events(namespace: str, limit: int = 20) -> list[dict]:
    events = core_v1.list_namespaced_event(namespace)
    items = sorted(
        events.items,
        key=lambda e: e.last_timestamp or e.event_time or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:limit]
    return [
        {
            "type": e.type,
            "reason": e.reason,
            "object": f"{e.involved_object.kind}/{e.involved_object.name}",
            "message": e.message,
            "count": e.count,
            "last_seen": str(e.last_timestamp or e.event_time),
        }
        for e in items
    ]


def get_logs(namespace: str, pod_name: str, container: str, tail_lines: int = 200) -> str:
    try:
        return core_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container,
            tail_lines=tail_lines,
            timestamps=True,
        )
    except client.exceptions.ApiException as e:
        return f"[не удалось получить логи: {e}]"


def exec_in_pod(namespace: str, pod_name: str, container: str, command: list[str]) -> str:
    """Выполняет команду внутри контейнера (аналог kubectl exec) и возвращает stdout+stderr."""
    try:
        resp = stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container=container,
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=True,
        )
        return resp
    except Exception as e:
        return f"[exec failed: {e}]"


def gitlab_ctl_status(namespace: str, pod_name: str, container: str) -> str:
    """Статус внутренних сервисов gitlab-ctl (postgresql, redis, puma, sidekiq, gitaly...)."""
    return exec_in_pod(namespace, pod_name, container, ["gitlab-ctl", "status"])


def restart_deployment(namespace: str, deployment_name: str) -> str:
    """Аналог `kubectl rollout restart deployment` - патчим аннотацию в темплейте подов."""
    now = datetime.now(timezone.utc).isoformat()
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"kubectl.kubernetes.io/restartedAt": now}
                }
            }
        }
    }
    apps_v1.patch_namespaced_deployment(deployment_name, namespace, patch)
    return f"Deployment {deployment_name} перезапущен (rollout restart) в {now}"


def delete_pod(namespace: str, pod_name: str) -> str:
    core_v1.delete_namespaced_pod(pod_name, namespace)
    return f"Pod {pod_name} удалён, будет пересоздан контроллером"


def scale_deployment(namespace: str, deployment_name: str, replicas: int) -> str:
    apps_v1.patch_namespaced_deployment_scale(
        deployment_name, namespace, {"spec": {"replicas": replicas}}
    )
    return f"Deployment {deployment_name} масштабирован до {replicas} реплик"


def gitlab_ctl_restart_service(namespace: str, pod_name: str, container: str, service: str) -> str:
    """Перезапуск конкретного внутреннего сервиса GitLab (например postgresql, redis, puma, sidekiq)."""
    return exec_in_pod(namespace, pod_name, container, ["gitlab-ctl", "restart", service])
