#!/usr/bin/env bash
# Feed the rider's app reviews back into the engine and push the result to the app, without a restart:
#   1. pull reviews from the pod into data/reviews.json
#   2. rebuild data/incidents.json (reviewed verdicts win, rule 0; rules are scored against them)
#   3. print the rules-vs-labels evaluation
#   4. rewrite the rider coaching report from the rider-fault incidents (data/coach.md)
#   5. log incidents, labels and the evaluation to W&B Weave (skipped without weave or a key)
#   6. push the data ConfigMap (the pod sees it within about a minute; its reviews in /tmp are kept)
#   k8s/apply-feedback.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
"$REPO_DIR/k8s/pull-reviews.sh"
python3 "$REPO_DIR/engine/build_incidents.py"
python3 "$REPO_DIR/probe/eval_labels.py"
PY="${WITNESS_PY:-$HOME/.venvs/witness/bin/python}"; [ -x "$PY" ] || PY=python3
"$PY" "$REPO_DIR/engine/coach.py" || echo "coach report skipped"
"$PY" "$REPO_DIR/engine/wandb_sync.py" || echo "W&B sync skipped"
"$REPO_DIR/k8s/update-data.sh"
