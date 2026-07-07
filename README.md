# GitOps + AIOps демо на Minikube (агент Hermes, open-source чат)

Стек: **ArgoCD** (GitOps) → **kube-prometheus-stack** (Prometheus/Alertmanager/Grafana) →
**GitLab CE** (ломаемое приложение) → **агент Hermes** (Flask + открытая модель Hermes 3
через Ollama + ChromaDB) → **Mattermost** (self-hosted чат вместо Telegram).

Всё, включая LLM и чат, крутится внутри самого кластера - никаких внешних облачных
сервисов не требуется.

Сценарий: ты ломаешь внутренний сервис GitLab (например `gitlab-ctl stop postgresql`) →
Prometheus видит, что под не Ready / крашится → Alertmanager шлёт webhook агенту →
агент собирает `kubectl`-состояние, логи, события и `gitlab-ctl status`, ищет похожие
инциденты в ChromaDB, просит Hermes (локальную LLM) поставить диагноз и предложить фикс →
пишет в Mattermost → на "ok" (текстом или кнопкой) сам выполняет исправление и
запоминает результат в ChromaDB.

---

## 0. Структура репозитория

```
argocd/                    # Application-манифесты ArgoCD
  app-of-apps.yaml
  apps/                     # monitoring / gitlab / chromadb / ollama / chat / hermes-agent
manifests/
  monitoring/               # PrometheusRule с алертами на GitLab
  gitlab/                    # Deployment/Service/PVC GitLab CE (ломаемое приложение)
  chromadb/                  # Deployment/Service/PVC ChromaDB (база знаний)
  ollama/                     # Deployment/Service/PVC Ollama + Job, качающий модель Hermes 3
  chat/                        # Postgres + Mattermost (open-source чат)
  hermes-agent/                 # RBAC/ConfigMap/Deployment/Service агента + seed Job
hermes-agent-src/           # исходники агента (Python), из них собирается образ hermes-agent:latest
secrets-templates/          # шаблон Secret с токеном бота Mattermost (не коммитить с реальными значениями)
```

## 1. Подготовка Minikube

Ollama с моделью Hermes 3 (8B, CPU-инференс) прожорлива по ресурсам - закладывай с запасом:

```bash
minikube start --cpus=6 --memory=16384 --disk-size=30g
alias kubectl='./minikube kubectl --'
```

Если на хосте есть GPU и он проброшен в minikube (`--driver=docker --gpus=all`),
инференс будет на порядок быстрее - тогда можно уменьшить `resources` в
`manifests/ollama/deployment.yaml` и добавить `nvidia.com/gpu` в лимиты.

## 2. Установка ArgoCD

```bash
kubectl apply -f argocd/namespace.yaml
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl -n argocd wait --for=condition=available deploy/argocd-server --timeout=180s
kubectl -n argocd port-forward svc/argocd-server 8080:443
```

Пароль администратора:
```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
```

## 3. Публикация репозитория

Запушь содержимое папки в свой git и подставь адрес репозитория:

```bash
grep -rl "your-user" argocd/ | xargs sed -i 's#<your-user>#ТВОЙ_ЮЗЕР#g'
```

## 4. Секрет для агента (токен бота Mattermost)

Секрет создаётся в два этапа: сначала Mattermost должен подняться и получить бота,
потом заполняем secrets-templates и применяем вручную (не через ArgoCD - шаблон
специально лежит вне синкаемых путей).

```bash
kubectl create namespace hermes-agent --dry-run=client -o yaml | kubectl apply -f -
cp secrets-templates/hermes-agent-secret.example.yaml secrets-templates/hermes-agent-secret.yaml
# заполнишь после шага 6 (настройка Mattermost)
```

## 5. Сборка и загрузка образа агента в Minikube

```bash
cd hermes-agent-src
docker build -t hermes-agent:latest .
cd ..
minikube image load hermes-agent:latest
```

После любых изменений кода агента повторить сборку/загрузку и
`kubectl -n hermes-agent rollout restart deploy/hermes-agent`.

## 6. Разворачиваем всё через GitOps

```bash
kubectl apply -f argocd/app-of-apps.yaml
```

ArgoCD поднимет:
- `kube-prometheus-stack` в `monitoring`, Alertmanager смотрит на
  `http://hermes-agent.hermes-agent.svc.cluster.local:8080/alert`;
- `PrometheusRule` с алертами на GitLab;
- GitLab CE в `gitlab` (первый старт 3-5 минут);
- ChromaDB в `chromadb` + разовая затравка примерами инцидентов;
- Ollama в `ollama` + Job, который качает модель `hermes3` (~4-5GB, может занять
  10-20 минут на первом запуске - см. прогресс: `kubectl -n ollama logs job/ollama-pull-hermes -f`);
- Postgres + Mattermost в `chat`;
- сам агент в `hermes-agent` (не заработает до появления Secret из шага 4/7).

### Настройка Mattermost (один раз)

```bash
kubectl -n chat port-forward svc/mattermost 8065:8065
```

1. Открой `http://localhost:8065`, пройди мастер первичной настройки (создай админа и команду).
2. Создай публичный канал, например `#gitlab-incidents`.
3. В **System Console → Integrations → Bot Accounts** создай бота (например `hermes`),
   выдай ему **Personal Access Token** - это и есть `MATTERMOST_BOT_TOKEN`.
4. Добавь бота в канал `#gitlab-incidents` (через `/invite @hermes` или UI).
5. Узнай ID канала: в веб-клиенте открой канал → **View Info** → внизу будет Channel ID,
   либо через API:
   ```bash
   curl -s -H "Authorization: Bearer <твой личный токен>" \
     http://localhost:8065/api/v4/teams/<team_id>/channels/name/gitlab-incidents | jq .id
   ```
6. Заполни `secrets-templates/hermes-agent-secret.yaml` полученными `MATTERMOST_BOT_TOKEN`
   и `MATTERMOST_CHANNEL_ID`, примени:
   ```bash
   kubectl apply -f secrets-templates/hermes-agent-secret.yaml
   kubectl -n hermes-agent rollout restart deploy/hermes-agent
   ```

Если Integrations → Bot Accounts недоступны - включи их в
**System Console → Integrations → Integration Management → Enable Bot Account Creation**.

Проверить статус компонентов:
```bash
kubectl -n gitlab get pods -w
kubectl -n ollama logs -f deploy/ollama
kubectl -n hermes-agent logs -f deploy/hermes-agent
```

## 7. Демонстрация

Дождись, пока GitLab станет `Ready`:
```bash
kubectl -n gitlab exec deploy/gitlab -- gitlab-ctl status
```

Ломаем внутренний сервис:
```bash
kubectl -n gitlab exec deploy/gitlab -- gitlab-ctl stop postgresql
```

Дальше:
1. Prometheus фиксирует `GitLabPodCrashLooping` / `GitLabPodNotReady` за 1-2 минуты.
2. Alertmanager дёргает `/alert` у агента.
3. Агент собирает состояние подов, события, логи, `gitlab-ctl status`, ищет похожие
   случаи в ChromaDB, зовёт Hermes (Ollama) - и пишет в канал `#gitlab-incidents`
   диагноз и предложенный фикс, с кнопками **✅ OK, исправить** / **❌ Не трогать**.
4. Отвечаешь **`ok`** текстом в канал (или жмёшь кнопку) - агент сам выполняет
   предложенное действие (например `gitlab-ctl restart postgresql`) и присылает результат.
5. Фикс сохраняется в ChromaDB - при следующем похожем инциденте Hermes это учтёт.

Другие варианты поломки: `gitlab-ctl stop redis`, `gitlab-ctl stop sidekiq`,
`kubectl -n gitlab delete pod <pod>` - под каждый сработает свой алерт.

Grafana (admin/admin):
```bash
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
```

## 8. Важные ограничения демо

- RBAC агента ограничен namespace `gitlab` (см. `manifests/hermes-agent/serviceaccount-rbac.yaml`).
- `gitlab-ctl` вызывается через `pods/exec`, без дополнительных сервисов вроде SSH.
- Postgres для Mattermost - демо-креды прямо в манифесте; для не-демо использования
  вынеси в Secret / SealedSecrets / External Secrets Operator.
- Hermes 3 (8B, квантованная) на CPU отвечает не мгновенно - закладывай десятки секунд
  на анализ одного инцидента (таймаут в `llm_client.py` выставлен в 600 секунд).
- Если хочешь модель полегче/побыстрее - смени `OLLAMA_MODEL` в
  `manifests/hermes-agent/configmap.yaml` на другой тег из библиотеки Ollama и
  соответствующим образом поправь `pull-hermes-job.yaml`.
