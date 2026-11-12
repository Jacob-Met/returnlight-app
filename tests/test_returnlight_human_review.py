import json
import pytest
from returnlight_app.returnlight import (CaseError, CaseChanged, new_case, review_case, human_review_case,
    dismiss_review, approve_case, approval_phrase, add_buyer_followup, case_snapshot)
from returnlight_app.returnlight_record import render_case_record

WORDS = "Please replace only the cracked bowl. Only if it arrives Friday. Do not refund anything yet."
class BackgroundModel:
    def review(self, text):
        return {"kind":"source_selection","quote":"Only if it arrives Friday."}

def opened(tmp_path):
    path = tmp_path / "case.json"
    new_case(path,"HUMANREVIEWCAPTURE","24.40",WORDS)
    return path

def test_exact_ai_suggestion_does_not_advance_human_review_or_approval(tmp_path):
    path=opened(tmp_path)
    case=review_case(path,BackgroundModel())
    assert case["state"]=="OPEN" and case["approved_cents"]==0
    with pytest.raises(CaseError,match="Confirm the current"):
        approve_case(path,"9.60",approval_phrase(case,"9.60"))
    record=render_case_record(case)
    assert "Review needed" in record and "Ready for a decision" not in record
    assert "AI suggestion · needs merchant review" in record

@pytest.mark.parametrize("quote,confirmed", [(WORDS,False),("Only if it arrives Friday.",True),(WORDS,1)])
def test_confirmation_requires_complete_current_words_and_explicit_boolean(tmp_path,quote,confirmed):
    path=opened(tmp_path);case=case_snapshot(path);before=path.read_bytes()
    with pytest.raises(CaseError):
        human_review_case(path,quote,confirmed,expected_case_revision=len(case["history"]))
    assert path.read_bytes()==before

def test_operator_can_remove_bad_highlight_and_confirm_complete_conditions(tmp_path):
    path=opened(tmp_path);suggested=review_case(path,BackgroundModel())
    cleared=dismiss_review(path,len(suggested["history"]))
    assert cleared["state"]=="OPEN" and cleared["review"] is None and cleared["complaint"]==WORDS
    reviewed=human_review_case(path,WORDS,True, expected_case_revision=len(cleared["history"]))
    assert reviewed["state"]=="REVIEWED" and reviewed["review"]["kind"]=="human_selection"
    assert reviewed["ai_suggestion"]==suggested["review"]
    assert "Merchant review" in render_case_record(reviewed)
    approved=approve_case(path,"9.60",approval_phrase(reviewed,"9.60"))
    assert approved["approved_cents"]==960 and approved["request_id"] is None

def test_late_ai_cannot_overwrite_human_confirmation(tmp_path):
    path=opened(tmp_path)
    class SlowModel:
        def review(self,text):
            current=case_snapshot(path)
            human_review_case(path,WORDS,True, expected_case_revision=len(current["history"]))
            return BackgroundModel().review(text)
    with pytest.raises(CaseChanged):
        review_case(path,SlowModel())
    assert case_snapshot(path)["review"]["kind"]=="human_selection"

def test_new_buyer_evidence_and_old_tab_cannot_reuse_confirmation(tmp_path):
    path=opened(tmp_path);case=case_snapshot(path)
    reviewed=human_review_case(path,WORDS,True, expected_case_revision=len(case["history"]))
    newer=add_buyer_followup(path,"The replacement is now delayed; contact me before any refund.",len(reviewed["history"]))
    before=path.read_bytes()
    with pytest.raises(CaseChanged):
        human_review_case(path,WORDS,True, expected_case_revision=len(reviewed["history"]))
    with pytest.raises(CaseError):
        approve_case(path,"9.60",approval_phrase(newer,"9.60"))
    assert path.read_bytes()==before

def test_legacy_ai_reviewed_case_still_requires_human_confirmation(tmp_path):
    path=opened(tmp_path);case=review_case(path,BackgroundModel())
    case["state"]="REVIEWED";path.write_text(json.dumps(case))
    with pytest.raises(CaseError,match="Confirm the current"):
        approve_case(path,"9.60",approval_phrase(case,"9.60"))
    confirmed=human_review_case(path,WORDS,True, expected_case_revision=len(case["history"]))
    assert confirmed["review"]["kind"]=="human_selection"

def test_earlier_exact_conditions_survive_review_and_record_then_are_retired(tmp_path):
    path = opened(tmp_path)
    case = case_snapshot(path)
    latest = "Please go ahead under the delivery and no-refund conditions in my first message."
    case = add_buyer_followup(path, latest, len(case["history"]))
    quote = "Do not refund anything yet."
    condition = {"message_index": 0, "source_start": WORDS.index(quote), "quote": quote}
    reviewed = human_review_case(path, latest, True, expected_case_revision=len(case["history"]), selected_conditions=[condition])
    assert reviewed["review"]["selected_conditions"] == [condition]
    assert reviewed["review"]["quote"] == latest
    record = render_case_record(case_snapshot(path))
    assert "Conditions retained by the merchant" in record and quote in record
    newer = add_buyer_followup(path, "I withdraw that request. Please contact me before doing anything.", len(reviewed["history"]))
    assert newer["state"] == "OPEN" and not newer.get("review")
    assert newer["buyer_followups"][-1]["superseded_review"]["selected_conditions"] == [condition]
    assert newer["approved_cents"] == 0 and newer["request_id"] is None
    exported = render_case_record(newer)
    assert "Previous merchant review" in exported and "Conditions retained by the merchant" in exported

@pytest.mark.parametrize("conditions", [
    [{"message_index": True, "source_start": 0, "quote": WORDS}],
    [{"message_index": 9, "source_start": 0, "quote": WORDS}],
    [{"message_index": 0, "source_start": 1, "quote": WORDS}],
    [{"message_index": 0, "source_start": 0, "quote": "Please refund everything."}],
    [{"message_index": 0, "source_start": 0, "quote": ""}],
    [{"message_index": 0, "source_start": 0, "quote": WORDS, "trusted": True}],
    [{"message_index": 0, "source_start": 0, "quote": WORDS}] * 2,
    {},
])
def test_condition_source_validation_never_changes_saved_case(tmp_path, conditions):
    path = opened(tmp_path); case = case_snapshot(path); before = path.read_bytes()
    with pytest.raises(CaseError):
        human_review_case(path, WORDS, True, expected_case_revision=len(case["history"]), selected_conditions=conditions)
    assert path.read_bytes() == before

def test_condition_unicode_offsets_and_stale_revision(tmp_path):
    words = "The 📦 arrived. Only replace it if blue is available."
    path = tmp_path / "case.json"; new_case(path, "CONDITIONUNICODE", "24.40", words)
    case = case_snapshot(path)
    phrase = "Only replace it if blue is available."
    condition = {"message_index": 0, "source_start": words.index(phrase), "quote": phrase}
    result = human_review_case(path, words, True, expected_case_revision=len(case["history"]), selected_conditions=[condition])
    assert result["review"]["selected_conditions"] == [condition]
    before = path.read_bytes()
    with pytest.raises(CaseChanged):
        human_review_case(path, words, True, expected_case_revision=len(case["history"]), selected_conditions=[condition])
    assert path.read_bytes() == before


def test_confirmed_conditions_can_be_reopened_before_approval(tmp_path):
    path = opened(tmp_path); case = case_snapshot(path)
    condition = {"message_index": 0, "source_start": 0, "quote": WORDS}
    reviewed = human_review_case(path, WORDS, True, expected_case_revision=len(case["history"]), selected_conditions=[condition])
    reopened = dismiss_review(path, len(reviewed["history"]))
    assert reopened["state"] == "OPEN" and reopened["review"] is None
    assert reopened["complaint"] == WORDS and reopened["approved_cents"] == 0
    with pytest.raises(CaseError):
        approve_case(path, "9.60", approval_phrase(reopened, "9.60"))
    with pytest.raises(CaseChanged):
        human_review_case(path, WORDS, True, expected_case_revision=len(reviewed["history"]))
    corrected = human_review_case(path, WORDS, True, expected_case_revision=len(reopened["history"]), selected_conditions=[])
    assert corrected["review"].get("selected_conditions", []) == []
    approved = approve_case(path, "9.60", approval_phrase(corrected, "9.60"))
    before = path.read_bytes()
    with pytest.raises(CaseError):
        dismiss_review(path, len(approved["history"]))
    assert path.read_bytes() == before

def test_old_ai_result_cannot_return_after_confirm_and_reopen(tmp_path):
    path = opened(tmp_path)
    class ReviewingWhileMerchantCorrects:
        def review(self, text):
            current = case_snapshot(path)
            reviewed = human_review_case(path, WORDS, True, expected_case_revision=len(current["history"]))
            dismiss_review(path, len(reviewed["history"]))
            return BackgroundModel().review(text)
    with pytest.raises(CaseChanged):
        review_case(path, ReviewingWhileMerchantCorrects())
    case = case_snapshot(path)
    assert case["state"] == "OPEN" and case["review"] is None
    assert case["approved_cents"] == 0 and case["request_id"] is None
    assert case["history"][-1]["event"] == "merchant.review_reopened"


def test_offline_record_links_exact_duplicate_and_overlapping_source_words(tmp_path):
    from html.parser import HTMLParser
    words = 'Earlier: Only if blue. 📦 <img src=x> Current: Only if blue and Friday.'
    path = tmp_path / 'case.json'
    new_case(path, 'RECORDSOURCEWORDS', '24.40', words)
    case = case_snapshot(path)
    phrases = ['Only if blue', 'blue and Friday.']
    conditions = [{'message_index': 0, 'source_start': words.rindex(quote), 'quote': quote} for quote in phrases]
    case = human_review_case(path, words, True, expected_case_revision=len(case['history']), selected_conditions=conditions)
    before = path.read_bytes()
    record = render_case_record(case)
    class Sources(HTMLParser):
        def __init__(self):
            super().__init__(); self.active=False; self.words=''; self.targets={}; self.links=[]; self.mark_targets=[]; self.marked={}
        def handle_starttag(self, tag, attrs):
            attrs=dict(attrs)
            if tag=='blockquote' and attrs.get('data-buyer-source')=='0': self.active=True
            if self.active and attrs.get('id','').startswith('kept-source-'): self.targets[attrs['id']]=len(self.words)
            if self.active and tag=='mark': self.mark_targets=attrs.get('data-source-targets','').split()
            if tag=='a' and attrs.get('href','').startswith('#kept-source-'): self.links.append(attrs['href'][1:])
        def handle_endtag(self, tag):
            if tag=='mark': self.mark_targets=[]
            if tag=='blockquote': self.active=False
        def handle_data(self, data):
            if self.active:
                self.words+=data
                for target in self.mark_targets:self.marked[target]=self.marked.get(target,'')+data
    parsed=Sources(); parsed.feed(record)
    assert parsed.marked == {'kept-source-'+str(n):quote for n,quote in enumerate(phrases)}
    assert parsed.words == words
    assert parsed.links == ['kept-source-0', 'kept-source-1']
    assert parsed.targets == {'kept-source-'+str(n): item['source_start'] for n,item in enumerate(conditions)}
    assert '<img src=x>' not in record
    assert path.read_bytes() == before
    reopened=dismiss_review(path,len(case['history']))
    cleared=render_case_record(reopened)
    assert 'href="#kept-source-' not in cleared
    assert 'Merchant reopened the review' in cleared

def test_kept_email_conditions_keep_neutral_origin_after_supersession(tmp_path):
    path = opened(tmp_path)
    case = case_snapshot(path)
    condition = {"message_index": 0, "source_start": 0, "quote": WORDS}
    case = human_review_case(path, WORDS, True, expected_case_revision=len(case["history"]), selected_conditions=[condition])
    # Rendering uses retained provenance, never guesses the speaker inside an email.
    case["imported_email"] = {"subject": "Re: order", "sender": "Buyer <buyer@example.com>"}
    record = render_case_record(case)
    assert "<p>Email message 1</p>" in record
    assert "<p>Buyer message 1</p>" not in record
    assert 'href="#kept-source-0"' in record
    old_review = case["review"]
    case["review"] = None
    case["state"] = "OPEN"
    case["buyer_followups"] = [{"text": "Please explain the alternative.", "superseded_review": old_review}]
    record = render_case_record(case)
    assert "Previous merchant review" in record and "<p>Email message 1</p>" in record
    assert "<p>Buyer message 1</p>" not in record


def merchant_offer_case(tmp_path):
    from returnlight_app.returnlight import record_merchant_reply
    path = opened(tmp_path); case = case_snapshot(path)
    words = 'Thanks for the 📦 photos. We can deliver only after you confirm a weekday.'
    reply_id = 'a' * 32
    case = record_merchant_reply(path, words, len(case['history']), True, reply_id)
    acceptance = 'Yes, please go ahead with that offer under my earlier no-refund condition.'
    case = add_buyer_followup(path, acceptance, len(case['history']))
    quote = 'We can deliver only after you confirm a weekday.'
    return path, case, words, {'reply_id':reply_id, 'source_start':words.index(quote), 'quote':quote}


def test_merchant_condition_preserves_source_through_saved_work_review_and_record(tmp_path):
    from returnlight_app.returnlight_working_drafts import WorkingDraftStore
    from html.parser import HTMLParser
    path, case, words, condition = merchant_offer_case(tmp_path)
    buyer = {'message_index':0, 'source_start':WORDS.index('Do not refund anything yet.'), 'quote':'Do not refund anything yet.'}
    conditions = [buyer, condition]
    store = WorkingDraftStore(tmp_path)
    draft = {'decision':None, 'reply':None, 'conditions':{'capture':case['capture_id'], 'revision':len(case['history']), 'items':conditions}}
    before = path.read_bytes()
    store.save(path, {'case_revision':len(case['history']), 'draft_version':None, 'draft':draft})
    assert WorkingDraftStore(tmp_path).detail(path)[1]['draft']['conditions']['items'] == conditions
    assert path.read_bytes() == before
    reviewed = human_review_case(path, case['buyer_followups'][-1]['text'], True, expected_case_revision=len(case['history']), selected_conditions=conditions)
    assert reviewed['review']['selected_conditions'] == conditions
    assert WorkingDraftStore(tmp_path).detail(path)[1]['draft']['conditions'] is None
    record = render_case_record(reviewed)
    assert '<p>Merchant reply 1</p>' in record and '<p>Buyer message 1</p>' in record
    class Reply(HTMLParser):
        def __init__(self):
            super().__init__();self.active=False;self.words='';self.target=None
        def handle_starttag(self, tag, attrs):
            attrs=dict(attrs)
            if tag=='p' and attrs.get('data-merchant-reply-id')==condition['reply_id']:self.active=True
            if self.active and attrs.get('id')=='kept-source-1':self.target=len(self.words)
        def handle_endtag(self, tag):
            if tag=='p':self.active=False
        def handle_data(self, data):
            if self.active:self.words+=data
    parsed=Reply();parsed.feed(record)
    assert parsed.words == words and parsed.target == condition['source_start']
    assert 'href="#kept-source-1"' in record
    newer = add_buyer_followup(path, 'Please check another delivery date before proceeding.', len(reviewed['history']))
    assert newer['review'] is None and newer['buyer_followups'][-1]['superseded_review']['selected_conditions'] == conditions
    assert '<p>Merchant reply 1</p>' in render_case_record(newer)


@pytest.mark.parametrize('change', [
    {'reply_id':'b'*32}, {'reply_id':True}, {'source_start':0},
    {'quote':'Do not refund anything yet.'}, {'message_index':0}, {'speaker':'buyer'},
])
def test_invalid_merchant_condition_cannot_change_review_or_saved_work(tmp_path, change):
    from returnlight_app.returnlight_working_drafts import WorkingDraftStore
    path, case, words, condition = merchant_offer_case(tmp_path)
    condition.update(change);before=path.read_bytes()
    with pytest.raises(CaseError):
        human_review_case(path, case['buyer_followups'][-1]['text'], True, expected_case_revision=len(case['history']), selected_conditions=[condition])
    with pytest.raises(CaseError):
        WorkingDraftStore(tmp_path).save(path, {'case_revision':len(case['history']), 'draft_version':None,
            'draft':{'decision':None, 'reply':None, 'conditions':{'capture':case['capture_id'], 'revision':len(case['history']), 'items':[condition]}}})
    assert path.read_bytes() == before

@pytest.mark.parametrize("retained", [False, True])
def test_offline_ai_links_follow_typed_exact_sources_without_changing_case(tmp_path, retained):
    from html.parser import HTMLParser
    from returnlight_app.returnlight_remote_review import validate_conversation_review
    path = opened(tmp_path)
    case = case_snapshot(path)
    quote = "Only after Friday."
    first = "Earlier proposal: " + quote
    latest = "📦 <b>Current request</b> " + quote
    case["complaint"] = first
    case["buyer_followups"] = [{"text": latest}]
    merchant = {"id": "merchant-stable", "text": "We can arrange it. " + quote}
    case["merchant_replies"] = [merchant]
    reading = validate_conversation_review({"current_request":"Confirm Friday.", "source_quotes":[
        {"speaker":"buyer","message_number":2,"quote":quote},
        {"speaker":"merchant","message_number":1,"quote":quote}]},
        [first, latest], [merchant])
    reading["open_questions"] = [1]
    case["history"].append({"event":"merchant.reply_recorded"})
    case["review"] = reading
    if retained:
        case["review"] = {"kind":"human_selection", "quote":latest}
        case["ai_suggestion"] = reading
    before = json.dumps(case, sort_keys=True)
    record = render_case_record(case)
    class Sources(HTMLParser):
        def __init__(self):
            super().__init__();self.source=None;self.words={};self.targets={};self.links=[]
        def handle_starttag(self, tag, attrs):
            attrs=dict(attrs)
            source=attrs.get("data-buyer-source", attrs.get("data-merchant-reply-id"))
            if source is not None:self.source=source;self.words[source]=""
            if self.source is not None and attrs.get("id","").startswith("ai-source-"):
                self.targets[attrs["id"]]=(self.source,len(self.words[self.source]))
            if tag=="a" and "ai-source-link" in attrs.get("class",""):
                self.links.append(attrs["href"][1:])
        def handle_endtag(self, tag):
            if tag in ("blockquote","p"):self.source=None
        def handle_data(self, data):
            if self.source is not None:self.words[self.source]+=data
    parsed=Sources();parsed.feed(record)
    assert parsed.words == {"0":first,"1":latest,"merchant-stable":merchant["text"]}
    assert parsed.targets == {"ai-source-0":("1",latest.index(quote)),
                              "ai-source-1":("merchant-stable",merchant["text"].index(quote))}
    assert set(parsed.links)==set(parsed.targets)
    assert json.dumps(case,sort_keys=True)==before
    assert "<b>Current request</b>" not in record

@pytest.mark.parametrize("change", [{"quote_start":0},{"message_number":99},{"reply_id":"missing","speaker":"merchant"}])
def test_offline_ai_link_never_guesses_when_saved_source_does_not_match(tmp_path, change):
    case=case_snapshot(opened(tmp_path))
    source={"message_number":1,"quote":"Only if it arrives Friday.","quote_start":WORDS.index("Only if")}
    source.update(change)
    case["review"]={"kind":"conversation_brief","summary":"Friday","sources":[source]}
    record=render_case_record(case)
    assert 'class="source-link ai-source-link"' not in record
    assert "Original passage could not be located" in record

@pytest.mark.parametrize("size,collapsed", [(1000,False),(1001,True)])
def test_long_reviewed_passage_is_disclosed_without_losing_words_or_conditions(tmp_path,size,collapsed):
    path=tmp_path/"long.json"
    words="📦"+"x"*(size-1)
    new_case(path,"LONGREVIEWPASSAGE","24.40",words)
    case=case_snapshot(path)
    reviewed=human_review_case(path,words,True,expected_case_revision=len(case["history"]),
        selected_conditions=[{"message_index":0,"source_start":0,"quote":"📦xxx"}])
    record=render_case_record(reviewed)
    assert ('class="reviewed-passage"' in record)==collapsed
    assert words in record
    assert "Conditions retained by the merchant" in record
    assert 'href="#kept-source-0"' in record

def test_support_record_links_each_typed_source_and_retires_stale_reading(tmp_path):
    from returnlight_app.returnlight_support_review import support_sources, validate_support_review
    from html.parser import HTMLParser
    case=case_snapshot(opened(tmp_path))
    case.update(state="PENDING",approved_cents=2850,support_followup_open=True)
    case["complaint"]="📦 Only the pouch."
    case["post_payment_messages"]=[{"text":"Please check the original reference.","payment_state":"PENDING"},
        {"text":"📦 <b>Keep Friday.</b> Do not refund again.","payment_state":"PENDING"}]
    case["merchant_replies"]=[{"id":"recorded-reply","text":"We will keep Friday.","payment_state":"PENDING",
        "through_buyer_followup":0,"through_post_payment_message":1}]
    case["support_resolutions"]=[{"reason":"We explained the pending status.","through_message":1}]
    case["history"].extend({"event":event} for event in ("buyer.post_payment_message","merchant.reply_recorded",
        "merchant.support_handled","buyer.post_payment_message"))
    bank=support_sources(case)
    selected=[{"source_number":n+1,"quote":entry["text"]} for n,entry in enumerate(bank)]
    case["support_review"]=validate_support_review({"current_request":"Check the original refund.","source_quotes":selected},case)
    before=json.dumps(case,sort_keys=True)
    record=render_case_record(case)
    class Sources(HTMLParser):
        def __init__(self):
            super().__init__();self.active=None;self.words={};self.targets={};self.links=[]
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs)
            for attr in ("data-buyer-source","data-support-source","data-merchant-reply-id","data-handling-source","data-payment-source"):
                if attr in attrs:self.active=(attr,attrs[attr]);self.words[self.active]=""
            if self.active and attrs.get("id","").startswith("ai-source-"):
                self.targets[attrs["id"]]=(self.active,len(self.words[self.active]))
            if tag=="a" and "ai-source-link" in attrs.get("class",""):
                self.links.append(attrs["href"][1:])
        def handle_endtag(self,tag):
            if tag in ("blockquote","p","dd"):self.active=None
        def handle_data(self,data):
            if self.active:self.words[self.active]+=data
    parsed=Sources();parsed.feed(record)
    identities=[("data-buyer-source","0"),("data-payment-source","0"),("data-support-source","1"),
                ("data-merchant-reply-id","recorded-reply"),("data-handling-source","1"),("data-support-source","2")]
    assert parsed.words == {identity:entry["text"] for identity,entry in zip(identities,bank)}
    assert parsed.targets == {"ai-source-"+str(n):(identity,0) for n,identity in enumerate(identities)}
    assert parsed.links == ["ai-source-"+str(n) for n in range(len(bank))]
    assert "<b>Keep Friday.</b>" not in record
    assert json.dumps(case,sort_keys=True)==before
    case["post_payment_messages"].append({"text":"New question: when was it checked?","payment_state":"PENDING"})
    stale=render_case_record(case)
    assert "AI reading · current support" not in stale
    assert 'class="source-link ai-source-link"' not in stale


@pytest.mark.parametrize("dismissed,fresh", [(False, False), (True, False), (True, True)])
def test_rejected_questions_stay_historical_until_a_fresh_reading(tmp_path, dismissed, fresh):
    from returnlight_app.returnlight_remote_review import validate_conversation_review
    case = case_snapshot(opened(tmp_path))
    reading = validate_conversation_review({"current_request":"Confirm the Friday condition.",
        "source_quotes":[{"message_number":1,"quote":"Only if it arrives Friday."}],
        "open_questions":[1]}, [WORDS], [])
    case["review"] = {"kind":"human_selection", "quote":WORDS}
    case["ai_suggestion"] = reading
    case["history"].append({"event":"ai.reviewed"})
    if dismissed:
        case["history"].extend([{"event":"merchant.highlight_dismissed"},{"event":"merchant.reviewed"}])
    if fresh:
        case["history"].extend([{"event":"ai.reviewed"},{"event":"merchant.reviewed"}])
    before = json.dumps(case, sort_keys=True)
    record = render_case_record(case)
    assert ("Questions to resolve · AI reading" in record) == (not dismissed or fresh)
    assert "Questions from earlier AI reading" in record
    assert "Only if it arrives Friday." in record
    assert json.dumps(case, sort_keys=True) == before
