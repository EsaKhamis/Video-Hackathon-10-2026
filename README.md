# Witness: your helmet cam is a witness, and a coach

VAST Builders Challenge, London, Sat 17 Oct 2026.

Cyclists record close passes every day, and almost none get reported: finding the 5 seconds that matter
in an hour of footage and filling in a police form takes too long. And nobody tells riders about the
risks they take themselves.

Witness watches a cyclist's ride footage, finds every risky moment and works out who caused it.

| Who was at fault | What the agent writes | Who sees it |
|---|---|---|
| Driver | **Authority report**: police-form fields, clip, vehicle, blank plate and street for the cyclist to fill in | Police / 311 / insurer, **only if the cyclist exports it** (Send is a dummy outbox) |
| Rider | **Rider lesson**: what happened, why it was risky, what to do next time | The cyclist only. Never sent anywhere |
| Unclear | Both, side by side | The cyclist decides |

**Try it:** the App button on workshop.thecosmoslabs.com (`/app`). Open a ride, then an incident: the clip
plays with the detection boxes drawn over it, the facts behind the verdict are listed, and you can approve,
edit, dismiss or change the fault, then open the report or lesson.

![Black SUV close pass (H1)](docs/images/near-miss/black_suv_close_pass.jpg)
![Taxi crossing on red: a rider lesson (H3)](docs/images/near-miss/taxi_crossing_signals_red.jpg)

## How it works: the models see, the rules decide

```
465 helmet-cam segments (3 NYC rides, VAST DataEngine index)
  │
  ├─ A  Cosmos3-Reason watches every 5 s clip (1280 px, 6 fps)  ─ near miss? signal? rider position?
  ├─ B  YOLO11 boxes from the pipeline → geometry               ─ how close, which side, entered the rider's corridor?
  ├─ C  Cosmos again on A ∪ B flags, 5 calls, majority vote     ─ + full-resolution still for the signal
  ├─ D  Plain-Python fault rules (no LLM)                       ─ driver / rider / unclear, plus severity
  └─ E  W&B Inference (gpt-oss-120b) writes the report or lesson from the facts
        │
        └─ data/incidents.json → stdlib web app on K8s (/app); video and boxes proxied from the VSS backend
```

- **Cosmos is good at context and fault** (the signal, the rider on the sidewalk, what happened) and at
  rejecting false alarms, but it is conservative about distance.
- **YOLO is good at distance and side**, but it can't tell a car in the next lane from a car cutting in.
- **So a driver verdict needs both:** a YOLO close-pass shape **and** at least 4 of 5 Cosmos calls saying
  the vehicle entered the rider's path, with no separator, no red signal and the rider not on the sidewalk.
  Missing or unknown facts never produce a driver verdict. A wrong "driver" puts a false accusation in front
  of the police, so it is the error we try hardest to avoid.
- Rider rules run first: entered on red (video **and** still), riding on the sidewalk, changing line into traffic.
- Cosmos's own `fault` answer is stored for comparison and never used.

## Results

On the 18 hand-labelled clips (`probe/labels.csv`, scored by `probe/eval_labels.py`):
**12 of 18 agree with the label, and no rider, adjacent-lane or separated clip gets a driver verdict.**
All five hero clips match: H1 and H4 driver, H3 rider (entered on red, 5/5 votes), H5 rider (sidewalk),
H2 (a van in the next lane) dropped.

Across all 465 segments: 80 incidents (13 driver, 25 rider, 42 unclear for a human).
How the near-miss definition was tuned, round by round: [PLAN.md](PLAN.md) ("Near-miss definition") and
[definition_log.md](definition_log.md).

## Honest limits

- Closeness is estimated from box size, not measured distance.
- Tuned and scored on a small hand-labelled set (18 clips), not a benchmark.
- Most driver verdicts outside the hero clips have not been checked by eye yet; the cyclist reviews every one.
- Cosmos is unreliable about vehicle colour, so colour and plate are always left for the cyclist to confirm.
- The archive has no recording date or GPS; reports say "cyclist to confirm".
- No face blurring in exported frames yet.

## Built on

VAST DataEngine + VastDB (index, YOLO detections, video), NVIDIA Cosmos3-Reason (screening and fault facts),
YOLO11 (geometry), W&B Inference `openai/gpt-oss-120b` (report and lesson text), Kubernetes on the VAST
runtime, Cursor and Claude Code.

## Repo

| Path | What |
|---|---|
| `engine/detect.py` | Stage B: YOLO geometry |
| `engine/confirm.py` | Stage C: Cosmos confirmation with majority vote |
| `engine/fault.py`, `engine/build_incidents.py` | Stage D: fault rules, severity, `data/incidents.json` |
| `engine/report.py` | Stage E: W&B writes the reports and lessons |
| `app/main.py` | Web app (stdlib `http.server`): rides, incidents, review, report, lesson |
| `k8s/` | `deploy-app.sh` (deploy to `/app`), `update-data.sh` (push new data) |
| `probe/` | Exploration, labelled set, evaluation |
| `PLAN.md` | Full plan, fault rules, prompts and changelog |

Rebuild and publish: see [engine/README.md](engine/README.md).

## Team

_names + emails_
