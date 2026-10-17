#!/usr/bin/env bash
# Deploy app/ to the team namespace at /app (deploy-app-no-registry skill):
# python:3.12-slim + code ConfigMap + VSS creds Secret + Deployment/Service/Ingress.
# Re-run after any change to app/ (it updates the ConfigMap and restarts the pod).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$REPO_DIR/app"
APP_NAME=witness-app
APP_PORT=8080

mapfile -t TEAM_CONFIGS < <(find /config -maxdepth 1 -type f -name '*.config' | sort)
(( ${#TEAM_CONFIGS[@]} == 1 )) || { echo "expected exactly one /config/*.config"; exit 1; }
TEAM_CONFIG="${TEAM_CONFIGS[0]}"
TEAM_USER=$(grep '^USERNAME=' "$TEAM_CONFIG" | cut -d= -f2-)
TEAM_PASS=$(grep '^PASSWORD=' "$TEAM_CONFIG" | cut -d= -f2-)
NS="$TEAM_USER"
APP_HOST="video-lab-team-${TEAM_USER#team-}.cosmos.vastdata.com"
export KUBECONFIG="/config/${NS}-k8s.yaml"

# The restart below wipes the pod's reviews, so save them first; data/reviews.json then seeds REVIEWS_PATH.
"$REPO_DIR/k8s/pull-reviews.sh"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
# Keep the ConfigMap small and free of local device state / bytecode.
shopt -s nullglob
for f in "$APP_DIR"/*.py "$APP_DIR"/*.json "$APP_DIR"/*.html "$APP_DIR"/requirements.txt; do
  case "$(basename "$f")" in
    devices.json|reviews.json) continue ;;
  esac
  cp "$f" "$STAGE/"
done
if [[ -f "$REPO_DIR/data/reviews.json" ]]; then cp "$REPO_DIR/data/reviews.json" "$STAGE/reviews.json"; fi

kubectl -n "$NS" create configmap "${APP_NAME}-code" --from-file="$STAGE" \
  --dry-run=client -o yaml | kubectl apply -f -
"$REPO_DIR/k8s/update-data.sh"

# In-cluster backend: same API as $INGRESS_URL, without the round trip through the Ingress.
kubectl -n "$NS" create secret generic "${APP_NAME}-vss-creds" \
  --from-literal=VSS_URL="http://video-backend-service:8000" \
  --from-literal=VSS_USERNAME="$TEAM_USER" \
  --from-literal=VSS_PASSWORD="$TEAM_PASS" \
  --dry-run=client -o yaml | kubectl apply -f -

# Optional mount: team.config for VastDB reads when present.
EXTRA_MOUNTS=""
EXTRA_VOLUMES=""
if kubectl -n "$NS" get secret witness-config >/dev/null 2>&1; then
  EXTRA_MOUNTS+=$'\n        - {name: team-config, mountPath: /config, readOnly: true}'
  EXTRA_VOLUMES+=$'\n      - {name: team-config, secret: {secretName: witness-config, optional: true}}'
fi

kubectl -n "$NS" apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${APP_NAME}
  labels: {app: ${APP_NAME}}
spec:
  replicas: 1
  selector:
    matchLabels: {app: ${APP_NAME}}
  template:
    metadata:
      labels: {app: ${APP_NAME}}
    spec:
      containers:
      - name: app
        image: python:3.12-slim
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: ${APP_PORT}
        env:
        - {name: PORT, value: "${APP_PORT}"}
        - {name: BASE_PATH, value: "/app"}
        - {name: REVIEWS_PATH, value: "/tmp/reviews.json"}
        - {name: DEVICES_PATH, value: "/tmp/devices.json"}
        - {name: DATA_DIR, value: "/data"}
        - {name: PYTHONUNBUFFERED, value: "1"}
        - name: VSS_URL
          valueFrom: {secretKeyRef: {name: ${APP_NAME}-vss-creds, key: VSS_URL}}
        - name: VSS_USERNAME
          valueFrom: {secretKeyRef: {name: ${APP_NAME}-vss-creds, key: VSS_USERNAME}}
        - name: VSS_PASSWORD
          valueFrom: {secretKeyRef: {name: ${APP_NAME}-vss-creds, key: VSS_PASSWORD}}
        volumeMounts:
        - {name: code, mountPath: /code}
        - {name: data, mountPath: /data}${EXTRA_MOUNTS}
        workingDir: /code
        command: ["bash", "-c"]
        args:
        - |
          set -euo pipefail
          if [ -f requirements.txt ]; then pip install --no-cache-dir -q -r requirements.txt || true; fi
          exec python main.py
        readinessProbe:
          httpGet: {path: /health, port: ${APP_PORT}}
          initialDelaySeconds: 20
          periodSeconds: 10
          failureThreshold: 18
      volumes:
      - name: code
        configMap: {name: ${APP_NAME}-code}
      - name: data
        configMap: {name: ${APP_NAME}-data, optional: true}${EXTRA_VOLUMES}
---
apiVersion: v1
kind: Service
metadata:
  name: ${APP_NAME}
  labels: {app: ${APP_NAME}}
spec:
  selector: {app: ${APP_NAME}}
  ports:
  - {name: http, port: 80, targetPort: ${APP_PORT}}
  type: ClusterIP
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${APP_NAME}
  labels: {app: ${APP_NAME}}
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /\$2
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "300"
spec:
  ingressClassName: nginx
  rules:
  - host: ${APP_HOST}
    http:
      paths:
      - path: /app(/|\$)(.*)
        pathType: ImplementationSpecific
        backend:
          service:
            name: ${APP_NAME}
            port: {number: 80}
EOF

kubectl -n "$NS" rollout restart deploy/"$APP_NAME"
kubectl -n "$NS" rollout status deploy/"$APP_NAME" --timeout=300s
kubectl -n "$NS" get pods,svc,ingress -l app="$APP_NAME"
echo "health: $(curl -sS "http://${APP_HOST}/app/health")"
echo "Open https://workshop.thecosmoslabs.com and click App."
echo "Public: https://team-1-app.thecosmoslabs.com/app/"
