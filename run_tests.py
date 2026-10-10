"""CSV acceptance runner. Never infer expected values from actual engine output.

Run with the project's backend virtualenv. Offline executes source-backed C tests;
--base-url additionally executes independent source-backed HTTP/search assertions.
Human/jury requirements remain REVIEW, never counted as passing automated tests.
"""
import argparse, csv, json, sys, urllib.request, urllib.error
from decimal import Decimal
from pathlib import Path

UNITS={'003':'мм','004':'см','006':'м','050':'мм²','051':'см²','055':'м²','163':'г','166':'кг','212':'Вт','214':'кВт','796':'шт','715':'пар'}

def source_property(rule):
    from app.catalog import AllowedValue, Property
    allowed=[]
    for v in rule['values']:
        common={'source':'jury-test-source-XML','unit':UNITS.get(v['okei'],v['unit'] or None),'okei_code':v['okei'] or None}
        if v['text']:allowed.append(AllowedValue(kind='enum',value=v['text'],**common))
        for number in v['concrete']:allowed.append(AllowedValue(kind='number',value=number,**common))
        for r in v['ranges']:
            allowed.append(AllowedValue(kind='interval',lower=r['min'] or None,upper=r['max'] or None,
                lower_inclusive=r['left']=='greaterOrEqual',upper_inclusive=r['right']=='lessOrEqual',**common))
    count=bool(allowed) and all(a.okei_code in ('796','715') for a in allowed)
    return Property(id=rule['id'],name=rule['name'],value_type='text' if rule['type']=='1' else 'number',
        kind=rule['kind'],source_type=rule['type'],required=rule['required']=='true',verified=True,
        evidence='Provided XML source',allowed=tuple(allowed),numeric_semantics='count' if count else 'physical',
        numeric_semantics_evidence='Explicit source OKEI' if rule['type']=='2' else None)

def offline(row):
    from app.checks import check_fact
    from app.schemas import Fact
    from app.structured import structure
    from algorithms.AlgorithmC import Units, check_fact as c_check
    from algorithms.contract import Fact as AlgorithmFact
    from algorithms.ktru_index import Alternative, Characteristic
    rule=json.loads(row['source_rule_json']); prop=source_property(rule)
    input_fact=json.loads(row['request_json'])['attributes'][0]
    fact=Fact(id=input_fact['id'],name=input_fact['name'],value=input_fact['value'],unit=input_fact.get('unit'),raw='source-controlled-input',source='input')
    expected=json.loads(row['expected_json'])['candidate_check']['outcome']
    actual=check_fact(prop,fact)[0]
    assert actual==expected,f'backend checker: expected {expected}, got {actual}'
    alternatives=[]
    for a in prop.allowed:
        alternatives.append(Alternative(kind={'enum':'text','number':'number','interval':'range'}[a.kind],
          text=a.value if a.kind=='enum' else None,number=float(a.value) if a.kind=='number' else None,
          lo=float(a.lower) if a.lower is not None else None, hi=float(a.upper) if a.upper is not None else None,
          lo_strict=not a.lower_inclusive,hi_strict=not a.upper_inclusive,unit_code=a.okei_code,unit_text=a.unit))
    char=Characteristic(prop.id,prop.name,rule['type'],rule['kind'],prop.required,None,alternatives,
      prop.numeric_semantics,prop.numeric_semantics_evidence)
    result=c_check(Units(),char,AlgorithmFact.from_dict(structure(fact).model_dump()))[0]
    allowed=json.loads(row['expected_json']).get('algorithm_c_allowed_outcomes',[expected])
    assert result in allowed,f'AlgorithmC: expected one of {allowed}, got {result}'

def http(base,path,body=None):
    request=urllib.request.Request(base.rstrip('/')+path,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,
        headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=120) as response:return response.status,json.load(response)
    except urllib.error.HTTPError as failure:return failure.code,json.load(failure)

def live(base,row):
    code,result=http(base,'/api/v1/analyses',json.loads(row['request_json']))
    assert code==200,f'HTTP {code}: {result}'
    if result['status']=='needs_data' and not result.get('candidates') and not result.get('excluded'):
        return 'BLOCKED','Catalog/index/model unavailable: '+','.join(i['code'] for i in result.get('issues',[]))
    expected=json.loads(row['expected_json'])
    if expected.get('selected_must_be_null'):assert result['selected_ktru_code'] is None,'Unexpected final code'
    if 'category_contains' in expected:
        assert any(c['id']==expected['category_contains'] for c in result['categories']),'Expected category missing from top-k'
    if 'candidate_check' in expected:
        check=expected['candidate_check']
        candidate=next((c for c in result['candidates']+result['excluded'] if c['ktru_code']==check['code']),None)
        assert candidate is not None,'Expected source position absent from evaluated results'
        actual=next((p for p in candidate['checks'] if p['property_id']==check['property_id']),None)
        assert actual is not None,'Source property not checked'
        assert actual['outcome']==check['outcome'],f"Expected {check['outcome']}, got {actual['outcome']}"
    return 'PASS','Source-backed HTTP assertion passed'


def integration(base,row):
    expected=json.loads(row['expected_json'])
    body=json.loads(row['request_json'])
    code,result=http(base,'/api/v1/analyses',body)
    assert code==expected.get('http_status',200),f'HTTP {code}: {result}'
    if code!=200:return 'PASS','Invalid request rejected by API'
    from app.schemas import AnalysisOut
    AnalysisOut.model_validate(result)
    assert result['original_text']==body['description'],'Original description lost'
    assert result['catalog_version']!='unavailable','Catalog unavailable'
    assert not any(i['code'] in ('resources_invalid','model_unavailable','analysis_unavailable') for i in result['issues']), 'Resources unavailable'
    selected=result['selected_ktru_code']
    if selected:
        assert result['status']=='resolved','Final code without resolved status'
        assert result['verification']['ktru_unique'],'Final code without unique KTRU'
        assert result['scope']['complete'],'Final code from partial search scope'
        assert not result['verification']['blocking_issues'],'Final code despite blocking issues'
        assert selected not in [c['ktru_code'] for c in result['excluded']],'Excluded code selected'
    if expected.get('no_selected'):
        assert selected is None,'Unsupported final code assigned'
    if expected.get('additional'):
        assert expected['additional'] in [f['raw'] for f in result['additional']],'Additional information lost'
    if expected.get('fact_id'):
        assert any(f['id']==expected['fact_id'] and f['value']==expected['fact_value'] for f in result['facts'] if f['source']=='input'),'Original input fact lost'
    if expected.get('replay'):
        http(base,'/api/v1/analyses',{'description':'Рубашка мужская'})
        again_code,again=http(base,'/api/v1/analyses',body)
        assert again_code==200,'Replay failed'
        for key in ('status','selected_ktru_code','facts','categories','candidate_count','excluded_count','scope','verification','next_question'):
            assert result[key]==again[key],f'Stateless isolation/replay violated: {key}'
    return 'PASS','Live API: schema, independent safety and preservation assertions'

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--csv',type=Path,default=Path(__file__).with_name('TenderHack_tests.csv'))
    parser.add_argument('--base-url')
    parser.add_argument('--report',type=Path,default=Path(__file__).with_name('test_report.json'))
    args=parser.parse_args();sys.path.insert(0,str(args.repo/'backend'))
    with args.csv.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
    records=[]
    for row in rows:
        record={'case_id':row['case_id'],'group':row['group'],'status':'REVIEW','detail':'Requires controlled fixture or acceptance review'}
        offline_error=None
        try:
            if row['source_rule_json']:
                try:
                    offline(row);record.update(status='PASS',detail='Backend checker + AlgorithmC, source-backed independent oracle',offline_status='PASS')
                except AssertionError as error:
                    offline_error=str(error);record.update(status='FAIL',detail=offline_error,offline_status='FAIL')
            if args.base_url and row['execution_mode']=='integration_api':
                status,detail=integration(args.base_url,row);record.update(status=status,detail=detail)
            if args.base_url and row['execution_mode']=='live_api':
                status,detail=live(args.base_url,row);record.update(status=status,detail=detail,live_status=status)
                if offline_error:record.update(status='FAIL',detail=offline_error+'; HTTP: '+status+' '+detail)
        except Exception as error:record.update(status='FAIL',detail=str(error))
        records.append(record)
    counts={status:sum(r['status']==status for r in records) for status in ('PASS','FAIL','BLOCKED','REVIEW')}
    args.report.write_text(json.dumps({'counts':counts,'records':records},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(counts,ensure_ascii=False))
    return 1 if counts['FAIL'] else 2 if counts['BLOCKED'] else 0

if __name__=='__main__':sys.exit(main())
