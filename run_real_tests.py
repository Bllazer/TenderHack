"""Independent source-labeled evaluation of real portal cards; no model-generated gold."""
import argparse,csv,json,urllib.request,urllib.error
from decimal import Decimal,InvalidOperation
from pathlib import Path

def call(base,body):
    req=urllib.request.Request(base.rstrip('/')+'/api/v1/analyses',data=json.dumps(body,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=120) as r:return json.load(r)
    except urllib.error.HTTPError as e:raise AssertionError(f'HTTP {e.code}: {e.read().decode()}') from e

def ready(base):
    with urllib.request.urlopen(base.rstrip('/')+'/api/health/ready',timeout=30) as r:return json.load(r)

def check_source(result,checks):
    evaluated={c['ktru_code']:c for c in result['candidates']+result['excluded']}
    errors=[]
    for check in checks:
        candidate=evaluated.get(check['code'])
        prop=next((p for p in candidate['checks'] if p['property_id']==check['property_id']),None) if candidate else None
        if not prop:errors.append(f"{check['code']}/{check['property_id']}: not evaluated")
        elif prop['outcome']!=check['outcome']:errors.append(f"{check['code']}/{check['property_id']}: expected {check['outcome']}, actual {prop['outcome']}")
    assert not errors,'; '.join(errors)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base-url',required=True)
    parser.add_argument('--csv',type=Path,default=Path(__file__).with_name('TenderHack_real_cases.csv'))
    parser.add_argument('--report',type=Path,default=Path(__file__).with_name('real_test_report.json'))
    args=parser.parse_args()
    with args.csv.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
    records=[]; health=ready(args.base_url); diagnostics={}
    def run(row,stage,action):
        diagnostics.clear()
        try:
            detail=action();status='PASS'
        except (AssertionError,urllib.error.URLError,TimeoutError,KeyError) as e:status='FAIL';detail=str(e)
        record={'case_id':row['case_id'],'source_ref':row['source_ref'],'stage':stage,'status':status,'detail':detail}
        if status=='FAIL' and diagnostics:record['actual_response']=dict(diagnostics)
        records.append(record)
    for i,row in enumerate(rows,1):
        raw=json.loads(row['request_json']);confirmed=json.loads(row['confirmed_request_json']);checks=json.loads(row['expected_checks_json'])
        gold_attrs=confirmed['attributes']; parent=row['expected_category_id']
        def retrieval():
            r=call(args.base_url,raw); ids=[x['id'] for x in r['categories']]
            assert parent in ids,f'Expected {parent} in top-5, got {ids}'
            return {'rank':ids.index(parent)+1,'status':r['status']}
        run(row,'retrieval',retrieval)
        if checks:
            def constraints():
                r=call(args.base_url,{**confirmed,'candidate_limit':100});check_source(r,checks)
                expected_codes=set(json.loads(row['expected_codes_json']))
                assert {c['ktru_code'] for c in r['candidates']}==expected_codes,'Remaining codes differ from independent XML intersection'
                return {'independent_assertions':len(checks),'status':r['status']}
            run(row,'source_constraints',constraints)
            def natural():
                r=call(args.base_url,{**raw,'confirmed_category_id':parent,'candidate_limit':100})
                diagnostics.update(r)
                check_source(r,checks)
                return {'independent_assertions':len(checks),'status':r['status']}
            run(row,'natural_language',natural)
            codes=json.loads(row['expected_codes_json'])
            if len(codes)==1:
                def unique_code():
                    r=call(args.base_url,{**confirmed,'candidate_limit':100,'confirmed_ktru_code':codes[0]})
                    diagnostics.update(r)
                    assert r['selected_ktru_code']==codes[0], f"Expected unique {codes[0]}; actual {r['selected_ktru_code']}, status={r['status']}, verification={r['verification']}"
                    return {'selected_code':codes[0],'fields_complete':r['verification']['fields_complete']}
                run(row,'unique_code_confirmation',unique_code)
            if row.get('incomplete_request_json'):
                def incomplete():
                    body=json.loads(row['incomplete_request_json'])
                    r=call(args.base_url,body);diagnostics.update(r)
                    assert r['selected_ktru_code']==body['confirmed_ktru_code'],'Jury requirement: unique code blocked by missing mandatory fields'
                    assert not r['verification']['fields_complete'],'Missing source-required fields incorrectly considered complete'
                    assert any(c['missing_required'] for c in r['candidates']),'No warning about missing required properties'
                    return {'selected_code':r['selected_ktru_code']}
                run(row,'jury_unique_incomplete',incomplete)
            if len(gold_attrs)>1:
                def funnel():
                    previous=None; details=[]
                    for n in range(1,len(gold_attrs)+1):
                        body={**confirmed,'attributes':gold_attrs[:n],'candidate_limit':100}
                        r=call(args.base_url,body)
                        assert r['scope']['complete'],'Incomplete category scope'
                        assert r['scope']['category_id']==parent,'Category changed during clarification'
                        remaining={c['ktru_code'] for c in r['candidates']}
                        if previous is not None:assert remaining<=previous,'Adding non-replacing facts restored excluded codes'
                        # Exact XML oracle for every property supplied at this step.
                        names={a['name'] for a in gold_attrs[:n]}
                        step_checks=[c for c in checks if c['property_name'] in names]
                        check_source(r,step_checks)
                        details.append({'facts':n,'remaining':len(remaining)});previous=remaining
                    return details
                run(row,'clarification_funnel',funnel)
        else:
            records.append({'case_id':row['case_id'],'source_ref':row['source_ref'],'stage':'source_constraints','status':'NOT_LABELED','detail':'No explicit source-property mapping; retrieval only. No invented final-code gold.'})
        print(f"{i}/{len(rows)} {row['case_id']}",flush=True)
    stages={}
    for r in records:
        counter=stages.setdefault(r['stage'],{});counter[r['status']]=counter.get(r['status'],0)+1
    output={'ready':health,'stages':stages,'records':records,'limits':'Purposive sample, not population accuracy. Labels cover family and explicit source checks, not unconditional final code.'}
    args.report.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(stages,ensure_ascii=False))
    return 1 if any(r['status']=='FAIL' for r in records) else 0

if __name__=='__main__':raise SystemExit(main())
