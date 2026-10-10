"""Full real HTTP evidence; extraction acceptance is separate from search acceptance."""
import argparse
import json
from pathlib import Path
from real_semantics_stage2 import http

CASES = [
    ('hammer-ambiguous', 'молоток деревянный', '25.73.30.141-00000011'),
    ('hammer-handle', 'молоток с деревянной рукояткой', '25.73.30.141-00000011'),
    ('mallet', 'киянка деревянная', '25.73.30.290-00000004'),
    ('coat', 'пальто мужское синее, размер 48, без капюшона', '14.13.10.000-00000033'),
    ('shovel-negative', 'лопата совковая без черенка', '25.99.29.120-00004'),
    ('shovel-typo', 'ловпата совкавая', '25.99.29.120-00004'),
    ('shirt-size-before', 'футболка мужская 40 размер', '14.14.30.110-00000003'),
    ('shirt-size-after', 'футболка мужская размер 40', '14.14.30.110-00000003'),
    ('shirt-size-explicit', 'футболка мужская, российский размер 40', '14.14.30.110-00000003'),
    ('threshold-anonymous', 'порожки напольные 3 мм', None),
    ('threshold-explicit', 'порожки напольные толщиной 3 мм', None),
    ('chair', 'стул без подлокотников', '31.01.12.160-00000004'),
    ('metal-chair', 'Стул на металлическом каркасе без подлокотников', '31.01.11.150-00000004'),
    ('desk-angular', 'Стол письменный угловой', '31.01.10.000-00000006'),
    ('leather-jacket', 'Куртка зимняя из кожи женская', '14.11.10.000-00000006'),
    ('saw', 'Пила сабельная; Тип обрабатываемого материала: Дерево', '28.24.11.000-00000024'),
    ('saw-negative-alias', 'Пила сабельная деревянная', '28.24.11.000-00000024'),
]


def run(base, output, verify=False):
    records = []
    for name, text, category in CASES:
        for confirmed in (False, True) if category else (False,):
            body = dict(description=text, candidate_limit=100)
            if confirmed:
                body['confirmed_category_id'] = category
            status, result = http(base, '/api/v1/analyses', body)
            records.append(dict(scenario=name, confirmed=confirmed, request=body, http_status=status, response=result))
            assert status == 200, (name, result)
            if verify and confirmed:
                facts = result['facts']
                if name == 'hammer-handle':
                    assert any(f['name'] == 'Материал рукоятки' and f['value'] == 'Дерево' and f['card_mappings'] and not f['mappings'] for f in facts)
                if name in ('hammer-ambiguous', 'mallet'):
                    assert result['next_question']['kind'] == 'correction'
                    assert not any(f['name'] == 'Материал бойка' for f in facts)
                if name.startswith('shirt-size'):
                    assert any(f['name'] == 'Российский размер' and f['value'] == '40' and not f['unit'] and not f['unknown'] for f in facts)
                if name in ('coat', 'shovel-negative'):
                    prop = 'Половой признак' if name == 'coat' else 'Тип лопаты'
                    assert any(f['name'] == prop and not f['unknown'] and not f['negated'] for f in facts)
                if name in ('chair', 'metal-chair'):
                    assert any(f['name'] == 'Наличие подлокотников' and f['value'] == 'Нет' and f['negation_interpretation'] == 'canonical_absence' for f in facts)
                if name == 'desk-angular': assert any(f['name'] == 'Конфигурация стола' and f['value'].lower() == 'угловой' for f in facts)
                if name == 'leather-jacket': assert any(f['name'] == 'Половой признак' and f['value'].lower() == 'женская' for f in facts)
                if name == 'saw': assert any(f['name'] == 'Тип обрабатываемого материала' and f['value'] == 'Дерево' for f in facts)
                if name == 'saw-negative-alias': assert not any(f['name'] == 'Тип обрабатываемого материала' for f in facts)
                assert all(f['raw'] == text[slice(*f['span'])] for f in facts if f['source'] == 'text')
            print(name, confirmed, result['status'], [(f['name'], f['value'], f['unknown']) for f in result['facts']])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--base', default='http://127.0.0.1:8000')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--verify', action='store_true')
    a = p.parse_args()
    run(a.base, a.output, a.verify)
