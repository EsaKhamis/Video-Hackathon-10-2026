"""Stage D fault rules (PLAN.md "Fault rules"), plain Python, no LLM. First match wins.

decide(facts, still_signal, geo_row, votes=None) -> (fault, rule, title)   fault in driver | rider | unclear | drop
votes: per-call answers when engine/confirm.py asked Cosmos several times (it is not deterministic).
Cosmos's own `fault` is never read. Missing or unknown facts never produce a driver verdict.
"""
DRIVER_ACTIONS = {'overtaking', 'turning_across', 'pulling_out', 'crossing', 'stopped_in_lane', 'cut_in'}
CROSSING_EVENTS = {'crossing_path', 'turning_across'}
UNCLEAR_PEAK_H = 0.45
LATERAL_MIN = 1.0
CLOSE_PASS_GAP_M = 1.0
VOTES_NEEDED = 4          # of 5 Cosmos calls
VERSION = 'v6'


def track(geo_row):
    """The track the Stage B shape flagged (the one Cosmos was hinted about), else the most threatening one."""
    geo_row = geo_row or {}
    g, alt = geo_row.get('geometry'), geo_row.get('runner_up')
    shape = geo_row.get('pass') or geo_row.get('cut')
    if not (g and alt and shape):
        return g
    t = shape.get('t', shape.get('t1'))
    side = shape.get('side')

    def miss(x):
        return (side is not None and x['side'] != side, abs(x['t_peak'] - t))
    return min((g, alt), key=miss)


def voted(votes, key, value):
    """True when at least VOTES_NEEDED of the Cosmos calls gave `value`; None when there was no vote."""
    if not votes or key not in votes:
        return None
    return sum(v == value for v in votes[key]) >= min(VOTES_NEEDED, votes['n'])


def decide(f, still_signal, geo_row, votes=None):
    f = f or {}
    geo_row = geo_row or {}
    g = track(geo_row) or {}
    pan = bool(g.get('pan_suspect'))
    corridor = bool(g.get('entered_corridor'))
    cosmos_entered = f.get('vehicle_entered_rider_path') is True
    encroach = cosmos_entered or corridor
    # v4: the still alone answers "red" on mid-block clips with no intersection; red needs video and still
    red = f.get('signal_for_rider') == 'red' and still_signal == 'red'
    signal_unknown = f.get('signal_for_rider') in (None, 'unknown') and still_signal in (None, 'unknown')
    crossing = f.get('event') in CROSSING_EVENTS or f.get('vehicle_action') in ('crossing', 'turning_across')

    if pan and f.get('near_miss') is False:
        return 'drop', 1, 'Rider turning, no near miss'
    # v2: rider rules before the separator drop: a barrier next to the rider doesn't excuse entering on red
    if red and f.get('rider_entered_on_red') is not False:
        return 'rider', 3, 'Entered on red'
    # v6: one call says "sidewalk" too easily; with a vote, it needs 4 of 5
    if f.get('rider_position') == 'sidewalk' and voted(votes, 'rider_position', 'sidewalk') is not False:
        return 'rider', 4, 'Riding on the sidewalk'
    # v5: a pass within ~1 m is worth a human look even in its own lane (YOLO close, Cosmos often says over 2 m)
    close_pass = bool(geo_row.get('pass')) and not pan and (g.get('min_gap_m') or 9) <= CLOSE_PASS_GAP_M \
        and (g.get('peak_h') or 0) >= UNCLEAR_PEAK_H
    if f.get('separator') == 'yes' or (not encroach and not close_pass and (f.get('vehicle_action') == 'parallel_in_own_lane'
                                                        or (g.get('lateral_toward_rider') or 0) < LATERAL_MIN)):
        return 'drop', 2, 'Adjacent-lane or separated traffic'
    if (f.get('rider_moved_into_vehicle_path') is True or pan) and not cosmos_entered:
        return 'rider', 5, 'Changed line into traffic'
    # v3: a driver verdict needs both models: Cosmos saw the vehicle enter the rider's path AND YOLO saw it
    # reach the rider corridor without a camera pan
    rule7 = (cosmos_entered and corridor and not pan and f.get('vehicle_action') in DRIVER_ACTIONS
             and f.get('rider_position') in ('bike_lane', 'traffic_lane') and f.get('separator') == 'no'
             and not red and not (crossing and signal_unknown)
             # v6: blind Cosmos says "entered path" readily; a driver verdict needs it in 4 of 5 calls
             and voted(votes, 'entered_path', True))
    if rule7:
        return 'driver', 7, 'Driver entered the rider\'s path'
    # v6: blind Cosmos, majority vote. YOLO saw an overtaking vehicle (close-pass shape, no pan) and at least 4 of 5
    # Cosmos calls say near miss with the vehicle entering the rider's path. Stricter on red than rule 3: a red
    # signal in the video OR the still blocks it. H1 and H4: 5/5; adjacent-lane clips: 0/5.
    overtake = bool(geo_row.get('pass')) and not pan
    if (overtake and voted(votes, 'near_miss', True) and voted(votes, 'entered_path', True)
            and f.get('separator') != 'yes' and f.get('rider_moved_into_vehicle_path') is not True
            and f.get('rider_position') in ('bike_lane', 'traffic_lane')
            and f.get('signal_for_rider') != 'red' and still_signal != 'red'):
        return 'driver', 7, 'Close pass: driver entered the rider\'s path'
    if (encroach or close_pass) and (g.get('peak_h') or 0) >= UNCLEAR_PEAK_H:
        return 'unclear', 8, 'Models disagree: you decide'
    if crossing and signal_unknown:
        return 'unclear', 9, 'Signal not visible: confirm you had right of way'
    return 'drop', 10, ''
