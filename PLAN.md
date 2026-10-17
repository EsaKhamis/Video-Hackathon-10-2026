# Plan: Rider safety agent ("your helmet cam is a witness, and a coach")

**Pitch:** An agent watches a cyclist's ride footage, finds every risky moment, and works out
who caused it. When a driver is at fault, it drafts a report the cyclist can send to the police or 311.
When the rider is at fault, it writes a private lesson on what to do differently.
The cyclist reviews everything and decides what, if anything, gets sent.

**Two outputs from one agent:**

| Fault | Output | Audience | Leaves the app? |
|---|---|---|---|
| Driver | **Authority report**: police-portal-style form, clip, key frames, vehicle, blank plate/street | Police portal, NYC 311, insurer, whoever the cyclist chooses | Only if the cyclist exports it (dummy "Send" in the demo) |
| Rider | **Rider lesson**: what happened, why it was risky, what to do next time, the clip | The cyclist only | Never |
| Shared / unclear | Both views side by side; the reviewer decides | The cyclist | Only after they pick "driver at fault" |

**Deadline:** 15:45 code freeze, demo-ready. Submissions open around 16:30
([tokensand.com/vastlondon](https://tokensand.com/vastlondon), "Submit your project").
Deliverables by 15:45: deployed app at `/app`, team repo pushed with a judges' README + screenshots,
a 60-second screen recording of the demo as backup.

**Demo spine:** a judge opens the App button on workshop.thecosmoslabs.com and sees one helmet-cam ride
with a ride safety summary ("3 driver incidents, 2 rider lessons") and its incidents ranked by severity, each tagged
with who was at fault. They open the top driver close pass, watch the clip with detection boxes,
type a plate, click Approve and get a printable authority report. Then they open the taxi crossing,
where the agent says the rider entered on red, and get a rider lesson instead of a report.

## Status at 14:30 (halfway gate: missed, cut to H1 + H3)

The spine runs on real data and nothing labelled rider / adjacent-lane / separated gets a driver verdict, but the
engine makes **no automatic driver verdict** on any labelled clip: Cosmos says H1 and H4 stay in their own lane at
over 2 m, YOLO says about 1 m, so the rules give `unclear`. So the demo is cut to two incidents: **H1 = "unclear,
you decide"** (the reviewer picks driver and gets the authority report) and **H3 = rider, entered on red**.

| Must | State |
|---|---|
| 1. Stage A screening | Done: `data/screen.json`, 465 rows |
| 2. Stage B geometry | Done: `engine/detect.py` → `data/geometry.json` (96 of 465 flagged) |
| 3. Stage C confirmation | Done for the **18 labelled clips** only (`engine/confirm.py` → `data/stage_c.json`, ~5 s per clip). Not yet run on the 96 Stage B flags |
| 4. Stage D rules | Done: `engine/fault.py` v5 (changelog below). `data/stage_d.json` (rider-on-red from Stage A) still used outside the cut |
| 5. Stage E documents | Report and lesson pages are app templates; **W&B text not wired**. No VastDB `witness-incidents` table; the app reads `data/incidents.json` |
| 6. Web app | Done: ride list, incident detail with boxes, review, `/report`, `/lesson`, timeline, dummy `/api/submit` |
| 7. Deployed at `/app` | Done; `data/incidents.json` = H1 + H3 (`build_incidents.py --only H1,H3`), pushed with `k8s/update-data.sh`. Stream returns 206 |
| 8. Evaluation | `probe/eval_labels.py`: **10/18 agree with labels, 0 wrong driver verdicts**. Not yet on an "About" panel |

Should: dashcam witness, re-ingest of hero chunks: not started (nothing in the repo).

**Next, in order:**
1. Merge `feature/stage-c-halfway-cut`; before any full redeploy clear the stale `H1` entry in `app/reviews.json`
   (approved, fault driver) so the demo opens on a pending review.
2. Rehearse the cut demo on the App button (script below, steps 3–5 rewritten for the cut).
3. Judges' README + screenshots + 60 s recording (I), by 15:45.
4. Only if ahead: Stage C on the 96 flagged segments (~2 min) and rebuild without `--only`, keeping only clips
   where label and verdict agree; accuracy line on an "About" panel.

Order of work: **rider (GoPro) data first**. The **Toronto dashcam witness** feed is the
first Should item, added once the spine is deployed.

## Principle: the models do the video work

The NVIDIA models watch every clip; our code only moves clips, merges evidence and applies written rules.

| Model | Where it runs | What it does for us |
|---|---|---|
| **YOLO11** | Already ran in the ingest pipeline | Per-frame boxes at 30 fps for every segment, used for geometry: how close, which side, how fast |
| **Cosmos3-Reason** (video input) | GPU endpoint, called from the engine Job | Watches every 5 s segment and returns structured facts: near miss?, event, vehicle, rider position, signal facing the rider, fault |
| **Cosmos3-Reason** (pipeline Reasoner via re-ingest) | DataEngine, via `ingest-reingest-chunk` | Rewrites the searchable descriptions of the hero chunks with a near-miss prompt, so VSS search and agent Q&A find incidents |
| **Cosmos Embed1** | Already in the index | Semantic search over the re-ingested descriptions (demo "search the archive for near misses") |
| **W&B Inference** (`openai/gpt-oss-120b`) | App-side reasoning | Writes the authority report and rider lesson text from the facts |

### Measured on our labelled clips (12:15)

| Test | Result |
|---|---|
| Cosmos3-Reason accepts a whole clip as `video_url` (base64 mp4) | Yes |
| Speed, 1280 px @ 6 fps, about 0.6 MB per 5 s clip | 3–5 s per call; **6 clips in parallel in 4.9 s wall** → all 465 bike segments in about **6 min** at 6-way parallelism |
| 640 px @ 8 fps | Faster (2.6 s) but misses the signal and the vehicle: **don't use below 1280 px** |
| Blind screening (no hint), 6 labelled clips | 4 of 6 right: taxi crossing = **rider, red** ✔, black SUV = driver near miss ✔, UPS truck and sidewalk clips correctly rejected ✔ (and it noticed the rider was on the sidewalk). Missed both side-on passes (white van, taxi swing) |
| With a detector hint (time, side, box size) | Vehicle identity fixed (white van on the right, yellow taxi, black SUV on the right); SUV re-labelled `close_pass` ✔. Still judged the van and taxi as "safe distance" |
| Vehicle colour without a hint | Unreliable: defaults to "black car". Only trust colour from the hinted call |

**Conclusion:** Cosmos is good at **context and fault** (signal, rider position, what happened, who did it) and
at rejecting false alarms. It is conservative about **distance**. YOLO geometry is good at distance and side.
So: Cosmos screens everything, YOLO measures closeness, and when they disagree it's `unclear` for a human.

## Data check

Live check at 11:50: login OK, dashboard `healthy: true`, `pending_index: 0`, 3,537 segments indexed.

| Need | Camera / source | Status | Evidence or action |
|---|---|---|---|
| Helmet-cam rides | `nyc_bike_gopro-1` (465 segments, 3 rides: GOPR0130, GX010001, GX050001) | Present | Rows dumped to `~/bike-probe/gopro_all.json` |
| Per-frame vehicle boxes | `/api/v1/videos/detections` | Present | car/truck/bus/bicycle/person at 30 fps; `probe/scan_nearmiss.py` scores all 465 in 50 s |
| Near-miss wording in descriptions | GoPro descriptions | Missing | Existing descriptions say "no collisions or abrupt manoeuvres"; search similarity ~0.4. Fixed by the Cosmos screening pass (engine) and a re-ingest of hero chunks (search) |
| Blocked bike lane | GoPro descriptions | Present | 76 descriptions mention blocking; "vehicle is stopped on..." on GOPR0130 chunk 0001/0002 |
| Signal facing the rider | Cosmos on 1280 px video | Present | Cosmos read red correctly on `GOPR0130_chunk_0017_segment_005`; 69 descriptions mention a red signal (12 with a crossing-vehicle shape) |
| Rider on the sidewalk | Cosmos on video | Present | Cosmos identified it on `GX010001_chunk_0018_segment_003` |
| Door zone (riding close to parked cars) | Detections + Cosmos | Present | 13 descriptions; the parked-car shape the close-pass rule rejects |
| Wrong way, phone, no lights | – | Absent | Not visible from a helmet cam or not in the data. Out of scope |
| Plates | Helmet cam | Absent (unreliable) | Model hallucinated "1138". Cyclist types it in; never auto-filled |
| Plates | Toronto dashcam `pie_cam-3` | Present (human confirms) | 1 of 3 read correctly (`BEVX 388`); show the crop for a human to confirm |
| Dashcam witness candidates | `pie_cam-3` | Present | `~/bike-probe/dash_close.json`: 24 segments with a ridden cyclist and a vehicle within half a bike-width |
| Recording date / time | Filenames | **Absent** | The `20261008_072600` prefix looks like index time, not capture time. Reports show "recorded: unknown, cyclist to confirm" plus the offset within the ride (e.g. "GOPR0130, 6:42") |
| GPS / street | none | Absent | Cyclist fills in the street; landmarks (e.g. Residence Inn sign) shown as a hint |
| Report writing | W&B Inference | Present | `openai/gpt-oss-120b`, `deepseek-ai/DeepSeek-V4-Flash-0731` available |

## Hero clips (fixed now, checked by eye)

| # | Clip | What happens | Expected fault | Status |
|---|---|---|---|---|
| H1 | `GOPR0130_chunk_0026_segment_003` | Black SUV passes close on the right and pulls ahead | Driver (close pass) | **Engine: unclear (rule 8)**. Stage C: own lane, over 2 m, no signal visible, rider in traffic lane; YOLO: ~0.95 m at 56% height. In the demo as "you decide" |
| H2 | `GX050001_chunk_0019_segment_001` | White van close in the next lane at dusk, then ahead | Driver, unclear, **or not a near miss** | Labelled adjacent-lane. **Engine: dropped (rule 2, separator)** ✔. Not in the demo |
| H3 | `GOPR0130_chunk_0017_segment_005` | Yellow taxi crosses inches in front of the handlebars | **Rider (entered on red)** | **Engine: rider (rule 3)** ✔, red in the video and the still. In the demo |
| H4 | `GOPR0130_chunk_0026_segment_002` | Taxi swings across the path of the **lead** cyclist | Driver, against another rider | **Engine: unclear (rule 8)**; Cosmos says own lane. Cut from the demo |
| H5 | `GX010001_chunk_0018_segment_003` | Rider on the sidewalk past pedestrians | Rider (sidewalk) | **Engine: rider (rule 4)** ✔. Cut from the demo (could come back as a second lesson) |

## Scope

- **Must (demo spine):**
  1. **Stage A, Cosmos screening** (`engine/screen.py`): every bike segment (465) is downloaded from the in-cluster
     backend, transcoded to 1280 px @ 6 fps (no audio, about 0.6 MB), and sent to Cosmos3-Reason as video with the
     screening prompt below. Output `screen.json`. About 6 min at 6-way parallelism.
  2. **Stage B, YOLO geometry** (`engine/detect.py` = `probe/scan_nearmiss.py` plus the encroachment fields in Stage D):
     close-pass and crossing shapes from the pipeline's boxes, with peak time, side, box, `pan_suspect`,
     `entered_corridor` and `lateral_toward_rider`. Shapes already built and validated; 50 s.
  3. **Stage C, focused confirmation** (`engine/confirm.py`): every segment that Stage A flags **or** Stage B flags
     (expected 40–80) gets a second Cosmos call with the detector hint (time, side, box size), plus a
     full-resolution still of the frame about 1 s before the peak for the signal. Output: final facts per incident.
  4. **Stage D, fault rules** (`engine/fault.py`, plain Python, **no LLM**): the rules below turn facts into
     `fault` and `severity`. "The models see, the rules decide."
  5. **Stage E, documents** (`engine/report.py`): W&B LLM writes one document per incident:
     - **Authority report** (driver fault): neutral, factual summary plus police-form fields.
     - **Rider lesson** (rider fault): "What happened", "Why it was risky", "Next time"; supportive tone;
       general safety advice only, no specific laws or fines.
     Output: VastDB table `witness-incidents` + `app/incidents.json`.
  6. **Web app**: ride list with a ride safety summary (driver incidents vs rider lessons), then an incident list with
     fault badges and two tabs ("Reports to send" / "Lessons for you"), then incident detail (clip plus box overlay,
     the facts behind the verdict, approve / edit / dismiss / change fault), then a printable authority report or a lesson card.
  7. Deployed at `/app` with `deploy-app-no-registry`.
  8. **Evaluation**: accuracy on the labelled set (`probe/labels.csv`, H1–H5 + the 12 clips checked by eye, grown to ~30
     in the definition loop) printed by `probe/eval_labels.py` and shown on an "About / how it works" panel.
- **Should:**
  1. **Dashcam witness feed** (Toronto `pie_cam-3`): Stage A over the 24 candidates in `dash_close.json`
     (or all 1,080 segments, about 15 min, if time allows), plate crop drawn client-side from the box.
     **Driver verdicts only**: the dashcam's cyclist is a stranger, so never a rider lesson (privacy).
  2. **Searchable incidents via re-ingest**: re-ingest the 3 hero chunks (GOPR0130 chunk 0017, 0026; GX050001 chunk 0019,
     about 18 segments) with the custom prompt below, so a VSS search for "driver passed dangerously close to the cyclist"
     or "cyclist ran a red light" returns them. One person (I) runs it.
  3. Severity timeline strip per ride (red = driver, amber = rider, grey = unclear).
  4. Lead-up and aftermath: previous/next segment, found by `original_video` + `segment_number` (never guessed filenames).
  5. Door-zone and following-too-close rider rules (Must covers red light + sidewalk).
- **Could (only if ahead; nobody starts these before 15:00):**
  - Persist review decisions in VastDB (`vast-database-vastdb-write`).
  - W&B Weave tracing of the Cosmos + W&B calls.
  - Email draft (`mailto:`) export; face blurring in exported frames; "rider score" across rides.
- **Out of scope:**
  - Auto-submitting to police or 311 (rule). "Send" posts to a dummy `/api/submit` outbox; lessons have no send action.
  - Crash or theft prediction (data: no such footage).
  - Matching the same incident across bike and dashcam (data: NYC vs Toronto). Pitched as independent witnesses.
  - Re-ingesting all 465 bike segments (rule: keep re-ingest small; the direct Cosmos pass gives the engine the same information in minutes).
  - Wrong-way riding, phone use, missing lights (data). Legal advice (lessons never claim an offence or fine).
  - Native/mobile app (rule).

## Near-miss definition (worked out back and forth, before reports are built on it)

Reviewing the first results showed two errors, and both would wreck the demo:

1. **Cars driving normally in the next lane get called near misses.** The YOLO close-pass shape only needs a big box
   at the frame edge that later shrinks, so ordinary adjacent-lane traffic passes it, even behind a separator.
   Example: the sedan beside the protected bike lane, behind flex posts
   (`docs/images/near-miss/sedan_beside_bike_lane_ambiguous.jpg`). H2 (white van "close in the next lane") may be the same case.
2. **Rider-caused conflicts get a driver verdict.** The rider entered on red, swerved into the traffic lane, rode on
   the sidewalk or squeezed between cars, and it still came out as driver fault. Part of the cause: the Stage D driver
   rule used Cosmos's own `fault` answer.

So the definition is written down here, tested against hand-labelled clips, and revised in short rounds
**before** reports and lessons are built on top. D changes the rules and prompts; I labels clips and judges the result.

### Definition v1

A **near miss** is a moment where a motor vehicle and the rider got close enough that one of them had to,
or should have had to, change speed or line to avoid contact. All four must hold:

| # | Condition | How it's measured |
|---|---|---|
| 1 | **Shared space:** nothing physical between them (flex posts, curb, parked-car buffer, median). A painted line alone does not count as separation | Cosmos `separator` |
| 2 | **Encroachment:** the vehicle moves into the rider's path, or the rider into the vehicle's, rather than travelling beside it | YOLO: the box's inner edge moves toward the image centre and enters the **rider corridor** (middle ~40% of frame width, lower half). Cosmos `vehicle_entered_rider_path`, `rider_moved_into_vehicle_path` |
| 3 | **Closeness:** big in frame while encroaching | Peak box height ≥ 30% of frame height (calibrated in round 1) |
| 4 | **Something changed:** not steady parallel travel | Any of: vehicle moves sideways toward the rider, the rider swerves or brakes (all tracks shift together), or time-to-contact < ~1.5 s |

**Not a near miss** (each exclusion has a check, so it can be tuned):

| Pattern | Why it looks like one | How it's rejected |
|---|---|---|
| Adjacent-lane traffic | Big box at the edge | Inner edge never moves toward the centre (sideways speed toward the rider under a threshold), box size roughly steady; Cosmos `vehicle_action = parallel_in_own_lane` |
| Separated lane | Car right next to the rider | Cosmos `separator = yes` |
| Rider closing on a stopped or slower vehicle | Box grows fast | Close-pass shape already rejects it (grows and moves outward); goes to door-zone / blocked-lane instead |
| Queued traffic at a light | Many big boxes, low time-to-contact | Rider barely moving (background static across the window) |
| Rider turning or swerving | All boxes sweep sideways | `pan_suspect`. **No longer just dropped:** it sets `rider_manoeuvre = true`, which counts toward rider fault |

Fault is decided by the Stage D rules below. A wrong "driver" verdict is worse than a wrong "unclear", because it puts
a false accusation in front of the police, so missing or `unknown` facts never produce a driver verdict.

### The loop

Each round takes about 15 minutes:

1. D runs the engine and `probe/contact_sheet.py` (stdlib + `ffmpeg` on the VM): one 6-frame strip per candidate,
   like the sedan example, with the geometry, Cosmos facts and verdict printed under it.
2. I labels each strip in `probe/labels.csv` with one of: `driver`, `rider`, `shared`, `adjacent_lane`, `separated`,
   `stationary`, `rider_turning`, `other_fp`.
3. `probe/eval_labels.py` compares engine against labels: what share of survivors are real near misses, and a
   fault table (engine verdict vs label). This is also the Must 8 evaluation.
4. D changes **one thing** (a threshold, an exclusion, a prompt field or a rule), logs it in the changelog, and reruns.
   Re-running Stages B–D on the labelled clips only takes about a minute, so rounds stay short.

Order of rounds: **round 1** YOLO encroachment (fixes adjacent lanes from geometry); **round 2** the new Cosmos facts on
the labelled set (does it see separators, lane changes and red signals correctly?); **round 3** the fault rule order.

**Labelled set:** the 17 clips already checked (H1–H5 + 12), grown to ~30. Deliberately include the sedan beside
the bike lane, 3–4 plain adjacent-lane passes, and any rider swerve or filtering clips that show up.

**Stop when all three hold, or at 13:30, whichever comes first:**

- ≥ 70% of the top 15 survivors are labelled as real near misses.
- No clip labelled `rider`, `adjacent_lane` or `separated` gets a driver verdict.
- The engine agrees with the label on every hero clip.

If the bar isn't met at 13:30, freeze the current version and demo only clips where label and verdict agree.

**Changelog** (one line per change: what changed, what it fixed, survivors and wrong driver verdicts before/after):

- v1: definition above, not yet run.
- 14:15 Stage C (`engine/confirm.py`) on all 18 labelled clips, rules in `engine/fault.py`, scored by `probe/eval_labels.py`.
  v1–v3 (rider rules before the separator drop; driver needs Cosmos `vehicle_entered_rider_path` **and** YOLO corridor, no pan):
  8/18 agree, 0 wrong driver. Most misses: the still alone says "red" on mid-block clips.
- v4: red needs video **and** still: 11/18 agree, 0 wrong driver.
- v5: hint and rules use the track the Stage B shape flagged (H1's corridor hit was a different car on the left); a
  close-pass shape within 1 m at ≥ 45% height is `unclear` even in its own lane: 10/18 agree, 0 wrong driver, H1 kept.
- **Halfway gate (14:30): not met → cut to H1 + H3.** Cosmos says H1 (and H4) is "own lane, over 2 m" while YOLO says
  ~1 m, so the rules give `unclear`, never `driver`. Demo: H1 is "unclear, you decide"; the reviewer picks driver and
  gets the authority report. H3 = rider, red in video and still. `build_incidents.py --only H1,H3` → deployed.

## Prompts (fixed, shared by D and A)

**Stage A screening prompt** (Cosmos3-Reason, video). Allowed values are fixed; no example values that could bias the model:

```text
You are reviewing a 5-second clip from a cyclist's helmet camera. Answer ONLY with JSON:
{"near_miss": true|false,
 "event": "close_pass"|"cut_in"|"crossing_path"|"turning_across"|"blocked_bike_lane"|"door_zone"|"none",
 "vehicle": {"type": "car|taxi|van|suv|truck|bus|motorcycle|none", "colour": "<colour you see>", "side": "left"|"right"|"front"|"none"},
 "closest_distance": "under_1m"|"1_to_2m"|"over_2m"|"unknown",
 "peak_time_sec": number,
 "rider_position": "bike_lane"|"traffic_lane"|"sidewalk"|"crosswalk"|"unknown",
 "separator": "yes"|"no"|"unknown",
 "vehicle_action": "overtaking"|"turning_across"|"pulling_out"|"crossing"|"stopped_in_lane"|"parallel_in_own_lane"|"none",
 "vehicle_entered_rider_path": true|false|null,
 "rider_moved_into_vehicle_path": true|false|null,
 "signal_for_rider": "red"|"green"|"yellow"|"none_visible"|"unknown",
 "rider_entered_on_red": true|false|null,
 "fault": "driver"|"rider"|"shared"|"unclear"|"none",
 "evidence": "one sentence"}
```

`separator` means a physical barrier between the rider and the vehicle (posts, curb, parked cars); a painted line is
not a separator. Cosmos's `fault` is stored as `model_fault` and shown for comparison only; **Stage D never uses it.**

**Stage C focused prompt** = a hint line + the same schema:
`A detector flagged a {label} on the {side} edge at about {t} s, filling {h}% of the frame height, which then moved ahead of the rider. Look closely at that vehicle around that time. Describe THAT vehicle's actual colour and type. Did it stay in its own lane, or move into the rider's path? Did the rider move into its path?`
Plus a separate still-image call on the full-resolution frame 1 s before the peak:
`What colour is the traffic signal facing the camera's direction of travel? Answer red, green, yellow, none_visible or unknown.`

**Re-ingest custom prompt** (Should 2; ≤ 800 characters):

```text
Cyclist helmet-camera safety review. Describe: (1) every motor vehicle that comes within about 2 metres of the rider: type, colour, side, and what it did (passed, cut in, turned across, crossed in front, stopped in the bike lane); (2) whether the rider is in a bike lane, traffic lane, sidewalk or crosswalk; (3) the traffic signal facing the rider (red, green, yellow, not visible) and whether the rider enters an intersection on red; (4) whether a near miss occurs and who caused it: driver, rider, shared or unclear. Use the words near miss, close pass, cut in, ran a red light, riding on the sidewalk when they apply.
```

## Fault rules (Stage D, plain Python)

Inputs: Stage C facts (Cosmos) + Stage B geometry (YOLO). Applied in order; the first match wins. Rules are
revised through the definition loop above; this table is v1.

Stage B adds three geometry fields per track to `scan_nearmiss.py`: `entered_corridor` (inner edge reached the rider
corridor), `lateral_toward_rider` (sideways speed of the inner edge toward the centre, frame widths per second) and
`rider_manoeuvre` (= `pan_suspect`). **Encroachment** = Cosmos `vehicle_entered_rider_path = true` **or** `entered_corridor`.

| # | Condition | `fault` | Output |
|---|---|---|---|
| 1 | `pan_suspect` and Cosmos `near_miss = false` | drop | – |
| 2 | `separator = yes`, or no encroachment and (`vehicle_action = parallel_in_own_lane` or `lateral_toward_rider` under threshold) | drop | Adjacent-lane or separated traffic |
| 3 | `signal_for_rider = red` (video **or** still) and `rider_entered_on_red != false` | rider | Rider lesson "Entered on red" |
| 4 | `rider_position = sidewalk` | rider | Rider lesson "Riding on the sidewalk" |
| 5 | (`rider_moved_into_vehicle_path = true` or `rider_manoeuvre`) and Cosmos `vehicle_entered_rider_path != true` | rider | Rider lesson "Changed line into traffic" (look over your shoulder, signal, hold your line) |
| 6 | Door-zone shape (parked car grows and moves outward at the edge, ≥ 30% of frame height) and no moving traffic alongside | rider | Rider lesson "Door zone" (Should) |
| 7 | Encroachment, `vehicle_action` in overtaking / turning across / pulling out / crossing / stopped in lane, rider in `bike_lane` / `traffic_lane`, `signal_for_rider` not red **and not unknown on a crossing/turning event**, `separator = no` | driver | Authority report |
| 8 | YOLO encroachment with peak ≥ 45% of frame height **but** Cosmos says `over_2m` / no near miss, or any rule 7 fact is `unknown` | unclear | Both views; human decides |
| 9 | `signal_for_rider = unknown` on a crossing/turning event | unclear | Reviewer must tick "I had right of way" before export |
| 10 | Everything else | drop | – |

`shared` is never assigned automatically; the reviewer can pick it with "change fault".

**Severity** is separate from fault: rank by YOLO peak box height × approach speed, scaled 0–100 across the
incidents, shown as High (≥ 70) / Med (40–69) / Low. Night clips (GX050001) are flagged "low light".

A rider-fault incident can never produce an authority report. Every authority report prints the rider's signal
and position as facts.

## Runtime: what runs in VAST vs locally

| Capability | VAST runtime (K8s pod in `team-1`) | VM (local) |
|---|---|---|
| Create Jobs / Deployments | Yes, no quota | n/a |
| VSS backend | `http://video-backend-service:8000` (in-cluster, fast) | `$INGRESS_URL` |
| VastDB SDK | Yes (`pip install vastdb`) | **No**: VM has no `pip` / `venv` |
| Cosmos3-Reason (GPU host :8001), W&B Inference | Reachable | Reachable |
| `ffmpeg` | `apt-get install ffmpeg` in the Job | Installed |

**The engine runs as a K8s Job** (`witness-engine`, `python:3.12-slim` + apt `ffmpeg` + pip `vastdb`), code from the
ConfigMap `witness-engine-code`, team config from the Secret `witness-config` mounted at `/config/team.config`.
Stages A–E run in one Job, about 10 minutes end to end. The VM is only for editing, `kubectl`, and stdlib checks.
Fallback if the Job is blocked: the same scripts run on the VM with stdlib only (already proven: the Stage A test ran from the VM).

Gotcha: the VastDB SDK logs a harmless "sorted columns ... without elysium" RPC error on table open; ignore it.

## Architecture

```
K8s Job "witness-engine" (team-1)                                    K8s Deployment (/app)
--------------------------------                                     ---------------------
VastDB vss-collection (camera_id=nyc_bike_gopro-1, 465 rows)         main.py (stdlib http.server)
  │                                                                  ├─ GET /                ride list + safety summary
  ├─► A screen.py   backend stream → ffmpeg 1280px/6fps              ├─ GET /incident/<id>   player + overlay + facts + review
  │                 → Cosmos3-Reason (video)  ── 465 calls, ~6 min   ├─ GET /report/<id>     authority report (driver only)
  ├─► B detect.py   backend detections (YOLO) ── 50 s                ├─ GET /lesson/<id>     rider lesson (never sent)
  │                                                                  ├─ GET /api/incidents   VastDB, JSON fallback
  ├─► C confirm.py  A∪B flagged (~40–80) → Cosmos (video + hint)     ├─ GET /api/detections  proxy (boxes for <canvas>)
  │                 + full-res still → Cosmos (signal)               ├─ GET /api/stream      proxy with Range → 206
  ├─► D fault.py    rules table → fault + severity                   ├─ POST /api/review     approve/edit/dismiss/change fault
  ├─► E report.py   W&B gpt-oss-120b → report / lesson text          └─ POST /api/submit     dummy outbox
  │                                                                  
  └─► VastDB table witness-incidents  +  incidents.json  ───────────►  read by the app
                                                                     
Pipeline (Should 2): ingest-reingest-chunk on 3 hero chunks with the near-miss prompt
  → Reasoner (Cosmos3-Reason) → Embedder (Cosmos Embed1) → VSS search finds incidents
```

App details that must not be missed:
- **`/api/stream` must pass the browser's `Range` header to the backend and return `206 Partial Content`**,
  or `<video>` won't seek (and may not play) in Chrome. The JWT stays server-side.
- No video or JPEGs in the ConfigMap (1 MiB limit). Boxes are drawn on a `<canvas>` over the video; evidence
  crops (signal, plate) are cut client-side from the video frame.

Incident schema (shared by engine and app): `id, ride, source, ride_offset_sec, t_peak, event_type, severity, severity_label,
fault, fault_rule, facts{signal_for_rider, rider_position, separator, vehicle_action, vehicle_entered_rider_path,
rider_moved_into_vehicle_path, vehicle{type, colour, side}, closest_distance, evidence, model_fault},
geometry{peak_h, side, bbox, pan_suspect, entered_corridor, lateral_toward_rider}, model_agreement, evidence_frames[t], document{kind: authority_report | rider_lesson, ...},
review{status, plate, street, fault_override, recorded_at}`.

Code lives in the team repo `~/Video-Hackathon-10-2026` (`engine/`, `app/`, `k8s/`), reusing `probe/vss_client.py`.

## Schedule (from 12:20)

Three people: **D** = data/engine (K8s Job, prompts, rules), **A** = app + report/lesson templates,
**I** = deploy/integration, re-ingest owner, story/demo (pitch, judges' README, recording).

| Time | Owner | Task | Skill(s) | Exit check |
|---|---|---|---|---|
| 12:20–12:50 | D | Engine Job skeleton + **Stage A** over all 465 bike segments | `retrieval-videos`, `gpu-model-smoke-test` | `screen.json` with 465 rows; H3 = rider/red, H5 = sidewalk |
| 12:20–12:50 | A | `incidents.stub.json` (H1–H5 in the final schema), stdlib `main.py`, ride + list pages | – | Click-through on the VM |
| 12:20–12:50 | I | Hello-world at `/app`, then `/api/stream` with Range support | `deploy-app-no-registry`, `retrieval-videos` | A real segment plays **and seeks** via the App button |
| 12:50–13:00 | D | **Stage C** (focused + signal still, new prompt fields), **Stage D** v1 rules, `contact_sheet.py`, `eval_labels.py` | `gpu-model-smoke-test` | Contact sheets for the labelled set + top survivors |
| 12:50–13:00 | I | Start the re-ingest of the 3 hero chunks (runs in the background); seed `probe/labels.csv` with the 17 checked clips, the sedan beside the bike lane and 3–4 adjacent-lane passes | `ingest-reingest-chunk` | ~22 labelled clips; re-ingest running |
| 13:00–13:30 | D + I | **Definition loop**, 3 rounds of ~10 min (round 1 YOLO encroachment, round 2 Cosmos facts, round 3 rule order). I labels new survivors and checks hero signals / rider position by eye; D changes one thing per round and logs it | `gpu-model-smoke-test`, `retrieval-dashboard` | Stop criteria in "Near-miss definition" met; ≥ 5 driver + ≥ 2 rider incidents, all agreeing with labels |
| 12:50–13:30 | A | Incident detail: `<video>` + canvas boxes, facts panel (incl. separator, who changed lane, `model_fault` vs rule verdict), review buttons, fault tabs | – | Works on the stub |
| 13:30–13:50 | D | **Stage E** report/lesson text; write VastDB `witness-incidents` + `incidents.json` | W&B Inference, `vastdb-write` | Real incidents replace the stub |
| **13:45** | all | **Halfway gate:** spine works on real data, and no labelled rider / adjacent-lane clip gets a driver verdict. If not, cut to H1 (driver) + H3 (rider) only | – | – |
| 13:50–14:45 | D | Should 1: dashcam witness (Stage A + C on the 24 Toronto candidates, driver verdicts only) | `gpu-model-smoke-test` | ≥ 3 Toronto driver incidents with plate box |
| 13:50–14:45 | A | Authority report + lesson card pages, print styles, timeline, previous/next segment, VastDB read | `vastdb-read` | Full flow on the VM |
| 13:50–14:45 | I | Judges' README, pitch, demo script dry run on the VM | – | Script timed under 3 min |
| 14:45–15:15 | I + A | Redeploy the full app, check through the App button | `deploy-app-no-registry`, `deploy-health` | Full demo flow works from workshop.thecosmoslabs.com |
| 15:15–15:30 | all | Highest-value polish only | – | Still deploys cleanly |
| 15:30–15:45 | all | Code freeze, push repo, screenshots, 60 s screen recording, rehearse twice | – | Demo runs twice with no help |

If anything blocks for more than 15 minutes, run `/ask-cosmos`, post the note on Cosmos, and move on.

## Rules check

- [x] **Web app** (stdlib Python server + HTML). No native app.
- [x] **Builds on the running stack.** Uses the backend APIs, GPU endpoints and re-ingest skill. No pipeline/Segmenter/DataEngine redeploy, no Docker build.
- [x] **Skills first:** `retrieval-login`, `-search`, `-videos`, `-dashboard`, `gpu-model-smoke-test`, `ingest-reingest-chunk`, `vastdb-read/-write`, `deploy-app-no-registry`.
- [x] **Credentials** from `/config/*.config` on the VM and the `witness-config` Secret in pods. JWT never reaches the browser. Nothing printed or committed. No `/etc/hosts` edits.
- [x] **Own namespace only** (`team-1`), own buckets.
- [x] **Small re-ingest:** 3 hero chunks (about 18 segments), by one person (I). The bulk analysis uses direct Cosmos calls instead.
- [x] **Corpus video only.**
- [x] **App-side reasoning on W&B** (report and lesson text). Cosmos3-Reason is called from the engine Job, never from the app at demo time.
- [x] **Deployed with `deploy-app-no-registry`** at `/app`, viewed via the App button.
- [x] **No automatic real-world actions.** "Send" goes to a dummy outbox; lessons have no send action.

## Risks and fallbacks

| Risk | Signal | Fallback |
|---|---|---|
| Cosmos GPU slow or shared with other teams | Stage A > 10 s per call, or 429/5xx | Drop to 4-way parallelism; screen only A∪B candidates (≈ 60) instead of all 465 |
| Cosmos misses side-on close passes | H1/H2-type clips come back `none` | That is why Stage B exists: YOLO-only close passes become `unclear` (rule 6), never dropped |
| Cosmos invents facts (colour, signal) | Disagrees with the hinted call or the still | Use only hinted-call colour; signal from the still overrides the video; disagreement → `unclear` |
| Engine Job blocked (image pull, apt, pip) | Job not Running after 5 min | Run the same scripts on the VM (stdlib + `$INGRESS_URL`), as in the 12:15 tests |
| `<video>` won't seek or play | Player stuck at 0:00 | Range pass-through in `/api/stream`; fallback: a still frame with boxes |
| Deploy breaks late | App button 404/502 after redeploy | Hello-world deployed first; roll back to the last good ConfigMap; recording as last resort |
| ConfigMap > 1 MiB | `kubectl apply` error | No media in the ConfigMap; trim description fields |
| Fault verdict is wrong on a hero clip | Judge spots a red light, or the reverse; `eval_labels.py` shows rider-labelled clips with a driver verdict | Rules never use Cosmos's `fault`; rider rules come before the driver rule; `unknown` facts give `unclear`, never driver. H1–H5 are in the labelled set; facts shown under every verdict; reviewer can change fault |
| Adjacent-lane cars still flagged after round 1 | `adjacent_lane` / `separated` labels among the survivors | Tighten the corridor width and the sideways-speed threshold; if geometry can't separate them, require Cosmos `vehicle_entered_rider_path = true` for any driver verdict |
| Definition loop overruns | Stop criteria not met at 13:30 | Freeze the current version; demo only clips where label and verdict agree |
| Lessons sound preachy or make legal claims | Text cites laws/fines or blames | Fixed 3-part template, no legal claims; I reviews hero lessons by hand |
| Wrong recording date on a report | Filename prefix is index time | Show "recorded: unknown, cyclist to confirm" + ride offset |
| Model invents plates | Plate text in model output | Plate field always blank for helmet cam; dashcam shows crop + "confirm plate" box |

## Honest limits (say these in the demo)

- Closeness is estimated from box size, not measured distance.
- Cosmos is conservative about distance and unreliable about colour without a hint; the human confirms.
- No face blurring yet in exported frames.
- No recording date/GPS in the corpus; the cyclist confirms date and street.
- The near-miss definition was tuned on a small hand-labelled set (~30 clips), and accuracy is reported on that set, not a benchmark.

## Demo script

1. **Problem (15 s):** "Cyclists record close passes every day. Almost none get reported,
   because finding the 20 seconds that matter in an hour of footage and filling in a police
   form takes too long. And nobody tells riders about the risks they take themselves."
2. **How it works (20 s):** "Cosmos Reason watched every one of the 465 clips in about six minutes; YOLO measured how
   close each vehicle got; plain rules decide who was at fault. The models see, the rules decide."
3. **Walkthrough (cut, 14:30):** open the App button. Ride GOPR0130: two incidents on the timeline. Open H1 (black SUV
   close pass): clip with boxes. "YOLO says it came within about a metre; Cosmos says it stayed in its lane over 2 m.
   The models disagree, so the agent doesn't accuse anyone, it asks you." Pick driver at fault, type the street,
   click Approve, open the authority report. (This replaces step 5.)
4. **The same agent coaches the rider:** open H3 (taxi crossing). "The agent found a taxi inches from the handlebars, but the
   signal facing the rider was red." Rider at fault, red signal crop as evidence, and a lesson instead of a report.
   "It tells you when it was you, and it never sends that anywhere."
5. ~~**Unclear means you decide:**~~ covered by H1 in step 3 after the cut. Original: an unclear clip (H2 if round 1 keeps it, otherwise the best `unclear` survivor): the
   boxes say very close, Cosmos says safe distance, so it's marked unclear for the human. Mention that cars simply
   driving in the next lane are filtered out on purpose.
6. **Witness network (if done):** Toronto dashcam finds a car squeezing a cyclist and crops the plate (`BEVX 388`) to confirm.
   If the re-ingest landed: search VSS for "driver passed dangerously close to the cyclist" and H1 comes up.
7. **Honest limits** (above), and accuracy on the labelled set: 10 of 18 agree, and no false driver verdicts.

Built on: VAST DataEngine + VastDB (index, detections, incidents table), NVIDIA Cosmos3-Reason (screening, fault facts),
YOLO11 (geometry), Cosmos Embed1 (search), W&B Inference (report and lesson writing), Cursor.
