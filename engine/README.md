# Engine

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
