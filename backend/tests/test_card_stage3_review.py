"""General extraction/clarification regressions; fixture search is not real E5 acceptance."""
import pytest
from test_card_stage3 import card_client, post

SHIRT = '14.14.30.110-00000003'
COAT = '14.13.10.000-00000033'


@pytest.mark.parametrize('text', [
    'Футболка мужская EU 40 размер', 'Футболка мужская 40 размер EU',
    'Футболка мужская европейского размера 40', 'Футболка мужская US 40 размера',
    'Футболка мужская UK размер 40', 'Футболка мужская размер XL',
    'Футболка мужская; Российский размер: EU 40',
    'Футболка мужская; Российский размер: 40 EU',
    'Футболка мужская; Российский размер: европейский 40',
    'Футболка мужская EU 40', 'Футболка мужская 40 EU',
    'Футболка мужская EU40 размер', 'Футболка мужская 40EU размер',
    'Футболка мужская FR40 размер', 'Футболка мужская итальянского размера 40',
    'Футболка мужская international 40 размер',
])
def test_foreign_size_cannot_prove_russian_size(card_client, text):
    result = post(card_client, text, SHIRT, confirmed_ktru_code='14.14.30.110-00000002')
    assert result['selected_ktru_code'] is None and result['status'] == 'needs_input'
    assert not any(f['name'] == 'Российский размер' and not f['unknown'] for f in result['facts'])
    assert result['next_question'] and result['next_question']['kind'] == 'correction'
    fact = next(f for f in result['facts'] if f['assignment_options'])
    assert fact['raw'] == text[slice(*fact['span'])] and fact['extraction_evidence']
    assert fact['unknown'] and fact['uncertainty_reason']


def test_coat_foreign_size_and_explicit_edit(card_client):
    text = 'Пальто мужское 48 размер EU'
    result = post(card_client, text, COAT)
    fact = next(f for f in result['facts'] if f['assignment_options'])
    assert result['next_question']
    edited = post(card_client, text, COAT, attributes=[dict(id='edit', name='Российский размер', value='48', replaces=fact['id'])])
    assert any(f['name'] == 'Российский размер' and f['value'] == '48' and not f['unknown'] for f in edited['facts'])
    assert not any(i['code'] == 'unresolved_fact_uncertainty' for i in edited['issues'])
    assert any(c['original'] == fact['raw'] for c in edited['corrections'])


@pytest.mark.parametrize('label', ['40 размер', '40 размера', 'размера 40', '40-го размера'])
@pytest.mark.parametrize('separator', [' ', ', ', '; ', '\n'])
def test_designation_and_adjacent_measure_keep_distinct_spans(card_client, label, separator):
    text = 'Футболка мужская ' + label + separator + '3 мм'
    result = post(card_client, text, SHIRT)
    size = next(f for f in result['facts'] if f['name'] == 'Российский размер')
    measure = next(f for f in result['facts'] if f['unit'] == 'мм')
    assert size['value'] == '40' and size['unit'] is None and not size['unknown']
    assert measure['value'] == '3' and measure['name'] == ''
    assert size['span'][1] <= measure['span'][0]
    assert all(f['raw'] == text[slice(*f['span'])] for f in result['facts'] if f['source'] == 'text')
    assert result['selected_ktru_code'] is None
    assert result['next_question'] and result['next_question']['kind'] == 'correction'


@pytest.mark.parametrize('separator', ['; ', ' | ', '\n'])
@pytest.mark.parametrize('words', [('искусственный', 'утеплитель'), ('искуственный', 'утеплитель'), ('искусственного', 'утеплителя')])
def test_enum_windows_never_cross_segments(card_client, separator, words):
    text = 'Пальто мужское' + separator + separator.join(words)
    result = post(card_client, text, COAT)
    assert not any(f['name'] == 'Тип утеплителя' for f in result['facts'])
    assert all(word in ' '.join(f['raw'] for f in result['unparsed']) for word in words)
    assert not any(separator.strip() and separator.strip() in f['raw'] for f in result['facts'])


def test_negative_fact_question_unknown_and_explicit_correction(card_client):
    text = 'Футболка мужская размер 40; не мужская'
    first = post(card_client, text, SHIRT, confirmed_ktru_code='14.14.30.110-00000002')
    negative = next(f for f in first['facts'] if f['negated'])
    assert negative['unknown'] and 'не мужская' in negative['raw']
    assert first['next_question'] and first['selected_ktru_code'] is None
    q = first['next_question']
    unknown = post(card_client, text, SHIRT, answers=[dict(question_id=q['id'], unknown=True)])
    assert unknown['selected_ktru_code'] is None
    assert not unknown['next_question'] or unknown['next_question']['id'] != q['id']
    assert any(i['code'] == 'unresolved_fact_uncertainty' for i in unknown['issues'])
    rejected = post(card_client, text, SHIRT, answers=[dict(question_id=q['id'], value='reject')])
    assert any(f['raw'] == negative['raw'] and f['name'] == '' for f in rejected['facts'])
    assert not any(i['code'] == 'unresolved_fact_uncertainty' for i in rejected['issues'])
    changed = post(card_client, text, SHIRT, attributes=[dict(id='edit', name='Назначение', value='Женская', replaces=negative['id'])])
    assert any(i['code'] == 'conflicting_facts' for i in changed['issues'])
    assert changed['selected_ktru_code'] is None


def test_uncertain_relevant_enum_has_a_way_to_correct_it(card_client):
    result = post(card_client, 'Футболка возможно мужская размер 40', SHIRT)
    assert result['next_question'] and result['selected_ktru_code'] is None
    assert any(i['code'] == 'unresolved_fact_uncertainty' for i in result['issues'])


def test_uncertain_additional_card_field_does_not_block_ktru(card_client):
    result = post(card_client, 'Футболка мужская размер 40; возможно синий', SHIRT,
                  confirmed_ktru_code='14.14.30.110-00000002')
    assert any(f['name'] == 'Цвет' and f['unknown'] for f in result['facts'])
    assert result['status'] == 'resolved'


def test_foreign_structured_edit_not_trusted(card_client):
    result = post(card_client, 'Футболка мужская', SHIRT, attributes=[dict(id='size', name='Российский размер', value='40 EU')])
    assert result['next_question'] and result['selected_ktru_code'] is None
    assert next(f for f in result['facts'] if f['id'] == 'size')['unknown']


def test_system_in_structured_unit_is_not_a_physical_unit(card_client):
    result = post(card_client, 'Футболка мужская', SHIRT, attributes=[dict(id='size', name='Российский размер', value='40', unit='EU')],
                  confirmed_ktru_code='14.14.30.110-00000002')
    assert result['status'] == 'needs_input' and result['next_question']
    assert next(f for f in result['facts'] if f['id'] == 'size')['unknown']


def test_unassigned_measurement_has_an_explicit_exit(card_client):
    text = 'Футболка мужская 40 размер 3 мм'
    result = post(card_client, text, SHIRT)
    question = result['next_question']
    assert '3 мм' in question['title']
    unknown = post(card_client, text, SHIRT, answers=[dict(question_id=question['id'], unknown=True)])
    assert unknown['selected_ktru_code'] is None
    assert not unknown['next_question'] or unknown['next_question']['id'] != question['id']
    rejected = post(card_client, text, SHIRT, answers=[dict(question_id=question['id'], value='reject')])
    assert any(f['unit'] == 'мм' and f['raw'] == '3 мм' for f in rejected['facts'])
    assert not any(i['code'] == 'unmapped_measurement' for i in rejected['issues'])


def test_physical_unit_is_not_dropped_from_explicit_size(card_client):
    result = post(card_client, 'Футболка мужская размер 40 см', SHIRT)
    assert any(f['name'] == 'Российский размер' and f['unit'] == 'см' for f in result['facts'])
    assert result['selected_ktru_code'] is None and not result['verification']['value_verified']


@pytest.mark.parametrize('description,value', [('размер 40-42', '40-42'), ('размер [40;42]', '[40;42]'),
                                               ('[40;42] размера', '[40;42]')])
def test_designation_interval_is_not_truncated(card_client, description, value):
    result = post(card_client, 'Футболка мужская ' + description, SHIRT)
    assert any(f['name'] == 'Российский размер' and f['value'] == value for f in result['facts'])
