# Near-miss definition loop

Dashboard at the start: 612 videos fully indexed, 3,537 segments, pending index 0, pipeline healthy.
YOLO health returned model loaded. Cosmos3-nano-reasoner was ready.

Eye labels are in `labels.csv`. Verdicts are in `round_results.json`.

## Round 1 — YOLO encroachment

Change: the inner edge has to move toward the rider at 0.10 frame-widths per second. The first cut was 0.05.

That 0.05 cut kept H2, the white van in the next lane (`N0437`). The van's edge only drifts. At 0.10 it drops. The old edge-shrink shape had 10 hits. The corridor at 0.05 had 32. After this change, 20 remain.

## Round 2 — Cosmos facts

Change: one still prompt. A painted line or scaffolding is not a separator. If the handlebars are visible, the position cannot be unknown.

Before, H1 (`N0301`) and several sidewalk approaches came back `separator=yes`, which would have thrown out a real pass and hidden the sidewalk. After, H1 is `separator=no`, and `N0272`, `N0337`, `N0112`, `N0109`, and H5 (`N0111`) are `sidewalk`.

The same prompt stopped calling the red signal on `N0181`. That clip stays unlabelled by the engine. The eye still sees a red signal and a rider entering the crosswalk.

## Round 3 — rule order

Change: a sidewalk position is decided before any driver verdict. A vehicle that is already beside the rider counts when Cosmos sees an overtake in the bike lane or the traffic lane, because the box never has to cross into the corridor. An unnamed position becomes a driver only when the box is at least 55% of the frame height, so the next-lane van (`N0416`, 45%) stays dropped.

## Stop check

- 10 of 11 survivors match a real near-miss label (91%).
- No clip labelled rider, adjacent lane, or separated received a driver verdict.
- H1 through H5 agree with the eye.
- Driver, agreeing: `N0074`, `N0301` (H1), `N0228`, `N0300` (H4), `N0245`.
- Rider, agreeing: `N0111` (H5), `N0272`, `N0337`, `N0112`, `N0109`.

`N0425` is the one survivor that is not a near miss. The rider looks back and the bus stays in its lane. The engine calls it a driver. It is labelled `rider_turning`, so it does not break the rider or adjacent-lane ban, and the 91% figure includes it.
