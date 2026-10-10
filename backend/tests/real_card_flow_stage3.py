"""Source-derived acceptance and full current-state HTTP decisions on real E5/API."""
import argparse
import json
from pathlib import Path
from app.catalog import Property
from app.questions import property_question_id
from real_semantics_stage2 import http


def run(base, output):
    records = []

    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')

    def post(label, body, expected_status=200):
        status, response = http(base, '/api/v1/analyses', body)
        records.append(dict(scenario=label, request=body, http_status=status, response=response))
        save()
        assert status == expected_status, (label, response)
        print(label, status, response.get('status'), response.get('selected_ktru_code'))
        return response

    # Record source cards and verify the specific checker result for reported facts.
    rows = json.loads(Path('docs/reviews/stage3/source-selection.json').read_text(encoding='utf-8'))
    cards = {}
    for row in rows:
        if row.get('is_template'):
            continue
        status, card = http(base, '/api/v1/catalog/positions/' + row['code'])
        assert status == 200
        cards[row['code']] = card
        records.append(dict(route='GET /api/v1/catalog/positions/' + row['code'], http_status=status, response=card))
    save()

    body = dict(description='Молоток деревянный', confirmed_category_id='25.73.30.141-00000011')
    initial = post('ambiguous material owner', body)
    q = initial['next_question']
    accepted = post('accept material owner', dict(body, answers=[dict(question_id=q['id'], value=q['options'][0]['value'])]))
    assert any(f['name'] == 'Материал рукоятки' and f['value'] == 'Дерево' and not f['mappings'] for f in accepted['facts'])
    assert not accepted['next_question'] or accepted['next_question']['id'] != q['id']
    rejected = post('reject material owner', dict(body, answers=[dict(question_id=q['id'], value='reject')]))
    assert any(f['name'] == '' and f['raw'] == 'деревянный' for f in rejected['facts'])

    bad = post('explicit incompatible wooden striker', dict(body, description='Молоток с деревянным бойком'))
    assert bad['excluded'] and bad['selected_ktru_code'] is None
    assert all(any(c['name'] == 'Материал бойка' and c['outcome'] == 'incompatible' for c in p['checks']) for p in bad['excluded'])
    post('manual excluded striker code forbidden', dict(body, description='Молоток с деревянным бойком',
         confirmed_ktru_code=bad['excluded'][0]['ktru_code']), 422)
    for candidate in bad['candidates']:
        assert candidate['outcome'] == 'unknown'
        unverified = post('manual unknown competitor cannot prove material', dict(body,
                          description='Молоток с деревянным бойком', confirmed_ktru_code=candidate['ktru_code']))
        assert unverified['status'] != 'resolved' and unverified['selected_ktru_code'] is None

    body = dict(description='ловпата совкавая', confirmed_category_id='25.99.29.120-00004')
    initial = post('value typo pending', body)
    q = initial['next_question']
    assert q['kind'] == 'correction'
    corrected = post('accept value typo', dict(body, answers=[dict(question_id=q['id'], value=q['options'][0]['value'])]))
    assert any(f['name'] == 'Тип лопаты' and f['value'] == 'Совковая' for f in corrected['facts'])
    assert corrected['next_question'] is None or corrected['next_question']['property_name'] != 'Тип лопаты'
    rejected = post('reject value typo', dict(body, answers=[dict(question_id=q['id'], value='reject')]))
    assert any(f['name'] == '' and f['raw'] == 'совкавая' for f in rejected['facts'])
    unknown = post('unknown value typo answer', dict(body, answers=[dict(question_id=q['id'], unknown=True)]))
    assert unknown['selected_ktru_code'] is None and unknown['next_question']['id'] == q['id']

    body = dict(description='Футболка мужская размер 40', confirmed_category_id='14.14.30.110-00000003')
    initial = post('shirt full card', body)
    fact = next(f for f in initial['facts'] if f['name'] == 'Российский размер')
    current = dict(body, attributes=[dict(id='size-edit', name='Российский размер', value='42', replaces=fact['id'])])
    edited = post('explicit size edit rechecks', current)
    assert [f['value'] for f in edited['facts'] if f['name'] == 'Российский размер'] == ['42']
    wrong = post('size physical unit rejected', dict(body, description='Футболка мужская размер 40 см'))
    assert wrong['selected_ktru_code'] is None and any(i['code'] == 'unit_mismatch' for i in wrong['issues'])
    conflict = post('size conflict', dict(body, attributes=[dict(id='conflict', name='Российский размер', value='42')]))
    assert any(i['code'] == 'conflicting_facts' for i in conflict['issues'])
    assert conflict['selected_ktru_code'] is None
    prop = Property.model_validate(next(p for p in cards['14.14.30.110-00000002']['position']['properties'] if p['name']=='Российский размер'))
    qid = property_question_id(body['confirmed_category_id'], prop)
    unknown = post('unknown size replaces earlier fact', dict(body, answers=[dict(question_id=qid, unknown=True)]))
    assert any(f['name'] == 'Российский размер' and f['unknown'] for f in unknown['facts'])
    assert unknown['selected_ktru_code'] is None
    # Code is chosen from the actual API pool and only after verified uniqueness.
    if edited['verification']['value_verified'] and edited['candidate_count'] == 1:
        code = edited['candidates'][0]['ktru_code']
        final = post('confirm verified real shirt code', dict(current, confirmed_ktru_code=code))
        assert final['status'] == 'resolved' and final['selected_ktru_code'] == code

    changed = post('category change preserves user answer', dict(description='Стул без подлокотников',
                   confirmed_category_id='31.01.11.150-00000004', answers=[dict(question_id=qid, value='40')]))
    old = next(f for f in changed['facts'] if f['id'] == 'answer:' + qid)
    assert old['value'] == '40' and not old['mappings']
    save()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--base', default='http://127.0.0.1:8000')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    run(a.base, a.output)
