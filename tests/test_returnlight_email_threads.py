from email.message import EmailMessage
import pytest
from returnlight_app.returnlight_intake import import_buyer_email
from returnlight_app.returnlight import new_case, add_buyer_followup, case_snapshot, CaseError

def raw_email(**headers):
    message=EmailMessage()
    message['From']='Maya <maya@example.com>'
    message['Subject']='The replacement lid'
    for key,value in headers.items():message[key.replace('_','-')]=value
    message.set_content('The replacement lid does not fit the original bowl. Please help.')
    return message.as_bytes()

def test_import_retains_bounded_opaque_reply_context():
    result=import_buyer_email(raw_email(Message_ID='<Bowl.2@example.com>',References='<older@example.com> <Bowl.1@example.com>',In_Reply_To='<Bowl.1@example.com>'))
    assert result['message_id']=='<Bowl.2@example.com>'
    assert result['reply_references']==['<older@example.com>','<Bowl.1@example.com>']
    assert result['capture_id']==''
    assert result['complaint'].startswith('The replacement lid')

def test_malformed_message_reference_does_not_reject_readable_buyer_email():
    result=import_buyer_email(raw_email(Message_ID='not-an-id',References='not an identifier'))
    assert 'message_id' not in result and 'reply_references' not in result
    assert 'original bowl' in result['complaint']

def test_long_thread_keeps_recent_references_and_direct_reply_last():
    references=' '.join(f'<message.{i}@example.com>' for i in range(80))
    result=import_buyer_email(raw_email(References=references,In_Reply_To='<message.7@example.com>'))
    assert len(result['reply_references'])==32
    assert result['reply_references'][-1]=='<message.7@example.com>'
    assert '<message.79@example.com>' in result['reply_references']

@pytest.mark.parametrize('extra',[
    {'message_id':'<one@example.com><two@example.com>'},
    {'message_id':'<'+('a'*250)+'@example.com>'},
    {'reply_references':'<one@example.com>'},
    {'reply_references':['<one@example.com>']*33},
    {'reply_references':['<bad\n@example.com>']},
    {'message_id':True},
])
def test_invalid_saved_reference_refuses_mutation(tmp_path,extra):
    path=tmp_path/'case.json'
    new_case(path,'BOWLREFER1234','19.50','The replacement bowl arrived broken. Please help.')
    case=case_snapshot(path);before=path.read_bytes()
    with pytest.raises(CaseError):
        add_buyer_followup(path,'The replacement lid does not fit the bowl.',len(case['history']),
            imported_email={'sender':'Maya <maya@example.com>','subject':'Replacement lid',**extra})
    assert path.read_bytes()==before

def test_saved_thread_context_survives_reload_and_does_not_alias_caller(tmp_path):
    path=tmp_path/'case.json'
    source={'sender':'Maya <maya@example.com>','subject':'Replacement bowl','message_id':'<one@example.com>'}
    new_case(path,'BOWLREFER1234','19.50','The original bowl arrived broken. Please help.',imported_email=source)
    case=case_snapshot(path)
    followup={'sender':source['sender'],'subject':'Replacement lid','message_id':'<two@example.com>','reply_references':['<one@example.com>']}
    add_buyer_followup(path,'The replacement lid does not fit the bowl.',len(case['history']),imported_email=followup)
    followup['reply_references'].append('<mutated@example.com>')
    saved=case_snapshot(path)
    assert saved['imported_email']['message_id']=='<one@example.com>'
    assert saved['buyer_followups'][-1]['imported_email']['reply_references']==['<one@example.com>']
    assert saved['capture_id']=='BOWLREFER1234' and saved['state']=='OPEN'

def test_multiple_message_id_headers_are_not_a_single_saved_reference():
    raw=raw_email(Message_ID='<one@example.com>')
    raw=b'Message-ID: <two@example.com>\n'+raw
    assert 'message_id' not in import_buyer_email(raw)
