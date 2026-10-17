#!/usr/bin/env bash
# Push the app's data files (data/segments.json, screen.json, incidents.json) to its data ConfigMap.
# The pod sees the new files within about a minute; no redeploy or restart needed.
#   k8s/update-data.sh                  # whichever of those exist in data/
#   k8s/update-data.sh path/screen.json # copy a fresh engine output into data/ first
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$REPO_DIR/data"
APP_NAME=witness-app

KNOWN="segments.json screen.json incidents.json"
for f in "$@"; do
  [[ " $KNOWN " == *" $(basename "$f") "* ]] || { echo "$f: the app only reads $KNOWN (rename it)"; exit 1; }
  cp "$f" "$DATA_DIR/"
done

mapfile -t TEAM_CONFIGS < <(find /config -maxdepth 1 -type f -name '*.config' | sort)
(( ${#TEAM_CONFIGS[@]} == 1 )) || { echo "expected exactly one /config/*.config"; exit 1; }
NS=$(grep '^USERNAME=' "${TEAM_CONFIGS[0]}" | cut -d= -f2-)
export KUBECONFIG="/config/${NS}-k8s.yaml"

# Only what the app reads: engine-only files (geometry.json, ...) would push the ConfigMap past 1 MiB.
FILES=()
for name in $KNOWN; do
  if [[ -f "$DATA_DIR/$name" ]]; then FILES+=("$DATA_DIR/$name"); fi
done
(( ${#FILES[@]} > 0 )) || { echo "none of $KNOWN in data/"; exit 1; }
TOTAL=$(cat "${FILES[@]}" | wc -c)
(( TOTAL < 1000000 )) || { echo "data is $TOTAL bytes; a ConfigMap holds at most 1 MiB"; exit 1; }
for f in "${FILES[@]}"; do python3 -m json.tool "$f" >/dev/null || { echo "invalid JSON: $f"; exit 1; }; done

ARGS=(); for f in "${FILES[@]}"; do ARGS+=(--from-file="$f"); done
kubectl -n "$NS" create configmap "${APP_NAME}-data" "${ARGS[@]}" \
  --dry-run=client -o yaml | kubectl apply --server-side --force-conflicts -f -
echo "pushed: ${FILES[*]##*/}"
