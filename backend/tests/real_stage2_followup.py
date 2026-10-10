"""Live regression evidence on the unchanged real clean5 snapshot and E5."""
import argparse
import json
from pathlib import Path
from real_semantics_stage2 import http
from app.catalog import Property
from app.questions import property_question_id


def run(base, output, phase, source_dir=None):
    source_dir = source_dir or output.parent
    rows = json.loads((source_dir / 'source-selection.json').read_text(encoding='utf-8'))
    shovel = next(r for r in rows if r['name'] == 'Лопата')
    kit = next(r for r in rows if r is not shovel)
    records = []

    def post(row, label, description=None, **fields):
        body = dict(description=description or row['name'], confirmed_category_id=row['parent_position']['code'],
                    candidate_limit=100, **fields)
        status, result = http(base, '/api/v1/analyses', body)
        records.append(dict(scenario=label, request=body, http_status=status, response=result))
        assert status == 200, (label, result)
        return result

    def check(result, row, name):
        candidate = next(c for c in result['candidates'] + result['excluded'] if c['ktru_code'] == row['code'])
        return next(c for c in candidate['checks'] if c['name'] == name)

    mount = next(p for p in shovel['characteristics'] if p['name'] == 'Крепление черенка')
    upper, lower = [v['quality_description'] for v in mount['values']]
    text = f"{shovel['name']}; {mount['name']}: {upper}"
    for values, expected in [([upper], 'compatible'), ([upper, upper], 'compatible'), ([lower], 'unknown')]:
        result = post(shovel, 'text + structured ' + repr(values), text,
                      attributes=[dict(id='mount', name=mount['name'], values=values)])
        c = check(result, shovel, mount['name'])
        if phase == 'after':
            assert c['outcome'] == expected, c
            assert (c['reason'] == 'conflicting_facts') == (values == [lower])
            facts = [f for f in result['facts'] if f['name'] == mount['name']]
            assert any(f['source'] == 'text' and f['span'] for f in facts)
            assert any(f['values'] == values and f['raw'] for f in facts)

    card = json.loads((source_dir / (shovel['code']+'.json')).read_text(encoding='utf-8'))
    property_data = next(p for p in card['position']['properties'] if p['name'] == mount['name'])
    qid = property_question_id(shovel['parent_position']['code'],Property.model_validate(property_data))
    old_answer = [dict(question_id=qid,value=upper)]
    post(shovel,'saved scalar answer',answers=old_answer)
    edited = post(shovel,'explicit edit replaces saved scalar answer',answers=old_answer,
                  attributes=[dict(id='edited',name=mount['name'],values=[lower],replaces='answer:'+qid)])
    if phase == 'after':
        assert check(edited,shovel,mount['name'])['outcome']=='compatible'
        assert [f['values'] for f in edited['facts'] if f['name']==mount['name']]==[[lower]]
        assert not any(i['code']=='conflicting_facts' for i in edited['issues'])

    multiple = next(p for p in shovel['characteristics'] if p['kind']=='3' and len(p['values'])>1)
    selected = [v['quality_description'] for v in multiple['values'][:2]]
    for values in (selected,list(reversed(selected))+selected[:1]):
        result=post(shovel,'kind3 order and repetition '+repr(values),
                    attributes=[dict(id='multi',name=multiple['name'],values=values)])
        if phase=='after': assert check(result,shovel,multiple['name'])['outcome']=='compatible'

    handle = next(p for p in shovel['characteristics'] if p['name']=='Длина черенка')
    for values,expected in [(['100','100.0'],'compatible'),(['110'],'unknown')]:
        result=post(shovel,'numeric equivalent units '+repr(values),
                    f"Лопата; {handle['name']}: 1000 мм",
                    attributes=[dict(id='handle',name=handle['name'],values=values,unit='см')])
        if phase=='after': assert check(result,shovel,handle['name'])['outcome']==expected

    for name, cases in [
        ('Количество контейнеров в наборе', [('1','шт','compatible'), ('1.5','шт','incompatible'),
          (['1.5'],'шт','incompatible'), (['1','1'],'шт','compatible'), ('0.001','тыс. шт','compatible'),
          ('0.0015','тыс. шт','incompatible'), ('[0;5]','шт','compatible'), ('(0;5)','шт','compatible'),
          ('(5;5)','шт','incompatible'), ('[5;0]','шт','incompatible')]),
        ('Объем растворов дополнительных контейнеров', [('150.5','мл','compatible'), (['150.5'],'мл','compatible')])]:
        for value, unit, expected in cases:
            attribute = dict(id='quantity', name=name, unit=unit)
            attribute['values' if isinstance(value, list) else 'value'] = value
            result = post(kit, name + ' ' + repr(value) + ' ' + unit, attributes=[attribute])
            c = check(result, kit, name)
            if phase == 'after': assert c['outcome'] == expected, c
            if '1.5' == value or value == ['1.5']:
                if phase == 'after': assert c['reason'] == 'invalid_count', c
            print(records[-1]['scenario'], c['outcome'], c['reason'], flush=True)
    if phase=='after':
        body=dict(description=kit['name'],confirmed_category_id=kit['parent_position']['code'],
                  confirmed_ktru_code=kit['code'],attributes=[dict(id='bad-count',
                  name='Количество контейнеров в наборе',value='1.5',unit='шт')])
        status,result=http(base,'/api/v1/analyses',body)
        records.append(dict(scenario='manual code cannot bypass fractional count',request=body,
                            http_status=status,response=result))
        assert status==422 and result['detail']['code']=='incompatible_ktru_code',result
    output.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8000')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--phase', choices=['before','after'],required=True)
    parser.add_argument('--source-dir', type=Path)
    args=parser.parse_args()
    run(args.base,args.output,args.phase,args.source_dir)
