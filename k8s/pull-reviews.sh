#!/usr/bin/env bash
# Copy the rider's reviews out of the running app pod into data/reviews.json, the engine's feedback input.
# The pod keeps reviews in /tmp, which is wiped on every restart, so deploy-app.sh runs this first.
# Pod reviews win per incident; reviews already in data/reviews.json but not in the pod are kept.
# Each review is tagged with its segment so engine/build_incidents.py can match it after ids change.
#   k8s/pull-reviews.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO_DIR/data/reviews.json"
APP_NAME=witness-app

mapfile -t TEAM_CONFIGS < <(find /config -maxdepth 1 -type f -name '*.config' | sort)
(( ${#TEAM_CONFIGS[@]} == 1 )) || { echo "expected exactly one /config/*.config"; exit 1; }
NS=$(grep '^USERNAME=' "${TEAM_CONFIGS[0]}" | cut -d= -f2-)
export KUBECONFIG="/config/${NS}-k8s.yaml"

POD=$(kubectl -n "$NS" get pod -l app="$APP_NAME" --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [[ -z "$POD" ]]; then
  echo "no running $APP_NAME pod; keeping $OUT as is"
  exit 0
fi

POD_JSON="$(mktemp)"
trap 'rm -f "$POD_JSON"' EXIT
kubectl -n "$NS" exec "$POD" -- sh -c 'cat /tmp/reviews.json 2>/dev/null || echo "{}"' > "$POD_JSON"

python3 - "$POD_JSON" "$OUT" "$REPO_DIR/data/incidents.json" <<'EOF'
import json, os, sys
pod_path, out, incidents_path = sys.argv[1:]
pod = json.load(open(pod_path))
merged = json.load(open(out)) if os.path.exists(out) else {}
source = {i['id']: i['source'] for i in json.load(open(incidents_path))['incidents']} if os.path.exists(incidents_path) else {}
new = sum({f: x for f, x in merged.get(k, {}).items() if f != 'segment'} != v for k, v in pod.items())
for incident_id, review in pod.items():
    seg = source.get(incident_id) or merged.get(incident_id, {}).get('segment')
    merged[incident_id] = {**review, **({'segment': seg} if seg else {})}
json.dump(merged, open(out, 'w'), indent=2, sort_keys=True)
open(out, 'a').write('\n')
print(f"pulled {len(pod)} reviews from the pod ({new} new or changed); {len(merged)} in {os.path.relpath(out)}")
EOF
