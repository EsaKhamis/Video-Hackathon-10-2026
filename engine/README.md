# Engine

## Data flow

Everything lives in `data/`. The app reads only `segments.json`, `screen.json` and `incidents.json`;
`k8s/update-data.sh` pushes just those three (a ConfigMap holds at most 1 MiB).

| File | Made by | Read by |
|---|---|---|
| `segments.json` | `probe/build_segments.py` | app (playable sources), `build_incidents.py` |
| `screen.json` | Stage A (data-engine) | app (ride timeline) |
| `unfiltered_flags.json` | Stage A captions before the frame review | reference only |
| `geometry.json` | Stage B, `engine/detect.py` | `build_incidents.py` |
| `stage_d.json` | Stage D (data-engine): rider-on-red survivors | `build_incidents.py` |
| `stage_c.json` | Stage C, `engine/confirm.py`: Cosmos facts per flagged segment | `build_incidents.py` |
| `incidents.json` | `engine/build_incidents.py` | app |

```bash
python3 engine/detect.py           # Stage B -> data/geometry.json (~2 min)
python3 engine/confirm.py          # Stage C -> data/stage_c.json (~2.5 min, 102 segments)
python3 engine/build_incidents.py  # -> data/incidents.json
k8s/update-data.sh                 # live in /app within about a minute
```

`build_incidents.py` gives Stage D survivors `fault: rider` (rule 3, rider lesson) and the 12 most severe
Stage B close + encroaching vehicles `fault: unclear` (rule 8). It makes **no driver verdicts**: rule 7 needs
Stage C facts, and missing facts never produce a driver verdict. Stage C rows should replace the rule 8 ones.

## Stage B: YOLO geometry (`detect.py`)

Stdlib only. Reads the pipeline's per-frame YOLO boxes for every GoPro segment, tracks vehicles, and
writes `data/geometry.json` (one row per segment, keyed by `id` like `GOPR0130_chunk_0026_segment_003`).

```bash
python3 engine/detect.py                                    # all 465 segments, ~2 min
python3 engine/detect.py --labels probe/labels.csv --out /tmp/g.json   # labelled clips + check table
```

Per row: `geometry` for the most threatening vehicle (`runner_up` for the next), the close-pass / cut-in
shapes from `probe/scan_nearmiss.py` (`pass`, `cut`), `encroachment`, `adjacent_lane_like`, and
`flag` / `flag_reasons` (send to Stage C).

| Field | Meaning |
|---|---|
| `peak_h`, `t_peak`, `bbox`, `side` | Largest box height (fraction of frame), when, box at peak (normalised x1,y1,x2,y2), left/right |
| `gap_start_m`, `min_gap_m`, `gap_at_peak_m` | Near edge to the rider's line in metres, from box height and a typical vehicle height (car 1.5 m, truck/bus 3 m). Estimate only |
| `entered_corridor` | Gap fell below 0.5 m after starting at least 0.5 m further out |
| `lateral_toward_rider` | Fastest gap closing, m/s over 0.5 s, corrected for camera pan |
| `approach` | Fastest growth of box height in the second before the peak (for severity) |
| `pan_suspect` / `rider_manoeuvre` | Most other vehicles swept sideways together: the rider turned or swerved |

**Flag** = close-pass shape, or cut-in shape that is not a camera pan, or peak ≥ 30% of frame height
with `entered_corridor` and `lateral_toward_rider ≥ 1.0 m/s`.

**On the labelled set (18 clips):** flags 5 of 6 real near misses and 7 of 12 non-incidents; 96 of 465
segments flagged overall. Stage B is the high-recall filter: it can't tell adjacent-lane traffic
(H2, gap 0.6 m) from a real close pass (H1, 0.3 m) on its own; the helmet turning and the fisheye lens add
too much noise. Stage C/D use Cosmos (`separator`, `vehicle_entered_rider_path`) to decide. The one
missed clip (`GOPR0130_chunk_0006_segment_003`, rider on red) is in Stage A's red-light set, and
Stage C takes A ∪ B.

## Stage C: Cosmos confirmation (`confirm.py`)

Every Stage B flag plus the labelled clips (102 segments): the clip at 1280 px / 6 fps goes to
Cosmos3-Reason with the facts schema, plus a full-resolution still 1 s before the peak for the signal.

What we learned on the labelled clips, and why it is built this way:

- **No detector hint.** With the hint Cosmos agreed with it: near miss and driver fault on almost every clip,
  including adjacent-lane, sidewalk and separated ones. Blind, it rejects H2, H5 and the adjacent-lane passes.
- **Majority vote.** Cosmos is not deterministic at temperature 0: between runs H4 flipped to unclear (the
  still read red once) and an adjacent-lane clip flipped to driver. Close-pass candidates and sidewalk calls
  get 5 calls each; the vote was 5/5 on H1, H4 and the adjacent-lane clips.
- **Both models for a driver verdict** (rule 7 in `build_incidents.py`): YOLO close-pass shape and Cosmos
  near miss + vehicle entered the rider's path by vote, no separator, not on the sidewalk, no red signal.
  Colour and side from Cosmos are unreliable (it called the H4 yellow taxi on the right a black car on the
  left), so side comes from YOLO and colour is left for the cyclist to confirm.

Result on the 18 labelled clips: H1 and H4 are driver; no adjacent-lane, separated or rider clip gets a
driver verdict. Overall: 2 driver incidents, 11 sidewalk lessons (4-5 of 5 votes), 10 unclear (top by
severity), plus the 13 Stage D rider-on-red lessons. Tuned on 18 clips: an honest limit, not a benchmark.
