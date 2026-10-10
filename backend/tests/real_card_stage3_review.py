"""Record complete HTTP state against the running real snapshot/E5 (no mocks)."""
import argparse
import json
from pathlib import Path
from real_semantics_stage2 import http

SHIRT = '14.14.30.110-00000003'
COAT = '14.13.10.000-00000033'
CASES = [
    ('foreign-before', 'Футболка мужская EU 40 размер', SHIRT),
    ('foreign-after', 'Футболка мужская 40 размер EU', SHIRT),
    ('foreign-coat', 'Пальто мужское 48 размер EU', COAT),
    ('foreign-pair', 'Футболка мужская; Российский размер: EU 40', SHIRT),
    ('foreign-pair-after', 'Футболка мужская; Российский размер: 40 EU', SHIRT),
    ('foreign-name', 'Футболка мужская европейского размера 40', SHIRT),
    ('foreign-us', 'Футболка мужская US 40 размер', SHIRT),
    ('foreign-letter', 'Футболка мужская размер XL', SHIRT),
    ('foreign-no-label', 'Футболка мужская EU 40', SHIRT),
    ('foreign-dictionary', 'Пальто мужское EU 48', COAT),
    ('foreign-attached-prefix', 'Футболка мужская EU40 размер', SHIRT),
    ('foreign-attached-suffix', 'Футболка мужская 40EU размер', SHIRT),
    ('foreign-fr', 'Футболка мужская FR40 размер', SHIRT),
    ('foreign-italian', 'Футболка мужская итальянского размера 40', SHIRT),
    ('morph-before', 'Футболка мужская 40 размера', SHIRT),
    ('morph-after', 'Футболка мужская размера 40', SHIRT),
    ('morph-ordinal', 'Футболка мужская 40-го размера', SHIRT),
    ('adjacent', 'Футболка мужская 40 размер 3 мм', SHIRT),
    ('adjacent-comma', 'Футболка мужская 40 размер, 3 мм', SHIRT),
    ('adjacent-semicolon', 'Футболка мужская 40 размер; 3 мм', SHIRT),
    ('adjacent-line', 'Футболка мужская 40 размер\n3 мм', SHIRT),
    ('segment-semicolon', 'Пальто мужское; искусственный; утеплитель', COAT),
    ('segment-pipe', 'Пальто мужское | искусственный | утеплитель', COAT),
    ('segment-line', 'Пальто мужское\nискусственный\nутеплитель', COAT),
    ('negative', 'Футболка мужская размер 40; не мужская', SHIRT),
]


def run(base, output, verify):
    records = []
    def record(name, body):
        status, result = http(base, '/api/v1/analyses', body)
        records.append(dict(scenario=name, request=body, http_status=status, response=result))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        print(name, status, result.get('status'), [(f['name'], f['value'], f['unknown']) for f in result.get('facts', [])])
        if verify:
            assert status == 200, (name, result)
            assert result['scope']['complete'] and result['scope']['position_count'] == result['scope']['evaluated_count']
            assert all(f['raw'] == body['description'][slice(*f['span'])] for f in result['facts'] if f['source'] == 'text')
        return result
    for name, text, category in CASES:
        body = dict(description=text, confirmed_category_id=category, candidate_limit=100)
        result = record(name, body)
        if verify:
            if name.startswith('foreign'):
                assert not any(f['name'] == 'Российский размер' and not f['unknown'] for f in result['facts'])
                assert result['next_question'] and result['selected_ktru_code'] is None
            if name.startswith(('morph', 'adjacent')):
                assert any(f['name'] == 'Российский размер' and f['value'] == '40' and not f['unit'] for f in result['facts'])
            if name.startswith('adjacent'):
                assert any(f['value'] == '3' and f['unit'] == 'мм' for f in result['facts'])
                assert result['next_question'] and '3 мм' in result['next_question']['title']
                if name == 'adjacent':
                    q = result['next_question']
                    unknown = record('measurement-unknown', body | dict(answers=[dict(question_id=q['id'], unknown=True)]))
                    assert unknown['selected_ktru_code'] is None and (not unknown['next_question'] or unknown['next_question']['id'] != q['id'])
                    rejected = record('measurement-reject', body | dict(answers=[dict(question_id=q['id'], value='reject')]))
                    assert any(f['raw'] == '3 мм' and f['unit'] == 'мм' for f in rejected['facts'])
                    assert not any(i['code'] == 'unmapped_measurement' for i in rejected['issues'])
        if name.startswith('foreign') and result.get('candidates'):
            confirmed = record(name + '-confirm', body | dict(confirmed_ktru_code=result['candidates'][0]['ktru_code']))
            if verify:
                assert confirmed['status'] != 'resolved' and confirmed['selected_ktru_code'] is None
        if verify and name in ('foreign-before', 'foreign-coat'):
            uncertain = next(f for f in result['facts'] if f['assignment_options'])
            replacement = dict(id='explicit-size-edit', name='Российский размер', value=uncertain['value'], replaces=uncertain['id'])
            edited = record(name + '-explicit-edit', body | dict(attributes=[replacement]))
            assert not any(i['code'] == 'unresolved_fact_uncertainty' for i in edited['issues'])
            assert any(f['name'] == 'Российский размер' and not f['unknown'] for f in edited['facts'])
            if edited['verification']['value_verified'] and edited['candidate_count'] == 1:
                final = record(name + '-edit-confirm', body | dict(attributes=[replacement], confirmed_ktru_code=edited['candidates'][0]['ktru_code']))
                assert final['status'] == 'resolved'
        if name == 'negative' and result.get('next_question'):
            question = result['next_question']
            unknown = record('negative-unknown', body | dict(answers=[dict(question_id=question['id'], unknown=True)]))
            if verify:
                assert unknown['selected_ktru_code'] is None
                assert not unknown['next_question'] or unknown['next_question']['id'] != question['id']
                repeated = record('negative-unknown-retry', body | dict(answers=[dict(question_id=question['id'], unknown=True)]))
                assert repeated == unknown
                rejected = record('negative-reject', body | dict(answers=[dict(question_id=question['id'], value='reject')]))
                assert any(f['negated'] and not f['name'] for f in rejected['facts'])
                negative = next(f for f in result['facts'] if f['negated'])
                conflict = record('negative-explicit-conflict', body | dict(attributes=[dict(id='gender-edit', name=negative['name'], value='Женская', replaces=negative['id'])]))
                assert conflict['selected_ktru_code'] is None and any(i['code'] == 'conflicting_facts' for i in conflict['issues'])
    if verify:
        codes = {candidate['ktru_code'] for entry in records if entry['http_status'] == 200
                 for candidate in entry['response']['candidates'] + entry['response']['excluded']}
        for code in sorted(codes):
            status, card = http(base, '/api/v1/catalog/positions/' + code)
            assert status == 200
            records.append(dict(route='GET /api/v1/catalog/positions/' + code, http_status=status, response=card))
        output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8000')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    run(args.base, args.output, args.verify)
