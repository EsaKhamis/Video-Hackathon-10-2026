"""Stage E: W&B Inference writes the authority report and rider lesson text from the facts.

Reads data/incidents.json, asks the LLM for one document per incident, caches the text in
data/stage_e.json (keyed by segment source, so build_incidents.py can merge it back after a rebuild)
and writes it into incidents.json under `document`. The app uses `document.summary` for the authority
report and `what_happened` / `why_risky` / `next_time` for the rider lesson; without them it falls back
to its templates, so a failed call never breaks a page.

  python3 engine/report.py                  # every incident without cached text
  python3 engine/report.py --only driver    # driver reports only (or rider, unclear)
  python3 engine/report.py --force          # rewrite cached text too

Stdlib only. Needs WANDB_API_KEY (and WANDB_PROJECT, as team/project or with WANDB_TEAM) from the
environment or .env. Model: STAGE_E_MODEL, default openai/gpt-oss-120b.
"""
import argparse, json, os, re, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DATA = os.path.join(ROOT, 'data')
INCIDENTS = os.path.join(DATA, 'incidents.json')
CACHE = os.path.join(DATA, 'stage_e.json')
PARALLEL = 6


def load_env():
    path = os.path.join(ROOT, '.env')
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"\''))


def wandb_project():
    project = os.environ.get('WANDB_PROJECT', '').strip()
    team = os.environ.get('WANDB_TEAM', '').strip()
    return f'{team}/{project}' if team and project and '/' not in project else project


SYSTEM = """You write short documents for a cyclist safety app. Footage comes from the cyclist's helmet camera.
You are given facts that video models extracted and the verdict that fixed rules produced. Use ONLY those facts.
Never invent a plate, street, date, time, speed, injury, colour or anything not in the facts. If the vehicle
colour says "cyclist to confirm", do not name a colour. Distances are estimates from the video: say "about".
Never cite a law, offence, fine or penalty. Plain British English, no headings, no markdown, no emojis.
Answer ONLY with one JSON object."""

REPORT = """Write an AUTHORITY REPORT summary: a neutral, factual account a cyclist could send to the police
or a city service. Third person ("the cyclist", "the driver of the {vehicle}"). 3 to 5 sentences: where the
cyclist was riding, what the vehicle did and from which side, how close it came, the signal facing the cyclist
and whether anything separated them. Do not speculate about intent. Do not call it a crime.
JSON: {{"summary": "..."}}"""

LESSON = """Write a private RIDER LESSON for the cyclist. Second person ("you"), supportive and practical,
never preachy or blaming. General safety advice only. The lesson is about the cyclist's own choice given in
"why_this_is_a_rider_lesson": make that the centre of all three parts, and do not blame the vehicle.
JSON: {{"what_happened": "2-3 sentences on what the footage shows",
 "why_risky": "1-2 sentences on why it was risky",
 "next_time": "2-3 short concrete things to do differently next time"}}"""

BOTH = """The models disagree, so the cyclist will decide who was at fault. Write BOTH documents.
1) AUTHORITY REPORT summary: neutral, third person, 3 to 4 sentences; say that the closeness is estimated
   from the video. Do not mention models, rules, review or fault. 2) RIDER LESSON in the second person, supportive, general advice only.
JSON: {{"summary": "...", "what_happened": "...", "why_risky": "...", "next_time": "..."}}"""


def signal_fact(facts):
    """One signal value: the video and the still frame must agree, or it is 'not confirmed'."""
    video, still = facts.get('signal_for_rider'), facts.get('still_signal')
    seen = {s for s in (video, still) if s in ('red', 'green', 'yellow')}
    if len(seen) == 1 and (video == still or video in ('red', 'green', 'yellow')):
        return seen.pop()
    if seen:
        return 'not confirmed (video and still frame disagree; cyclist to confirm)'
    return 'none visible' if 'none_visible' in (video, still) else 'not visible'


def gap_fact(geo, facts):
    gap = geo.get('min_gap_m')
    if gap is None:
        return {'under_1m': 'under about 1 m', '1_to_2m': 'about 1 to 2 m',
                'over_2m': 'over about 2 m'}.get(facts.get('closest_distance'), 'unknown')
    return 'under about half a metre' if gap < 0.5 else 'about 1 m' if gap < 1.5 else f'about {round(gap)} m'


def facts_for_prompt(item):
    """Curated facts only: raw model fields (votes, model_fault, evidence notes) confuse the writer."""
    facts = item.get('facts') or {}
    geo = item.get('geometry') or {}
    vehicle = facts.get('vehicle') or {}
    offset = int(item.get('ride_offset_sec') or 0)
    colour = vehicle.get('colour') or ''
    out = {
        'verdict_by_rules': item.get('fault'),
        'event': item.get('event_type'),
        'ride_and_time': f"ride {item.get('ride')}, {offset // 60} min {offset % 60} s into the recording (date and clock time unknown)",
        'cyclist_position': facts.get('rider_position'),
        'vehicle': vehicle.get('type') or geo.get('label') or 'vehicle',
        'vehicle_colour': colour if colour and 'confirm' not in colour else 'not stated (cyclist to confirm)',
        'vehicle_side': geo.get('side') or vehicle.get('side'),
        'vehicle_action': facts.get('vehicle_action'),
        'vehicle_entered_cyclist_path': facts.get('vehicle_entered_rider_path'),
        'cyclist_moved_into_vehicle_path': facts.get('rider_moved_into_vehicle_path'),
        'cyclist_swerved_or_turned': geo.get('pan_suspect'),
        'physical_separator': facts.get('separator'),
        'closest_gap_estimate_from_video': gap_fact(geo, facts),
        'video_model_distance_estimate': facts.get('closest_distance'),
        'signal_facing_cyclist': signal_fact(facts),
        'low_light': item.get('ride') == 'GX050001',
    }
    if item.get('fault') == 'rider':
        out['why_this_is_a_rider_lesson'] = RIDER_REASON.get(item.get('fault_rule'),
                                                             (item.get('document') or {}).get('title', ''))
    return out


RIDER_REASON = {  # Stage D rule -> what the lesson is about
    3: 'The cyclist entered the intersection while the signal facing them was red; the vehicle had right of way.',
    4: 'The cyclist was riding on the sidewalk among pedestrians.',
    5: 'The cyclist changed line into the path of traffic.',
    6: 'The cyclist rode in the door zone of parked cars.',
}


def task_for(item):
    fault = item.get('fault')
    if fault == 'driver':
        return REPORT, ('summary',)
    if fault == 'rider':
        return LESSON, ('what_happened', 'why_risky', 'next_time')
    return BOTH, ('summary', 'what_happened', 'why_risky', 'next_time')


def call_llm(messages, model, key, project, tries=3):
    body = {'model': model, 'messages': messages, 'temperature': 0.2, 'max_tokens': 2500}
    if 'gpt-oss' in model:
        body['reasoning_effort'] = 'low'
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    if project:
        headers['OpenAI-Project'] = project
    base = os.environ.get('LLM_BASE_URL', '').strip() or 'https://api.inference.wandb.ai/v1'
    req = urllib.request.Request(base.rstrip('/') + '/chat/completions', json.dumps(body).encode(), headers)
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)['choices'][0]['message'].get('content') or ''
        except urllib.error.HTTPError as e:
            last = f'HTTP {e.code}: {e.read()[:200]!r}'
            if e.code < 500 and e.code != 429:
                break
        except Exception as e:  # timeouts, connection resets
            last = f'{type(e).__name__}: {e}'
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(last)


def parse_json(text):
    text = re.sub(r'^```(?:json)?|```$', '', text.strip(), flags=re.M)
    start, end = text.find('{'), text.rfind('}')
    return json.loads(text[start:end + 1])


def write_one(item, model, key, project):
    prompt, fields = task_for(item)
    vehicle = ((item.get('facts') or {}).get('vehicle') or {}).get('type') or 'vehicle'
    messages = [
        {'role': 'system', 'content': SYSTEM},
        {'role': 'user', 'content': prompt.format(vehicle=vehicle) + '\n\nFACTS:\n'
                                    + json.dumps(facts_for_prompt(item), indent=1)},
    ]
    for attempt in range(2):
        out = parse_json(call_llm(messages, model, key, project))
        doc = {f: str(out.get(f, '')).strip() for f in fields}
        if all(doc.values()) and not re.search(r'\b(fine|penalt|offence|offense|illegal)\w*', ' '.join(doc.values()), re.I):
            doc['generated_by'] = f'W&B Inference · {model}'
            return doc
    raise RuntimeError(f'bad output: {out}')


def merge(incidents, cache):
    """Copy cached text into each incident's document. Also used by build_incidents.py."""
    n = 0
    for item in incidents:
        doc = cache.get(item.get('source'))
        if doc and doc.get('verdict') == item.get('fault'):
            item.setdefault('document', {}).update({k: v for k, v in doc.items() if k != 'verdict'})
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', choices=['driver', 'rider', 'unclear'])
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    load_env()
    key = os.environ.get('WANDB_API_KEY', '').strip()
    if not key:
        sys.exit('WANDB_API_KEY is not set')
    model = os.environ.get('STAGE_E_MODEL', '').strip() or 'openai/gpt-oss-120b'
    project = wandb_project()

    data = json.load(open(INCIDENTS))
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    todo = [i for i in data['incidents']
            if (not args.only or i.get('fault') == args.only)
            and (args.force or (cache.get(i['source']) or {}).get('verdict') != i.get('fault'))]
    todo.sort(key=lambda i: -(i.get('severity') or 0))
    if args.limit:
        todo = todo[:args.limit]
    print(f'Stage E: {len(todo)} documents with {model}', flush=True)

    def run(item):
        t = time.time()
        try:
            doc = write_one(item, model, key, project)
            doc['verdict'] = item.get('fault')
            return item, doc, f'{time.time() - t:.1f}s'
        except Exception as e:
            return item, None, f'failed: {e}'

    ok = 0
    with ThreadPoolExecutor(PARALLEL) as pool:
        for item, doc, note in pool.map(run, todo):
            print(f"  {item['fault']:8} {item['source']}  {note}", flush=True)
            if doc:
                cache[item['source']] = doc
                ok += 1

    json.dump(cache, open(CACHE, 'w'), indent=1)
    n = merge(data['incidents'], cache)
    json.dump(data, open(INCIDENTS, 'w'), indent=1)
    print(f'{ok}/{len(todo)} written; {n}/{len(data["incidents"])} incidents carry W&B text -> {INCIDENTS}')


if __name__ == '__main__':
    main()
