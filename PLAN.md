# Plan: Your helmet cam is a witness

**Pitch:** An agent watches a cyclist's ride footage, finds the drivers who put them at risk,
and hands them a police-ready incident report to review. The cyclist decides what gets sent.

**Deadline:** 15:45 code freeze, demo-ready (started 11:45). Submissions open around 16:30
([tokensand.com/vastlondon](https://tokensand.com/vastlondon), "Submit your project").

**Demo spine:** a judge opens the App button on workshop.thecosmoslabs.com and sees one helmet-cam ride
with its incidents ranked by severity. They open the top close pass, watch the clip with detection boxes
drawn over it, read the model's confirmation, type a plate into the editable field, click
Approve, and get a printable report laid out like a police submission form.

Order of work: **rider (GoPro) data first**. The **Toronto dashcam witness** feed is the
first Should item, added once the spine is deployed.

## Data check

Live check at 11:50: login OK, dashboard `healthy: true`, `pending_index: 0`, 3,537 segments indexed.

| Need | Camera / source | Status | Evidence or action |
|---|---|---|---|
| Helmet-cam rides | `nyc_bike_gopro-1` (465 segments, 3 rides: GOPR0130, GX010001, GX050001) | Present | Dashboard `camera_id` counts; rows already dumped to `~/bike-probe/gopro_all.json` |
| Per-frame vehicle boxes | `/api/v1/videos/detections` | Present | Used by the probes; car/truck/bus/bicycle/person at 30 fps |
| Close pass / cut-in wording in descriptions | GoPro descriptions | Missing (generic) | Searching for "car passing very close to the cyclist" or "car swerving across in front" returns similarity ~0.4 with generic scene text. **Detections drive detection; descriptions are only supporting text.** Optional small re-ingest (Should tier) |
| Blocked bike lane | GoPro descriptions | Present | "vehicle stopped in the bike lane" hits GOPR0130 chunk 0001/0002 with "vehicle is stopped on..." text; 76 descriptions mention blocking |
| Vehicle type/colour confirmation | Cosmos3-Reason via the `gpu-model-smoke-test` call pattern | Present | `plate.py` / `dash2.py` already call it with frames |
| Plates | Helmet cam | Absent (unreliable) | Model hallucinated "1138". Cyclist types it in; never auto-filled |
| Plates | Toronto dashcam `pie_cam-3` | Present (needs a human to confirm) | 1 of 3 read correctly (`BEVX 388`); show the crop and let the human confirm it |
| Dashcam witness candidates | `pie_cam-3` | Present | `~/bike-probe/dash_close.json`: 24 segments with a ridden cyclist and a vehicle within half a bike-width |
| Report writing | W&B Inference | Present | `/v1/models` reachable; `openai/gpt-oss-120b`, `deepseek-ai/DeepSeek-V4-Flash-0731` available |
| GPS / street | none | Absent | Cyclist fills in the street; city comes from `location` |

## Scope

- **Must (demo spine):**
  1. `engine/detect.py`: event-shape rules over GoPro detections, producing a ranked candidate list.
  2. `engine/confirm.py`: top ~30 candidates are each sent to Cosmos3-Reason as 3 frames, which returns
     structured JSON (confirmed, event type, vehicle, colour, side). Candidates it doesn't confirm are dropped.
  3. `engine/report.py`: a W&B LLM writes a summary for each incident and fills the form fields.
     The output is `app/incidents.json`.
  4. Web app: ride list, then incident list, then incident detail (clip plus box overlay,
     approve / edit / dismiss), then printable report.
  5. Deployed at `/app` with `deploy-app-no-registry`.
- **Should:**
  1. **Dashcam witness feed** (Toronto `pie_cam-3`): the same confirm and report steps over
     the 24 candidates in `dash_close.json`, plus a plate crop drawn client-side from the box.
     Shown as a second "Witness: Toronto dashcam" source.
  2. Severity timeline strip per ride (dots along the ride, coloured by event type).
  3. Lead-up and aftermath: "previous segment" and "next segment" buttons on the player.
  4. Re-ingest 3–5 hero segments with a cyclist-safety prompt, so a live search for
     "driver passed dangerously close to the cyclist" finds them (good "the archive is now
     searchable for incidents" moment).
- **Could (only if ahead; nobody starts these before 15:00):**
  - Persist review decisions in a custom VastDB table (`vast-database-vastdb-write`).
  - W&B Weave tracing of the confirm/report calls, shown in the pitch.
  - Email-draft (`mailto:`) export with the report body.
- **Out of scope:**
  - Auto-submitting to police or 311 (rule: no automatic real-world actions). "Send" posts to a dummy `/api/submit` outbox.
  - Crash or theft prediction (data: no such footage).
  - Matching the same incident across bike and dashcam (data: NYC vs Toronto, different days). Pitched as independent witnesses that use the same detector.
  - Live detection at demo time (time). Everything is precomputed.
  - Native/mobile app (rule).

## Runtime: what runs in VAST vs locally

Checked at 12:00 from a test pod in `team-1`:

| Capability | VAST runtime (K8s pod in `team-1`) | VM (local) |
|---|---|---|
| Create Jobs / Deployments | Yes, no quota | n/a |
| VSS backend | `http://video-backend-service:8000` (in-cluster, fast) | `$INGRESS_URL` |
| VastDB SDK (`vss-collection`: `reasoning_content`, `perception_json`, `object_counts`, `detection_sidecar_uri`, `camera_id`, ...) | Yes (`pip install vastdb`) | **No**: VM has no `pip` / `venv` |
| Cosmos3-Reason (GPU host :8001), W&B Inference | Reachable | Reachable |
| PyPI + Debian apt (`vastdb`, `ffmpeg`) | Yes | `ffmpeg` installed, stdlib Python only |

**Decision: the engine runs as a K8s Job in the VAST runtime**, reads from VastDB and the in-cluster
backend, and writes incidents to a custom VastDB table `witness-incidents` (`vast-database-vastdb-write`),
plus a JSON copy as a fallback. The app reads that table, so the whole pipeline (data, models, results, app) runs in VAST.
The VM is only for editing code, `kubectl`, and quick `ffmpeg` checks with stdlib scripts.
Fall back to running stdlib scripts on the VM only if a Job is blocked.

Team config is mounted into pods from the Secret `witness-config` (created from `/config/team-1.config`,
mounted at `/config/team.config`), so `vss_client.py` works unchanged and nothing is copied into the repo.

Gotcha: the VastDB SDK logs a harmless "sorted columns ... without elysium" RPC error on table open; ignore it.

## Architecture

```
Engine = K8s Job "witness-engine" in team-1               Deployed app (K8s Deployment, /app)
(python:3.12-slim + apt ffmpeg + pip vastdb)              -----------------------------------
--------------------------------------------              main.py (stdlib http.server, python:3.12-slim)
VastDB vss-collection, camera_id=nyc_bike_gopro-1
   (465 rows: descriptions, object_counts)
   + /api/v1/videos/detections per segment  ──► detect.py   ├─ GET /                 ride list + incidents
     (retrieval-videos skill)                    │          ├─ GET /incident/<id>    player + overlay + review
                                                 ▼          ├─ GET /report/<id>      police-form layout (print → PDF)
   candidates.json (ranked by severity)                     ├─ GET /api/incidents    serves incidents.json
                                                 │          ├─ GET /api/detections   proxy to VSS (boxes for overlay)
   /api/v1/videos/stream → ffmpeg 3 frames ──► confirm.py   ├─ GET /api/stream       proxy to VSS stream (JWT stays server-side)
   Cosmos3-Reason /v1/chat/completions            │          ├─ POST /api/review      approve / edit / dismiss (in memory + /tmp)
     (gpu-model-smoke-test pattern)               ▼          └─ POST /api/submit      dummy outbox, never sends
   confirmed.json                                           
                                                 │          Reads VSS_URL / VSS_USERNAME / VSS_PASSWORD
   W&B Inference (gpt-oss-120b) ──────────────► report.py   from the K8s Secret. No video or JPEGs in
                                                 ▼          the ConfigMap (1 MiB limit): video and boxes
              VastDB table witness-incidents ──────────►    are proxied live; boxes drawn on <canvas>.
              (+ incidents.json fallback in app ConfigMap)  Reads incidents from VastDB, falls back to JSON.
```

In-cluster, the engine and app call the backend at `http://video-backend-service:8000`, not the public ingress.

Severity score (per candidate, 0–100): peak frame fill of the vehicle box (closeness)
× approach rate (growth in box height per second before the peak, a proxy for time-to-contact)
× an event weight (cut-in > close pass > blocked lane). Night clips (GX050001) are flagged.

Event-shape rules (moving camera, so look at shapes, not one score):

| Event | Rule over a tracked vehicle box (simple IoU tracker per segment) |
|---|---|
| Close pass | Box height ≥ 35% of the frame, touches the left or right 15% of the frame, then shrinks or moves toward centre (the vehicle moves off ahead) within 2 s |
| Cut-in | Box centre moves sideways ≥ 25% of frame width in ≤ 1.5 s while the box is ≥ 15% of the frame tall, crossing the frame centre line |
| Blocked lane | Description mentions bike lane + blocked/stopped/parked/double-parked, and a vehicle box grows steadily while its centre stays still (the rider is closing in on a stationary vehicle) |

Code lives in the team repo `~/Video-Hackathon-10-2026` (`engine/`, `app/`, `k8s/`), reusing
`probe/vss_client.py` for login. Credentials are read from the mounted config / env only.
Engine code ships to the Job as a ConfigMap (`witness-engine-code`), same pattern as the app.
Results come back to the VM with `kubectl cp` or by reading the VastDB table from a pod.

## Schedule

Three people: **D** = data/engine (owns the K8s engine Job and re-ingest), **A** = app + report form,
**I** = deploy/integration + story/demo (pitch, hero clips, judges' README).

| Time | Owner | Task | Skill(s) | Exit check |
|---|---|---|---|---|
| 12:05–12:25 | D | Engine Job skeleton: ConfigMap + Job that reads `nyc_bike_gopro-1` rows from VastDB and pulls detections for 3 hero segments | `vastdb-read`, `retrieval-videos` | Job logs show 465 rows and boxes for the hero clips |
| 12:05–12:25 | A | `app/incidents.stub.json` (3 fake incidents in the final schema), stdlib `main.py` serving it | – | Schema agreed with D; runs on the VM |
| 12:05–12:25 | I | Deploy hello-world `main.py` at `/app` | `deploy-app-no-registry` | App button shows "hello" |
| 12:25–13:30 | D | `detect.py` over all 465 GoPro segments, then `confirm.py` on the top 30 (Cosmos3-Reason, 3 frames each) | `retrieval-videos`, `gpu-model-smoke-test` | `confirmed.json` with ≥ 8 confirmed incidents, hero clips among them |
| 12:25–13:30 | A | UI against the stub: ride list, incident detail with `<video>` + canvas overlay, review buttons, police-form report page | – | Full click-through works on the VM |
| 12:25–13:30 | I | `/api/stream` + `/api/detections` proxies in the deployed app; then the pitch, hero clips and form field list (UK Dash Cam Safety Portal, NYC 311) | `retrieval-login`, `retrieval-videos`, `deploy-app-no-registry` | A real segment plays through the App button |
| 13:30–13:50 | D | `report.py` (W&B LLM summaries), write the `witness-incidents` VastDB table + `incidents.json` | W&B Inference, `vastdb-write` | Real incidents replace the stub |
| **13:45** | all | **Halfway gate:** spine works on real data. If not, cut to close passes only, top 5 | – | – |
| 13:50–14:45 | D | Should 1: dashcam witness, same Job over the 24 Toronto candidates (`source: witness`, plate box) | `gpu-model-smoke-test`, W&B | ≥ 3 confirmed Toronto incidents |
| 13:50–14:45 | A | Real data wired, severity timeline, previous/next segment, print styles, app reads VastDB with JSON fallback | `vastdb-read` | Full flow works on the VM |
| 13:50–14:45 | I | Re-ingest 3–5 hero segments with a cyclist-safety prompt (only I re-ingests); judges' README | `ingest-reingest-chunk`, `retrieval-dashboard` | Search "driver passed dangerously close" returns a hero clip |
| 14:45–15:15 | I + A | Redeploy the full app (ConfigMap + rollout restart), check through the App button | `deploy-app-no-registry`, `deploy-health` | Full demo flow works from workshop.thecosmoslabs.com |
| 15:15–15:30 | all | Highest-value polish only (Could items only if everything above is green) | – | Still deploys cleanly |
| 15:30–15:45 | all | Code freeze, push repo, backup screenshots, rehearse twice | – | Demo runs twice with no help |

If anything blocks for more than 15 minutes, run `/ask-cosmos`, post the note on Cosmos, and move on.

## Rules check

- [x] **Web app** (stdlib Python server + HTML, so it runs on the VM and in the pod with no installs). No native app.
- [x] **Builds on the running stack.** Only uses the existing backend APIs and GPU endpoints. No pipeline/Segmenter/DataEngine redeploy, no Docker build.
- [x] **Skills first:** `retrieval-login`, `-search`, `-videos`, `-dashboard`, `gpu-model-smoke-test`, `ingest-reingest-chunk`, `deploy-app-no-registry`.
- [x] **Credentials** come from `/config/*.config` / env on the VM and from a K8s Secret in the pod. The JWT never reaches the browser (stream is proxied). Nothing is printed or committed; outputs with source paths stay out of git if they would leak anything. No `/etc/hosts` edits.
- [x] **Own namespace only** (`team-1`), own buckets.
- [x] **Small re-ingest:** 3–5 segments, by one person (I).
- [x] **Corpus video only.**
- [x] **App-side reasoning on W&B** (report text). Cosmos3-Reason is called offline through the GPU skill pattern, never from the app at demo time.
- [x] **Deployed with `deploy-app-no-registry`** at `/app`, viewed via the App button.
- [x] **No automatic real-world actions.** "Send" goes to a dummy outbox; export is print-to-PDF / draft.

## Risks and fallbacks

| Risk | Signal | Fallback |
|---|---|---|
| Event rules are too noisy on a moving bike | > 50% of top-30 rejected by Cosmos, or hero clips missing | Rank by peak side-fill alone, take top 30, let Cosmos be the filter; hand-add the hero clips to the candidate list |
| Cosmos3-Reason slow or down | Calls > 60 s or 5xx | Use the existing `reasoning_content` + W&B LLM on that text for the confirmation; mark it "description-based" |
| Engine Job blocked (image pull, apt, pip) | Job not Running after 5 min | Run the same scripts on the VM with stdlib + `$INGRESS_URL` (VastDB read swapped for `/api/v1/tools/segments`, as in `list_gopro.py`) |
| Stream proxy too slow in the pod | Video stalls > 5 s | Pre-extract 1 keyframe per incident at ≤ 40 KB (fits ConfigMap for ~15 incidents), show the frame with boxes, keep video as optional |
| Deploy breaks late | App button 404/502 after redeploy | Hello-world deployed in slot 1, so the problem is a recent change; roll back to the last good ConfigMap. Screenshots as the last resort |
| ConfigMap > 1 MiB | `kubectl apply` error | No media in ConfigMap; trim `incidents.json` description fields |
| Night ride gives junk | GX050001 incidents look wrong | Drop night from the demo, mention it as a limit |
| Model invents plates/facts | Plate text in model output | Plate field is always blank for helmet cam; dashcam shows crop + "confirm plate" box, never a pre-filled value |

## Demo script

1. **Problem (15 s):** "Cyclists record close passes every day. Almost none get reported,
   because finding the 20 seconds that matter in an hour of footage and filling in a police
   form takes too long."
2. **Walkthrough:** open the App button. Show ride GOPR0130 (15 min) and its severity timeline with N incidents.
   Open the top close pass, the black sedan beside the rider with only a painted buffer.
   Play the clip with live boxes and show the model's confirmation and severity.
3. **Wow moment:** click Approve, type the street, then Report. A police-portal-style form is filled
   with date, time, vehicle, summary and key frames, ready to print. Switch to the
   **Toronto dashcam witness** to show that the same agent on a car's camera finds a car squeezing a cyclist
   and crops the plate (`BEVX 388`) for a human to confirm. "Any road user's camera can be a witness."
   If the re-ingest landed, search "driver passed dangerously close to the cyclist" in VSS and
   watch the incident come up.
4. **Honest limits:** the agent never accuses anyone on its own. Plates and streets are
   entered by the human, the model's guesses are shown as unconfirmed, and nothing is sent automatically
   (portals require a signed statement within ~14 days). It detects unsafe behaviour; it doesn't predict crashes.

Built on: VAST DataEngine + VastDB (index, detections), NVIDIA Cosmos3-Reason (confirmation),
YOLO11 (boxes), W&B Inference (report writing), Cursor.
