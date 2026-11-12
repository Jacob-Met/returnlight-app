"""Authored source-composition course. Controlled HTTP replies are not model-quality evidence."""
import copy
import json
import threading
from pathlib import Path

import pytest
from test_returnlight_remote_review import provider, reviewer
from returnlight_app.returnlight import CaseError, CaseChanged, case_snapshot, new_case, review_case, retain_purchase
from returnlight_app.returnlight_remote_review import validate_conversation_review
from returnlight_app.returnlight_purchase_sources import purchase_sources
from returnlight_app.returnlight_record import render_case_record
from returnlight_app.returnlight_web import LocalCases

BUYER = 'The blue item arrived damaged. Please refund that item and leave the other item alone.'


def observed(blue='bowl'):
    items = [('Blue ceramic bowl' if blue == 'bowl' else 'Natural ceramic bowl', 1900),
             ('Blue linen tea towel' if blue == 'towel' else 'Natural linen tea towel', 540)]
    return {'observed_at': '2026-11-11T19:00:00+00:00', 'capture': {
        'capture_id':'AUTHOREDPURCHASE01', 'original_cents':2440, 'order_id':'AUTHOREDORDER001',
        'order':{'id':'AUTHOREDORDER001','total_cents':2440,'items':[
            {'name':name,'quantity':1,'unit_cents':cents,'line_cents':cents,'sku':'PRIVATE-SKU'}
            for name,cents in items]}}}


def make(tmp_path, blue='bowl', key='a'*32):
    path = tmp_path / (key + '.json')
    case = new_case(path,'AUTHOREDPURCHASE01','24.40',BUYER,purchase_observation=observed(blue))
    return path,case


def answer(case, number):
    return {'current_request':'Buyer requests a refund for the blue item only; merchant approval is still needed.',
            'source_quotes':[{'speaker':'buyer','message_number':1,'quote':BUYER},
                             {'speaker':'purchase','message_number':number,'quote':purchase_sources(case)[number-1]}],
            'open_questions':[], 'reply_draft':{'text':'You requested a refund for the blue item and want to keep the other item.', 'source_numbers':[1]}}


def test_same_buyer_different_saved_items_reaches_explicit_reading(tmp_path,provider):
    model=reviewer(provider); app=LocalCases(tmp_path,remote_reviewer=model)
    for blue,key,number in [('bowl','a'*32,2),('towel','b'*32,3)]:
        path,case=make(tmp_path,blue,key)
        payload={'remote_confirmed':True,'destination_id':model.destination_id,'case_revision':len(case['history'])}
        before=path.read_bytes();count=len(provider['calls'])
        with pytest.raises(CaseError,match='saved purchase'):
            app.action(key,'review',payload)
        assert len(provider['calls']) == count and path.read_bytes() == before
        provider['answer']=answer(case,number)
        app.action(key,'review',{**payload,'review_scope':'conversation-purchase-v2'})
        saved=case_snapshot(path)
        assert saved['approved_cents']==0 and saved['request_id'] is None
        assert saved['purchase_context']==case['purchase_context']
        assert saved['review']['sources'][1]['speaker']=='purchase'
        content=provider['calls'][-1]['messages'][0]['content']
        assert all(words in content for words in purchase_sources(case))
        assert all(secret not in content for secret in ['AUTHOREDPURCHASE01','AUTHOREDORDER001','PRIVATE-SKU'])
        record=render_case_record(saved)
        assert 'Read original words' in record and 'Saved purchase '+str(number) in record
        assert 'id="ai-source-1"' in record and '<mark class="kept-source"' in record
    assert provider['calls'][0] != provider['calls'][1]


@pytest.mark.parametrize('change', ['wrong-speaker','wrong-number','invented-price','purchase-question'])
def test_purchase_citation_must_match_saved_bank(change,tmp_path):
    _,case=make(tmp_path);value=answer(case,2)
    if change=='wrong-speaker': value['source_quotes'][1]['speaker']='buyer'
    if change=='wrong-number': value['source_quotes'][1]['message_number']=3
    if change=='invented-price': value['source_quotes'][1]['quote']='USD 99.00'
    if change=='purchase-question': value['open_questions']=[2]
    with pytest.raises(CaseError):
        validate_conversation_review(value,[BUYER],purchase=purchase_sources(case))


def test_purchase_changed_while_reading_rejects_stale_result(tmp_path,provider):
    path,case=make(tmp_path);model=reviewer(provider)
    provider['answer']=answer(case,2);provider['mode']='held';errors=[]
    def read():
        try: review_case(path,model,expected_case_revision=len(case['history']))
        except Exception as error: errors.append(error)
    thread=threading.Thread(target=read);thread.start()
    assert provider['started'].wait(2)
    retain_purchase(path,observed('towel'),len(case['history']))
    provider['release'].set();thread.join(3)
    assert len(errors)==1 and isinstance(errors[0],CaseChanged)
    assert not case_snapshot(path).get('review')


def test_changed_purchase_cannot_receive_old_export_citation(tmp_path):
    _,case=make(tmp_path);case['review']=validate_conversation_review(answer(case,2),[BUYER],purchase=purchase_sources(case))
    case['purchase_context']['order']['items'][0]['name']='Natural ceramic bowl'
    output=render_case_record(case)
    assert 'Original passage could not be located' in output
    assert 'id="ai-source-1"' not in output


def test_new_saved_purchase_retires_old_reading_without_changing_payment(tmp_path,provider):
    path,case=make(tmp_path);provider['answer']=answer(case,2)
    reviewed=review_case(path,reviewer(provider)); old=reviewed['review']
    updated=retain_purchase(path,observed('towel'),len(reviewed['history']))
    assert updated['review'] is None and updated['ai_suggestion']==old
    assert updated['approved_cents']==0 and updated['request_id'] is None
    assert 'Previous model review' in render_case_record(updated)


@pytest.mark.parametrize('mode', ['timeout','refused'])
def test_purchase_fallback_keeps_complete_case_on_reviewer_failure(tmp_path,provider,mode):
    path,case=make(tmp_path);before=path.read_bytes();provider['mode']=mode
    with pytest.raises(CaseError):
        review_case(path,reviewer(provider,timeout=.05))
    assert path.read_bytes()==before
    assert case_snapshot(path)['complaint']==BUYER
    assert case_snapshot(path)['purchase_context']==case['purchase_context']
