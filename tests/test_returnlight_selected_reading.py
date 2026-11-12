import pytest
from returnlight_app.returnlight import CaseError,new_case,case_snapshot,review_case,add_buyer_followup

WORDS="Please replace only the cracked bowl. Only if it arrives Friday. Do not refund anything yet."
class BackgroundModel:
    def review(self,text):
        return {"kind":"source_selection","quote":"Only if it arrives Friday."}
def opened(tmp_path):
    path=tmp_path/"case.json"
    new_case(path,"HUMANREVIEWCAPTURE","24.40",WORDS)
    return path

def test_selected_sources_reach_case_reader_without_advancing_review(tmp_path):
    path=opened(tmp_path);basis=case_snapshot(path)
    condition={"message_index":0,"source_start":WORDS.index("Do not refund"),"quote":"Do not refund anything yet."}
    class Reader:
        supports_selected_conditions=True
        def review_case_conversation(self,case,*,before_request,selected_conditions):
            before_request()
            assert selected_conditions==[condition] and case==basis
            return BackgroundModel().review(WORDS)
    result=review_case(path,Reader(),expected_case_revision=len(basis["history"]),selected_conditions=[condition])
    assert result["state"]=="OPEN" and result["approved_cents"]==0 and result["request_id"] is None

@pytest.mark.parametrize("variant",["wrong_words","duplicate","stale","missing_revision","unsupported"])
def test_selected_sources_refuse_before_model_dispatch(tmp_path,variant):
    path=opened(tmp_path);basis=case_snapshot(path);before=path.read_bytes()
    condition={"message_index":0,"source_start":WORDS.index("Do not refund"),"quote":"Do not refund anything yet."}
    conditions=[condition];revision=len(basis["history"]);calls=[]
    class Reader:
        supports_selected_conditions=variant!="unsupported"
        def review_case_conversation(self,*args,**kwargs):
            calls.append(True);raise AssertionError("Invalid focus was sent")
    if variant=="wrong_words":condition["quote"]="Refund everything now."
    if variant=="duplicate":conditions.append(dict(condition))
    if variant=="stale":revision+=1
    if variant=="missing_revision":revision=None
    with pytest.raises(CaseError):
        review_case(path,Reader(),expected_case_revision=revision,selected_conditions=conditions)
    assert not calls and path.read_bytes()==before

def test_remote_focus_retains_whole_thread_and_exact_question(tmp_path):
    from returnlight_app.returnlight_remote_review import RemoteReviewer
    path=opened(tmp_path);case=case_snapshot(path)
    question="Do I need to print the return label, or can the driver bring one?"
    case=add_buyer_followup(path,question,len(case["history"]))
    condition={"message_index":0,"source_start":WORDS.index("Do not refund"),"quote":"Do not refund anything yet."}
    class CapturingReviewer(RemoteReviewer):
        def review_conversation(self,messages,**kwargs):
            return kwargs["_content"]
    reader=CapturingReviewer("https://example.com/review","controlled",api_key="fixture")
    content=reader.review_case_conversation(case,selected_conditions=[condition])
    before,focus=content.split("MERCHANT-SELECTED SOURCE CONDITIONS:")
    assert WORDS in before and question in before
    assert condition["quote"] in focus and "BUYER MESSAGE 1, character" in focus
    assert "unanswered questions unresolved" in focus and "do not by themselves prove agreement" in focus
