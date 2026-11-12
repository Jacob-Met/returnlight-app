import contextlib,http.client,json,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
from returnlight_app.returnlight import CaseError,case_snapshot
from returnlight_app.returnlight_remote_review import RemoteReviewer
from returnlight_app.returnlight_web import LocalCases,LocalReturnlightServer
PRIVATE="private buyer and draft words"
@contextlib.contextmanager
def provider(mode):
 calls=[]
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def do_POST(self):
   calls.append(self.rfile.read(int(self.headers["Content-Length"])))
   if mode=="status":
    self.send_response(503);self.end_headers();return
   if mode=="headers":
    time.sleep(.5);return
   self.send_response(200);self.send_header("Content-Type","text/event-stream");self.end_headers()
   event={"choices":[{"delta":{"content":PRIVATE,"reasoning_content":PRIVATE}}]}
   if mode=="incomplete":event["choices"][0]["finish_reason"]=["private invalid finish"]
   self.wfile.write(("data: "+json.dumps(event)+"\n\n").encode());self.wfile.flush()
   if mode=="stream":time.sleep(.5)
 class Server(ThreadingHTTPServer):daemon_threads=True
 with Server(("127.0.0.1",0),Handler) as server:
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:yield f"http://127.0.0.1:{server.server_port}/v1",calls
  finally:server.shutdown();thread.join(2)
@pytest.mark.parametrize("mode,phase,streamed",[("headers","waiting_headers",False),("stream","reading_stream",True),("incomplete","reading_stream",True)])
def test_private_failure_phases_are_actual_client_observations(mode,phase,streamed):
 with provider(mode) as (url,calls):
  r=RemoteReviewer(url,"controlled",no_auth=True,timeout=.15)
  with pytest.raises(CaseError) as caught:r.review_conversation([PRIVATE])
  d=caught.value.review_diagnostics
  assert d["phase"]==phase and len(calls)==1
  assert d["request_written_seconds"] is not None
  assert (d["response_headers_seconds"] is not None)==streamed
  assert (d["first_stream_byte_seconds"] is not None)==streamed
  assert (d["stream_bytes"]>0)==streamed
  assert d["content_characters"]==(len(PRIVATE) if streamed else 0)
  assert d["reasoning_characters"]==(len(PRIVATE) if streamed else 0)
  assert d["timed_out"]==(mode!="incomplete")
  assert not d["stream_finished"] and PRIVATE not in json.dumps(d)
  assert "private invalid finish" not in json.dumps(d)
def test_http_failure_keeps_diagnostic_and_case_unchanged(tmp_path):
 with provider("stream") as (url,calls):
  reviewer=RemoteReviewer(url,"controlled",no_auth=True,timeout=.15)
  app=LocalCases(tmp_path,remote_reviewer=reviewer)
  opened=app.create({"capture_id":"TIMINGCASE01","ceiling":"49.00","complaint":PRIVATE})
  key=opened["key"];before=app.path(key).read_bytes()
  with LocalReturnlightServer(("127.0.0.1",0),app) as server:
   thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   try:
    connection=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
    payload={"case_revision":len(opened["case"]["history"]),"remote_confirmed":True,"destination_id":reviewer.destination_id}
    connection.request("POST",f"/api/cases/{key}/review",body=json.dumps(payload),headers={"Content-Type":"application/json","X-Returnlight-Token":server.token})
    response=connection.getresponse();body=json.loads(response.read());connection.close()
    assert response.status==422 and "timed out" in body["error"]
    assert body["review_diagnostics"]["phase"]=="reading_stream"
    assert body["review_diagnostics"]["content_characters"]==len(PRIVATE)
    assert PRIVATE not in json.dumps(body)
   finally:server.shutdown();thread.join(2)
  assert app.path(key).read_bytes()==before
  case=case_snapshot(app.path(key));assert case["state"]=="OPEN" and case["approved_cents"]==0 and len(calls)==1

def test_http_refusal_is_headers_not_generation():
 with provider("status") as (url,calls):
  with pytest.raises(CaseError) as caught:RemoteReviewer(url,"controlled",no_auth=True).review_conversation([PRIVATE])
  d=caught.value.review_diagnostics
  assert d["phase"]=="response_headers" and d["response_status"]==503
  assert not d["timed_out"] and d["stream_bytes"]==0 and d["first_stream_byte_seconds"] is None
  assert len(calls)==1
def test_connection_failure_has_no_claim_of_server_receipt(monkeypatch):
 class Broken:
  sock=None
  def __init__(self,*args,**kwargs):pass
  def request(self,*args,**kwargs):raise OSError("private socket diagnostic")
  def close(self):pass
 monkeypatch.setattr("returnlight_app.returnlight_remote_review._ReviewHTTPSConnection",Broken)
 with pytest.raises(CaseError) as caught:RemoteReviewer("https://model.example/v1","controlled",no_auth=True).review_conversation([PRIVATE])
 d=caught.value.review_diagnostics
 assert d["phase"]=="writing_request" and d["request_written_seconds"] is None
 assert d["response_status"] is None and d["stream_bytes"]==0 and not d["timed_out"]
 assert "private socket diagnostic" not in json.dumps(d) and PRIVATE not in json.dumps(d)
