"""HTTP regressions on small source excerpts, with test-only search vectors.

Full real-model/catalog acceptance is recorded by real_card_stage3.py instead.
"""
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.catalog import Catalog, Category, Position
from app.clean_supply import adapt_position
from conftest import runtime


@pytest.fixture
def card_client(monkeypatch):
    rows = json.loads(Path('docs/reviews/stage3/source-selection.json').read_text(encoding='utf-8'))
    rows += [r for r in json.loads(Path('docs/reviews/stage2-followup/source-selection.json').read_text(encoding='utf-8'))
             if r['code'].startswith('25.99.29.120')]
    positions = tuple({row['code']: adapt_position(row, 'test-source-excerpt') for row in rows if row.get('parent_position')}.values())
    parents = tuple({p.parent_code: Position(code=p.parent_code, name=p.name, is_template=True, status='actual',
                             source_path='test-only-parent') for p in positions}.values())
    cats = tuple(Category(id=p.code, name=p.name, search_text=p.name, position_codes=tuple(
                         q.code for q in positions if q.parent_code == p.code),
                          link_verified=True, coverage_verified=True, evidence='source excerpt; test scope only')
                 for p in parents)
    catalog = Catalog(catalog_version='real-excerpts-test-only', source_description='Unit fixture; not full catalog',
                      validation_report='test-only', synthetic=True, positions=(*positions, *parents), categories=cats)
    import app.main as main
    monkeypatch.setattr(main, 'resources', lambda: runtime(catalog))
    with TestClient(main.app) as client:
        yield client


def post(client, description, scope='25.73.30.141-00000011', **state):
    response = client.post('/api/v1/analyses', json=dict(description=description, confirmed_category_id=scope, **state))
    assert response.status_code == 200, response.text
    return response.json()


def test_material_part_and_preserved_provenance(card_client):
    text = 'Молоток с деревянной рукояткой'
    result = post(card_client, text)
    fact = next(f for f in result['facts'] if f['name'] == 'Материал рукоятки')
    assert fact['value'] == 'Дерево' and fact['surface_value'] == 'деревянной'
    assert fact['raw'] == text[slice(*fact['span'])]
    assert fact['card_mappings'] and fact['extraction_evidence']
    assert not fact['mappings']  # This PP field is not a KTRU proof.
    assert result['selected_ktru_code'] is None


def test_material_without_part_requires_decision_and_rejection_keeps_raw(card_client):
    text = 'Молоток деревянный'
    first = post(card_client, text)
    assert not any(f['name'] == 'Материал бойка' for f in first['facts'])
    q = first['next_question']
    assert q['kind'] == 'correction' and 'Материал рукоятки' in str(q['options'])
    accepted = post(card_client, text, answers=[dict(question_id=q['id'], value=q['options'][0]['value'])])
    assert any(f['name'] == 'Материал рукоятки' and f['value'] == 'Дерево' for f in accepted['facts'])
    assert accepted['next_question'] is None or accepted['next_question']['id'] != q['id']
    rejected = post(card_client, text, answers=[dict(question_id=q['id'], value='reject')])
    assert any(f['name'] == '' and f['raw'] == 'деревянный' for f in rejected['facts'])
    assert rejected['selected_ktru_code'] is None


@pytest.mark.parametrize('text', ['Футболка мужская 40 размер', 'Футболка мужская размер 40',
                                'Футболка мужская, российский размер 40'])
def test_designation_without_fake_unit(card_client, text):
    result = post(card_client, text, '14.14.30.110-00000003')
    fact = next(f for f in result['facts'] if f['name'] == 'Российский размер')
    assert fact['value'] == '40' and fact['unit'] is None and not fact['unknown']
    assert fact['raw'] == text[slice(*fact['span'])]
    checks = [c for p in result['candidates'] + result['excluded'] for c in p['checks'] if c['name'] == fact['name']]
    assert all(c['outcome'] == 'compatible' for c in checks)


def test_foreign_size_not_converted(card_client):
    result = post(card_client, 'Футболка, европейский размер 40', '14.14.30.110-00000003')
    assert not any(f['name'] == 'Российский размер' for f in result['facts'])
    assert result['unparsed']


def test_shovel_negation_does_not_change_type(card_client):
    text = 'Лопата совковая без черенка'
    result = post(card_client, text, '25.99.29.120-00004')
    typ = next(f for f in result['facts'] if f['name'] == 'Тип лопаты')
    assert not typ['negated'] and not typ['unknown']
    negative = next(f for f in result['facts'] if f['negated'])
    assert negative['unknown'] and negative['raw'] == 'без черенка' and negative['uncertainty_reason']


def test_scoped_absence(card_client):
    text = 'Стул без подлокотников'
    result = post(card_client, text, '31.01.12.160-00000004')
    fact = next(f for f in result['facts'] if f['name'] == 'Наличие подлокотников')
    assert fact['value'] == 'Нет' and fact['negated'] and not fact['unknown']
    assert fact['negation_interpretation'] == 'canonical_absence'
    assert fact['raw'] == text[slice(*fact['span'])]
    checks = [c for p in result['candidates'] + result['excluded'] for c in p['checks'] if c['name'] == fact['name']]
    assert all(c['outcome'] == 'compatible' for c in checks)


def test_coat_gender_color_and_absence_do_not_negate_each_other(card_client):
    text = 'пальто мужское синее, размер 48, без капюшона'
    result = post(card_client, text, '14.13.10.000-00000033')
    facts = {f['name']: f for f in result['facts']}
    assert facts['Половой признак']['value'] == 'мужское'
    assert not facts['Половой признак']['unknown'] and not facts['Половой признак']['negated']
    assert facts['Цвет']['value'] == 'Синий'
    assert facts['Российский размер']['value'] == '48'
    assert facts['Тип капюшона']['value'] == 'Отсутствует' and facts['Тип капюшона']['negated']
    assert all(f['raw'] == text[slice(*f['span'])] for f in facts.values())
    assert not any(c['name'] in facts and c['outcome'] == 'compatible' and
                   result['next_question'] and result['next_question']['property_name'] == c['name']
                   for p in result['candidates'] for c in p['checks'])


def test_measurement_after_size_is_not_invented_unit(card_client):
    result = post(card_client, 'Футболка размер 40 мужская', '14.14.30.110-00000003')
    fact = next(f for f in result['facts'] if f['name'] == 'Российский размер')
    assert fact['unit'] is None
    wrong = post(card_client, 'Футболка размер 40 см', '14.14.30.110-00000003')
    assert wrong['selected_ktru_code'] is None
    assert any(i['code'] == 'unit_mismatch' for i in wrong['issues'])


def test_previous_category_answer_retained_without_old_mapping(card_client):
    from app.catalog import Property
    from app.questions import property_question_id
    original = json.loads(Path('docs/reviews/stage3/source-selection.json').read_text(encoding='utf-8'))
    shirt = adapt_position(next(r for r in original if r['code'] == '14.14.30.110-00000002'), 'unit-only')
    prop = next(p for p in shirt.properties if p.name == 'Российский размер')
    qid = property_question_id(shirt.parent_code, prop)
    result = post(card_client, 'Стул', '31.01.12.160-00000004', answers=[dict(question_id=qid, value='40')])
    fact = next(f for f in result['facts'] if f['id'] == 'answer:' + qid)
    assert fact['value'] == '40' and fact['mappings'] == []
    assert fact['card_mappings'] == [] and fact['source'] == 'answer'


def test_card_color_remains_and_edit_replaces_source(card_client):
    text = 'Молоток синий'
    first = post(card_client, text)
    fact = next(f for f in first['facts'] if f['name'] == 'Цвет')
    edited = post(card_client, text, attributes=[dict(id='color', name='Цвет', value='Красный', replaces=fact['id'])])
    assert [(f['value'], f['source']) for f in edited['facts'] if f['name'] == 'Цвет'] == [('Красный', 'input')]
    assert edited['original_facts'][0]['raw'] == 'синий'


def test_anonymous_measurement_and_metric_equivalence():
    from app.extraction import extract
    from app.checks import signature
    from app.catalog import Property
    from app.units import get_unit
    prop = Property(id='fixture-thickness', name='Толщина', value_type='number')
    facts = []
    for text in ['порожки напольные 3 мм', 'порожки напольные 0,3 см', 'порожки напольные 0,003 м']:
        result, _ = extract(text, ('Толщина',))
        fact = result[0]
        assert fact.name == '' and fact.raw == text[slice(*fact.span)]
        facts.append(fact)
    assert len({signature(f, prop, lambda p, u: get_unit(u)) for f in facts}) == 1
    explicit, _ = extract('порожки напольные толщиной 3 мм', ('Толщина',))
    assert explicit[0].name == 'Толщина'


def test_unknown_information_never_disappears(card_client):
    result = post(card_client, 'Молоток с необычной отделкой')
    assert any('необычной отделкой' in f['raw'] for f in result['unparsed'])


def test_client_cannot_supply_card_verification(card_client):
    response = card_client.post('/api/v1/analyses', json=dict(description='Молоток', attributes=[
        dict(id='spoof', name='Цвет', value='Синий', card_mappings=['trusted'])]))
    assert response.status_code == 422


def test_explicit_edit_uses_same_context_forms(card_client):
    result = post(card_client, 'Пальто', '14.13.10.000-00000033', attributes=[
        dict(id='gender', name='Половой признак', value='мужская')])
    fact = next(f for f in result['facts'] if f['id'] == 'gender')
    assert fact['value'] == 'Мужское' and fact['surface_value'] == 'мужская' and fact['raw'] == 'мужская'
    assert fact['extraction_evidence'] and fact['mappings']


def test_material_adjective_does_not_invent_processed_material(card_client):
    result = post(card_client, 'Пила сабельная деревянная', '28.24.11.000-00000024')
    assert not any(f['name'] == 'Тип обрабатываемого материала' for f in result['facts'])
    assert any('деревянная' in f['raw'] for f in result['unparsed'])


def test_explicit_invalid_part_material_is_checked_not_reassigned(card_client):
    result = post(card_client, 'Молоток с деревянным бойком')
    fact = next(f for f in result['facts'] if f['name'] == 'Материал бойка')
    assert fact['value'] == 'Дерево' and not fact['unknown']
    assert not any(f['name'] == 'Материал рукоятки' for f in result['facts'])
    assert result['candidate_count'] == 0
    assert any(c['outcome'] == 'incompatible' and c['name'] == 'Материал бойка' for p in result['excluded'] for c in p['checks'])


def test_invalid_explicit_part_not_limited_to_present_allowed_values():
    from app.extraction import extract
    from app.card_schemas import CardSchemas
    from app.catalog import AllowedValue, Property
    prop = Property(id='test-striker', name='Материал бойка', value_type='text', verified=True,
                    evidence='unit-only', allowed=(AllowedValue(kind='enum', value='Медь', source='unit-only'),))
    facts, _ = extract('деревянный боёк', (prop.name,), scoped_properties=(prop,),
                       lexicon=CardSchemas('resources/card-schemas.json').lexicon)
    assert facts[0].name == prop.name and facts[0].value == 'Дерево'
