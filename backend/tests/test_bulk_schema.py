
def test_bulk_schema_includes_all_positions_not_only_next_question(client):
    response = client.get('/api/v1/catalog/categories/tools/properties')
    assert response.status_code == 200
    body = response.json()
    fields = {f['name']: f for f in body['properties']}
    assert set(fields) == {'Питание', 'Мощность', 'Напряжение'}
    assert fields['Питание']['options'] == ['от аккумулятора', 'от сети']
    assert fields['Мощность']['units'] == ['Вт']
    assert fields['Мощность']['question_id'].startswith('property:')
    request = {'description': 'Дрель', 'confirmed_category_id': 'tools', 'answers': [
        {'question_id': fields['Мощность']['question_id'], 'value': '650', 'unit': 'Вт'},
        {'question_id': fields['Питание']['question_id'], 'value': 'от сети'}]}
    result = client.post('/api/v1/analyses', json=request).json()
    assert result['candidate_count'] == 1
    assert result['candidates'][0]['ktru_code'] == 'TEST-DRILL-MAINS'
    assert result['next_question']['kind'] == 'code'

def test_bulk_schema_rejects_unknown_category(client):
    assert client.get('/api/v1/catalog/categories/not-found/properties').status_code == 404
