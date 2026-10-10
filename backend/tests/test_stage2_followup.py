"""Regression checks for wire-independent equality and indivisible counts."""
import pytest
from app.catalog import AllowedValue, Property
from app.checks import check_fact, signature
from app.schemas import Fact
from test_semantics_stage2_http import BASE, client_with, rule
from algorithms.AlgorithmC import Units, _facts_conflict, _canonical_value, check_fact as c_check
from algorithms.contract import Fact as CFact, Unit as CUnit
from test_algorithm_contract import characteristic, position


def numeric(semantics='count', unit='шт', evidence='reviewed fixture', kind='2'):
    return Property(id='features',name='Количество',value_type='number',kind=kind,
        required=False,verified=True,evidence='fixture',numeric_semantics=semantics,
        numeric_semantics_evidence=evidence,
        allowed=(AllowedValue(kind='interval',lower='0',upper='5',unit=unit,source='fixture'),))


def fact(value=None, values=None, unit=None):
    return Fact(id='fact',name='Количество',value=value or '',values=values,unit=unit,raw='original',source='input')


def test_canonical_comparison_preserves_input():
    prop=rule()
    scalar=fact('Первый'); array=fact(values=['Первый','Первый'])
    assert signature(scalar,prop)==signature(array,prop)
    assert signature(fact(values=['Первый','Второй']),prop)==signature(fact(values=['Второй','Первый','Первый']),prop)
    assert array.values==['Первый','Первый'] and array.raw=='original'
    prop=numeric('physical','м')
    assert signature(fact('1',unit='м'),prop)==signature(fact(values=['100','100.0'],unit='см'),prop)
    assert signature(fact('1',unit='м'),prop)!=signature(fact(values=['100'],unit='мм'),prop)
    prop=numeric(unit=None)
    assert signature(fact('1'),prop)==signature(fact(values=['0.001'],unit='тыс. шт'),prop)


@pytest.mark.parametrize('values,conflict',[(['Первый'],False),(['Первый','Первый'],False),(['Второй'],True)])
def test_http_text_and_structured_same_or_different(monkeypatch,catalog_data,values,conflict):
    prop=rule().model_copy(update={'kind':'2'})
    with client_with(monkeypatch,catalog_data,prop) as client:
        body={**BASE,'description':BASE['description']+'; Особенности: Первый',
              'attributes':[{'id':'structured','name':'Особенности','values':values}]}
        result=client.post('/api/v1/analyses',json=body).json()
        assert any(i['code']=='conflicting_facts' for i in result['issues'])==conflict
        assert (result['status']=='resolved')== (not conflict)
        facts=[f for f in result['facts'] if f['name']=='Особенности']
        assert any(f['source']=='text' and f['span'] for f in facts)
        assert any(f['values']==values and f['source']=='input' for f in facts)


@pytest.mark.parametrize('value,unit,expected',[
    ('1','шт','compatible'),('1.5','шт','incompatible'),('0.001','тыс. шт','compatible'),
    ('0.0015','тыс. шт','incompatible'),('[0;5]','шт','compatible'),('(0;5)','шт','compatible'),
    ('(5;5)','шт','incompatible'),('[5;0]','шт','incompatible'),
    ('[0;0.005]','тыс. шт','compatible'),('1.5',None,'incompatible')])
def test_count_scalar_and_array_with_and_without_source_unit(value,unit,expected):
    prop=numeric(unit='шт' if unit else None)
    for given in (fact(value,unit=unit),fact(values=[value,value],unit=unit)):
        assert check_fact(prop,given)[0]==expected


def test_physical_fraction_and_unproven_count():
    assert check_fact(numeric('physical','м'),fact('1.5',unit='м'))[0]=='compatible'
    assert check_fact(numeric(evidence=None),fact('1.5',unit='шт'))[:2]==('unknown','numeric_semantics_unknown')


@pytest.mark.parametrize('value,expected',[('1','compatible'),('1.5','incompatible')])
def test_count_in_source_pairs_has_same_granularity_in_backend_and_c(value,expected):
    assert check_fact(numeric(unit='пар'),fact(value,unit='пар'))[0]==expected
    assert c_check(Units(),c_characteristic(unit='пар'),
                   CFact(kind='number',value=float(value),unit=CUnit(text='пар')))[0]==expected


@pytest.mark.parametrize('values,expected',[(['1','2'],'compatible'),(['1','1.5'],'incompatible')])
def test_multiple_numeric_count_selection_checks_every_element(values,expected):
    assert check_fact(numeric(kind='3'),fact(values=values,unit='шт'))[0]==expected
    char=c_characteristic(); char.kind='3'
    assert c_check(Units(),char,CFact(kind='number_set',value=[float(v) for v in values],
                                     unit=CUnit(text='шт')))[0]==expected


@pytest.mark.parametrize('values',[['1'],['1.5']])
def test_http_indivisible_count_array(monkeypatch,catalog_data,values):
    prop=numeric().model_copy(update={'name':'Количество'})
    with client_with(monkeypatch,catalog_data,prop) as client:
        result=client.post('/api/v1/analyses',json={**BASE,'confirmed_ktru_code':None,
            'attributes':[{'id':'n','name':'Количество','values':values,'unit':'шт'}]}).json()
        candidate=next(c for c in result['candidates']+result['excluded'] if c['ktru_code']==BASE['confirmed_ktru_code'])
        check=next(c for c in candidate['checks'] if c['property_id']==prop.id)
        assert check['outcome']==('compatible' if values==['1'] else 'incompatible')
        if values==['1.5']: assert check['reason']=='invalid_count'


def c_characteristic(semantics='count',unit='шт',evidence='reviewed fixture'):
    from algorithms.ktru_index import Alternative
    char=characteristic(Alternative('range',lo=0,hi=5,unit_text=unit))
    char.numeric_semantics=semantics; char.numeric_semantics_evidence=evidence
    return char


@pytest.mark.parametrize('kind,value,unit,expected',[
    ('number',1,'шт','compatible'),('number',1.5,'шт','incompatible'),
    ('number_set',[1.5],'шт','incompatible'),('number_set',[1,1.0],'шт','compatible'),
    ('number',.001,'тыс. шт','compatible'),('number',.0015,'тыс. шт','incompatible'),
    ('range',{'min':0,'max':5},'шт','compatible'),
    ('range',{'min':0,'max':5,'min_inclusive':False,'max_inclusive':False},'шт','compatible'),
    ('range',{'min':0,'max':.005},'тыс. шт','compatible'),
    ('number',1.5,None,'incompatible')])
def test_c_count_scaling_scalar_set_range(kind,value,unit,expected):
    char=c_characteristic(unit='шт' if unit else None)
    given=CFact(kind=kind,value=value,unit=CUnit(text=unit) if unit else None)
    assert c_check(Units(),char,given)[0]==expected


def test_c_fraction_semantics_and_equality():
    units=Units();char=c_characteristic('physical','m')
    assert c_check(units,char,CFact(kind='number',value=1.5,unit=CUnit(text='m')))[0]=='compatible'
    assert c_check(units,c_characteristic(evidence=None),CFact(kind='number',value=1.5,unit=CUnit(text='шт')))[0]=='unknown'
    scalar=CFact(kind='number',value=1,unit=CUnit(text='m'),name='Size',def_id='size')
    array=CFact(kind='number_set',value=[100,100],unit=CUnit(text='cm'),name='Size',def_id='size')
    assert _canonical_value(units,char,scalar)==_canonical_value(units,char,array)
    assert not _facts_conflict([scalar,array],units,[position(char=char)])
    array.value=[101]
    assert _facts_conflict([scalar,array],units,[position(char=char)])
    scalar.value=.0000000011
    array.unit=CUnit(text='m'); array.value=[.0000000012]
    assert _facts_conflict([scalar,array],units,[position(char=char)])
    text=CFact(kind='text',value='red',name='Size',def_id='size')
    selected=CFact(kind='text_set',value=['red','red'],name='Size',def_id='size')
    assert _canonical_value(units,char,text)==_canonical_value(units,char,selected)
    bare=c_characteristic(unit=None)
    assert _canonical_value(units,bare,CFact(kind='number',value=1))==_canonical_value(
        units,bare,CFact(kind='number_set',value=[.001,.001],unit=CUnit(text='тыс. шт')))
