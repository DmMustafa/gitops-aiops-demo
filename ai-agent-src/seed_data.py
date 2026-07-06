"""
Разово заполняет ChromaDB примерами прошлых инцидентов, чтобы на первом же
реальном алерте у агента уже было с чем сравнивать (RAG).
Запускается как Job (см. manifests/ai-agent/seed-job.yaml).
"""
import chroma_client

EXAMPLES = [
    {
        "description": "GitLabPodCrashLooping: postgresql внутри omnibus-контейнера не стартует, "
                        "в логах 'FATAL: could not create shared memory segment: No space left on device'",
        "metadata": {
            "remediation_type": "restart_gitlab_service",
            "service": "postgresql",
            "outcome": "Освобождено место на /dev/shm, postgresql перезапущен через gitlab-ctl restart postgresql, помогло.",
        },
    },
    {
        "description": "GitLabDeploymentReplicasUnavailable: под gitlab в CrashLoopBackOff сразу после старта, "
                        "gitlab-ctl status показывает redis down, ошибка подключения к redis в puma-логах",
        "metadata": {
            "remediation_type": "restart_gitlab_service",
            "service": "redis",
            "outcome": "gitlab-ctl restart redis решил проблему, puma переподключился и под стал Ready.",
        },
    },
    {
        "description": "GitLabPodNotReady: readiness-проба /-/readiness падает, sidekiq завис из-за "
                        "переполненной очереди задач после массового импорта репозиториев",
        "metadata": {
            "remediation_type": "restart_gitlab_service",
            "service": "sidekiq",
            "outcome": "Перезапуск sidekiq очистил зависшие воркеры, очередь начала обрабатываться.",
        },
    },
    {
        "description": "GitLabPodDown: под завис в Terminating/Unknown из-за проблем с узлом minikube, "
                        "kubelet не может достучаться до контейнера",
        "metadata": {
            "remediation_type": "delete_pod",
            "service": "",
            "outcome": "Принудительное удаление пода привело к пересозданию deployment-ом, под поднялся заново.",
        },
    },
]

if __name__ == "__main__":
    for ex in EXAMPLES:
        doc_id = chroma_client.add_incident(ex["description"], ex["metadata"])
        print(f"seeded: {doc_id} -> {ex['description'][:60]}...")
    print(f"Готово, добавлено {len(EXAMPLES)} примеров инцидентов в ChromaDB.")
