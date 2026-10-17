"""Better Biker Coach: collate every rider-fault incident into one report on how to ride better.

Plain Python groups and counts (as in Stage D, no LLM decides anything); W&B Inference writes the coaching
text from those counts and the per-incident lessons; Weave traces each step and keeps every version of the
report as the object `rider-coach-report`.

  python3 engine/coach.py           # -> data/coach.json + data/coach.md + a W&B Report

The W&B Report ("Your riding coach") is updated in place on every run (its URL is kept in coach.json as
`wandb_report`); it needs the `wandb-workspaces` package and is skipped without it.

Reads data/incidents.json (after build_incidents.py, so the cyclist's reviews already apply). Dismissed
incidents are left out. Needs WANDB_API_KEY; Model: STAGE_E_MODEL, default openai/gpt-oss-120b.
"""
import collections, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report  # noqa: E402
import wb_trace as trace  # noqa: E402

DATA = report.DATA
OUT_JSON = os.path.join(DATA, 'coach.json')
OUT_MD = os.path.join(DATA, 'coach.md')

ISSUES = {  # event_type -> name the cyclist sees; order is the tie-break
    'entered_on_red': 'Entering junctions on red',
    'sidewalk': 'Riding on the sidewalk',
    'changed_line': 'Changing line into traffic',
    'door_zone': 'Riding in the door zone',
}
BANNED = re.compile(r'\b(fine|penalt|offence|offense|illegal|law|ticket)\w*', re.I)

SYSTEM = report.SYSTEM.replace('short documents', 'a private coaching report') + """
You are a friendly, experienced cycling coach. The counts are shown to the cyclist next to your text, so write
no digits at all: no counts, percentages, distances, seconds or times. Do not invent places or situations
(entrances, busy junctions, weather) that the input does not mention. Never suggest any way of going on a red
signal, riding closer to traffic, or accepting a small gap. Never blame drivers; this report is about what the
cyclist can control. Call the light "the signal facing you"; there is no special cyclist signal."""

ISSUE = """Coach the cyclist on ONE recurring habit seen across their rides: "{name}".
You get counts and context gathered by fixed rules, and the short lessons already written for each clip.
Find the pattern behind them (where and when it happens), not a list of incidents.{once}
JSON: {{"headline": "one short encouraging sentence naming the habit",
 "pattern": "2-3 sentences on what tends to happen and in what situations",
 "why_it_matters": "1-2 sentences on the risk",
 "drills": ["3 short concrete things to practise on the next rides"],
 "cue": "a 3-6 word cue to remember at the moment it matters"}}"""

OVERVIEW = """Write the opening of the cyclist's coaching report from the habits below (most frequent first).
JSON: {{"summary": "2-3 sentences: overall picture, supportive, honest",
 "priorities": ["the 3 changes that would make the biggest difference, most important first"],
 "encouragement": "one sentence"}}"""

CRITIC = """You check a cycling coach's draft before a cyclist reads it. INPUT has the facts the coach was given
and the DRAFT. List every problem of these kinds:
 - unsafe: advice that could put the cyclist in danger (going on red in any way, accepting a small gap,
   riding closer to traffic or pedestrians, taking eyes off the road)
 - invented: a situation, place, vehicle or condition not supported by the facts
 - pattern: ONLY when INPUT "count" is 1, calling it a habit or "often" (with a higher count it IS a habit)
 - blame or legal: blaming a driver, or mentioning laws, fines or offences
JSON: {{"ok": true|false, "problems": ["<kind>: <the exact phrase> -> <what to do instead>"]}}"""
ONCE = ("\nThis happened only ONCE, so it is not a habit: do not say often, tend to or usually; "
        "coach it as a one-off worth avoiding.")
DIGIT = re.compile(r'\d')


def text(v):
    """One string from a model field that may come back as a list (or a stringified one)."""
    if isinstance(v, str) and v.strip().startswith('['):
        try:
            v = json.loads(v.replace("'", '"'))
        except ValueError:
            pass
    return ' '.join(str(x).strip() for x in v) if isinstance(v, list) else str(v or '').strip()


def items(v):
    if isinstance(v, str) and v.strip().startswith('['):
        try:
            v = json.loads(v.replace("'", '"'))
        except ValueError:
            pass
    return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else [s for s in [text(v)] if s]


@trace.op()
def collect(incidents):
    """Rider-fault incidents the cyclist has not dismissed, grouped by habit, with counts and context."""
    kept = [i for i in incidents if i.get('fault') == 'rider'
            and (i.get('review') or {}).get('status') != 'dismissed']
    groups = collections.defaultdict(list)
    for i in kept:
        groups[i.get('event_type') or 'other'].append(i)
    issues = []
    for event, rows in groups.items():
        facts = [i.get('facts') or {} for i in rows]
        sev = [i.get('severity') or 0 for i in rows]
        vehicles = [((f.get('vehicle') or {}).get('type')) for f in facts]
        rows.sort(key=lambda i: -(i.get('severity') or 0))
        issues.append({
            'issue': event, 'name': ISSUES.get(event, event.replace('_', ' ').capitalize()),
            'count': len(rows), 'share': round(len(rows) / len(kept), 2),
            'rides': dict(collections.Counter(i.get('ride') for i in rows).most_common()),
            'severity_avg': round(sum(sev) / len(sev)), 'severity_max': max(sev),
            'confirmed_by_you': sum((i.get('review') or {}).get('status') == 'approved' for i in rows),
            'context': {
                'where_you_were': dict(collections.Counter(f.get('rider_position') for f in facts)),
                'signal': dict(collections.Counter(f.get('signal_for_rider') for f in facts)),
                'vehicle_nearby': sum(v not in (None, 'none') for v in vehicles),
                'vehicle_types': dict(collections.Counter(v for v in vehicles if v not in (None, 'none'))),
                'vehicle_within_1m': sum(f.get('closest_distance') == 'under_1m' for f in facts),
                'low_light': sum(bool(i.get('low_light')) for i in rows),
            },
            'examples': [{'id': i['id'], 'ride': i.get('ride'), 'severity': i.get('severity'),
                          'at': f"{int(i.get('ride_offset_sec') or 0) // 60}:{int(i.get('ride_offset_sec') or 0) % 60:02d}"}
                         for i in rows[:3]],
            'lessons': [{k: text((i.get('document') or {}).get(k)) for k in ('what_happened', 'why_risky', 'next_time')}
                        for i in rows[:6]],
        })
    issues.sort(key=lambda g: (-g['count'], list(ISSUES).index(g['issue']) if g['issue'] in ISSUES else 99))
    unclear = sum(i.get('fault') == 'unclear' and (i.get('review') or {}).get('status') != 'dismissed'
                  for i in incidents)
    return {'rider_incidents': len(kept), 'issues': issues, 'unclear_to_review': unclear,
            'rides': sorted({i.get('ride') for i in kept})}


def flat(doc):
    return ' '.join(' '.join(v) if isinstance(v, list) else v for v in doc.values())


@trace.op()
def rule_check(doc):
    """Hard checks in plain Python: every field filled, no digits, no legal words."""
    problems = [f'empty: {f}' for f, v in doc.items() if not v]
    problems += [f'digit: "{m}" -> no numbers' for m in re.findall(r'[^.;]*\d[^.;]*', flat(doc))]
    problems += [f'legal: "{m}" -> no legal claims' for m in BANNED.findall(flat(doc))]
    return problems


@trace.op()
def critique(payload, doc, model, key, project):
    messages = [{'role': 'system', 'content': 'You are a strict cycling-safety editor. Answer ONLY with one JSON object.'},
                {'role': 'user', 'content': CRITIC + '\n\nINPUT:\n' + json.dumps(payload, indent=1)
                                            + '\n\nDRAFT:\n' + json.dumps(doc, indent=1)}]
    out = report.parse_json(report.call_llm(messages, model, key, project))
    return [] if out.get('ok') is True and not out.get('problems') else items(out.get('problems')) or ['unspecified']


@trace.op()
def write(prompt, payload, fields, model, key, project, lists=(), rounds=3):
    """Writer and critic: draft, check (rules, then a second model call), rewrite with the problems."""
    messages = [{'role': 'system', 'content': SYSTEM},
                {'role': 'user', 'content': prompt + '\n\nINPUT:\n' + json.dumps(payload, indent=1)}]
    doc, problems = None, []
    for _ in range(rounds):
        out = report.parse_json(report.call_llm(messages, model, key, project))
        doc = {f: items(out.get(f)) if f in lists else text(out.get(f)) for f in fields}
        problems = rule_check(doc) or critique(payload, doc, model, key, project)
        if not problems:
            return {**doc, 'checks': 'passed'}
        messages += [{'role': 'assistant', 'content': json.dumps(out)},
                     {'role': 'user', 'content': 'An editor found these problems. Rewrite the whole JSON fixing all '
                                                 'of them:\n- ' + '\n- '.join(problems)}]
    if rule_check(doc):
        raise RuntimeError(f'still failing hard checks: {rule_check(doc)}')
    return {**doc, 'checks': 'editor concerns left: ' + '; '.join(problems)}


@trace.op(call_display_name=lambda c: f"coach {c.inputs['issue']['issue']}")
def coach_issue(issue, model, key, project):
    payload = {k: issue[k] for k in ('name', 'count', 'share', 'rides', 'severity_avg', 'context', 'lessons')}
    once = ONCE if issue['count'] == 1 else ''
    return write(ISSUE.format(name=issue['name'], once=once), payload, ('headline', 'pattern', 'why_it_matters', 'drills', 'cue'),
               model, key, project, lists=('drills',))


@trace.op()
def coach_overview(issues, model, key, project):
    payload = [{'habit': g['name'], 'count': g['count'], 'share': g['share'], 'headline': g['coach']['headline'],
                'cue': g['coach']['cue']} for g in issues]
    return write(OVERVIEW, payload, ('summary', 'priorities', 'encouragement'), model, key, project,
               lists=('priorities',))


@trace.op(postprocess_inputs=lambda i: {'incidents': len(i['incidents']), 'model': i['model']})
def build_report(incidents, model):
    key, project = os.environ['WANDB_API_KEY'], report.wandb_project()
    data = collect(incidents)
    for g in data['issues']:
        g['coach'] = coach_issue(g, model, key, project)
        g.pop('lessons')
    data['overview'] = coach_overview(data['issues'], model, key, project) if data['issues'] else None
    data['generated_by'] = f'W&B Inference · {model}'
    return data


def markdown(r):
    if not r['issues']:
        return '# Your riding coach\n\nNo rider-fault incidents. Nice riding.\n'
    o = r['overview']
    out = ['# Your riding coach', '',
           f"{r['rider_incidents']} moments across {len(r['rides'])} rides where your own choice raised the risk.", '',
           o['summary'], '', '## Top priorities', ''] + [f'{n}. {p}' for n, p in enumerate(o['priorities'], 1)]
    for g in r['issues']:
        c = g['coach']
        rides = ', '.join(f'{k} ({v})' for k, v in g['rides'].items())
        out += ['', f"## {g['name']}: {'once' if g['count'] == 1 else str(g['count']) + ' times'} ({round(g['share'] * 100)}%)", '',
                f"*{c['headline']}*", '', c['pattern'], '', f"**Why it matters:** {c['why_it_matters']}", '',
                '**Practise:**'] + [f'- {d}' for d in c['drills']] + [
                '', f"**Cue:** \"{c['cue']}\"", '',
                f"Rides: {rides}. Worst clips: " + ', '.join(f"{e['ride']} at {e['at']}" for e in g['examples']) + '.']
    if r['unclear_to_review']:
        out += ['', f"{r['unclear_to_review']} more moments are unclear and wait for your review; "
                    'any you mark as yours will join this report.']
    out += ['', o['encouragement'], '', f"_{r['generated_by']}. Counts come from fixed rules, not the model._"]
    return '\n'.join(out) + '\n'


def publish_report(r, md, url=None):
    """The report as a W&B Report page in our project; updates `url` when given. Returns the URL or None."""
    try:
        import wandb_workspaces.reports.v2 as wr
    except ImportError:
        print('W&B Report skipped: pip install wandb-workspaces')
        return None
    entity, _, name = trace.project(trace.settings()).partition('/')
    body = md.split('\n', 2)[2]  # the Report has its own title
    weave_url = f'https://wandb.ai/{entity}/{name}/weave/objects/rider-coach-report/versions'
    blocks = [wr.TableOfContents(), wr.MarkdownBlock(text=body), wr.HorizontalRule(),
              wr.MarkdownBlock(text=f'Every version of this report, with the traced writer and critic calls behind it: '
                                    f'[Weave: rider-coach-report]({weave_url}).')]
    title, desc = 'Your riding coach', f"{r['rider_incidents']} rider-fault moments across {len(r['rides'])} rides, grouped by habit"
    with trace.own_key():
        try:
            rep = wr.Report.from_url(url) if url else None
        except Exception as e:  # deleted or not ours: make a new one
            print(f'W&B Report {url} not reusable ({type(e).__name__}); creating a new one')
            rep = None
        rep = rep or wr.Report(entity=entity, project=name)
        rep.title, rep.description, rep.blocks, rep.width = title, desc, blocks, 'readable'
        rep.save()
    return rep.url


def main():
    report.load_env()
    if not os.environ.get('WANDB_API_KEY', '').strip():
        sys.exit('WANDB_API_KEY is not set')
    model = os.environ.get('STAGE_E_MODEL', '').strip() or 'openai/gpt-oss-120b'
    trace.init()
    incidents = json.load(open(report.INCIDENTS))['incidents']
    previous = json.load(open(OUT_JSON)) if os.path.exists(OUT_JSON) else {}
    r = build_report(incidents, model)
    md = markdown(r)
    if trace.client:
        trace.weave.publish(r, name='rider-coach-report')
        r['wandb_report'] = publish_report(r, md, previous.get('wandb_report')) or previous.get('wandb_report')
    json.dump(r, open(OUT_JSON, 'w'), indent=1)
    open(OUT_MD, 'w').write(md)
    print(f"coach: {r['rider_incidents']} rider incidents, {len(r['issues'])} habits -> {OUT_JSON}, {OUT_MD}")
    if r.get('wandb_report'):
        print(f"W&B Report: {r['wandb_report']}")


if __name__ == '__main__':
    main()
