import sys,ssl,json,unittest,threading,socketserver,time
from unittest.mock import patch
from returnlight_app.returnlight_remote_review import RemoteReviewer
from returnlight_app.returnlight import CaseError
class Reply:
 status=200;fp=None
 def __init__(self):
  self.parts=[('data: '+json.dumps({'choices':[{'delta':{'content':'{"value":"read"}'},'finish_reason':'stop'}]})+'\n\ndata: [DONE]\n\n').encode(),b'']
 def read1(self,n):return self.parts.pop(0)
class Connection:
 made=[]
 def __init__(self,*args,**kw):self.args=args;self.kw=kw;self.sock=None;self.calls=[];self.tunnel=None;Connection.made.append(self)
 def set_tunnel(self,*args):self.tunnel=args
 def request(self,*args,**kw):self.calls.append((args,kw))
 def getresponse(self):return Reply()
 def close(self):pass
class Tests(unittest.TestCase):
 def test_explicit_tunnel_and_no_auth(self):
  with patch('returnlight_app.returnlight_remote_review._ReviewHTTPSConnection',Connection),patch.dict('os.environ',{'RETURNLIGHT_REMOTE_REVIEW_KEY':'must-not-leak'}):
   r=RemoteReviewer('https://model.example:444/v1/chat/completions','chosen',proxy_url='http://127.0.0.1:1055',no_auth=True)
   self.assertEqual(r.status()['state'],'configured')
   result=r.review_conversation(['Complete thread'],_validator=lambda v:v)
   c=Connection.made[-1];self.assertEqual(c.args,('127.0.0.1',1055));self.assertEqual(c.tunnel,('model.example',444))
   self.assertNotIn('Authorization',c.calls[0][1]['headers']);self.assertEqual(len(c.calls),1);self.assertEqual(result['value'],'read')
 def test_thinking_is_explicit_and_default_request_is_unchanged(self):
  with patch('returnlight_app.returnlight_remote_review._ReviewHTTPSConnection',Connection):
   for value in [None,False,True]:
    r=RemoteReviewer('https://model.example/v1','chosen',no_auth=True,thinking=value)
    r.review_conversation(['Complete thread'],_validator=lambda v:v)
    body=json.loads(Connection.made[-1].calls[0][1]['body'])
    if value is None:self.assertNotIn('chat_template_kwargs',body)
    else:self.assertEqual(body['chat_template_kwargs'],{'enable_thinking':value})
   with self.assertRaises(CaseError):RemoteReviewer('https://model.example/v1','chosen',thinking='off')
 def test_direct_auth_unchanged_and_missing_key_remains_missing(self):
  with patch('returnlight_app.returnlight_remote_review._ReviewHTTPSConnection',Connection):
   r=RemoteReviewer('https://model.example/v1/chat/completions','chosen',api_key='test-key')
   r.review_conversation(['Complete thread'],_validator=lambda v:v);c=Connection.made[-1]
   self.assertEqual(c.args,('model.example',None));self.assertIsNone(c.tunnel);self.assertEqual(c.calls[0][1]['headers']['Authorization'],'Bearer test-key')
   self.assertEqual(RemoteReviewer('https://model.example/v1','chosen',api_key='').status()['state'],'missing')
 def test_reject_unintended_proxy_routes(self):
  for proxy in ['http://remote.example:1055','https://127.0.0.1:1055','http://user:pass@127.0.0.1:1055','http://127.0.0.1','http://127.0.0.1:0','http://127.0.0.1:1055/path','http://127.0.0.1:1055?x=y','http://127.0.0.1:bad',42]:
   with self.subTest(proxy=proxy),self.assertRaises(CaseError):RemoteReviewer('https://model.example/v1','chosen',proxy_url=proxy)
  with self.assertRaises(CaseError):RemoteReviewer('http://127.0.0.1:8000','chosen',proxy_url='http://127.0.0.1:1055')
 def test_destination_consent_changes_for_route_or_auth(self):
  base=RemoteReviewer('https://model.example/v1','chosen',api_key='test')
  proxied=RemoteReviewer('https://model.example/v1','chosen',api_key='test',proxy_url='http://127.0.0.1:1055')
  noauth=RemoteReviewer('https://model.example/v1','chosen',no_auth=True)
  self.assertEqual(len({x.destination_id for x in [base,proxied,noauth]}),3)
 def test_tls_trust_and_hostname_remain_required(self):
  import http.client
  c=http.client.HTTPSConnection('127.0.0.1',1055);c.set_tunnel('model.example',443)
  self.assertEqual(c._context.verify_mode,ssl.CERT_REQUIRED);self.assertTrue(c._context.check_hostname)
 def test_deadline_stream_closure_is_reported_as_timeout(self):
  class ClosedReply(Reply):
   def read1(self,n):time.sleep(.15);raise AttributeError("closed stream")
  class ClosedConnection(Connection):
   def getresponse(self):return ClosedReply()
  with patch('returnlight_app.returnlight_remote_review._ReviewHTTPSConnection',ClosedConnection):
   r=RemoteReviewer('https://model.example/v1','chosen',no_auth=True,timeout=.05)
   with self.assertRaisesRegex(CaseError,'timed out'):r.review_conversation(['Complete thread'])
 def test_cli_requires_remote_destination_for_transport_settings(self):
  from returnlight_app.returnlight_web import main
  import contextlib,io
  for args in [['--remote-review-proxy','http://127.0.0.1:1055'],['--remote-review-no-auth'],['--remote-review-thinking','off']]:
   with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as err:main(args)
   self.assertEqual(err.exception.code,2)
 def test_cli_passes_explicit_transport_and_thinking_settings(self):
  from returnlight_app.returnlight_web import main
  with patch('returnlight_app.returnlight_web.RemoteReviewer') as reviewer,patch('returnlight_app.returnlight_web.LocalCases'),patch('returnlight_app.returnlight_web.LocalReturnlightServer'):
   main(['--remote-review-url','https://model.example/v1','--remote-review-model','chosen','--remote-review-proxy','http://127.0.0.1:1055','--remote-review-no-auth','--remote-review-thinking','off'])
   reviewer.assert_called_once_with('https://model.example/v1','chosen',timeout=45,proxy_url='http://127.0.0.1:1055',no_auth=True,thinking=False)
 def test_connect_wait_is_inside_whole_call_deadline(self):
  accepted=[]
  class Hold(socketserver.BaseRequestHandler):
   def handle(self):
    accepted.append(self.request.recv(4096));time.sleep(.4)
  class Server(socketserver.ThreadingTCPServer):daemon_threads=True
  with Server(('127.0.0.1',0),Hold) as server:
   thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   r=RemoteReviewer('https://model.example/v1','chosen',no_auth=True,timeout=.1,proxy_url='http://127.0.0.1:'+str(server.server_address[1]))
   t=time.monotonic()
   with self.assertRaisesRegex(CaseError,'timed out'):r.review_conversation(['Complete thread'])
   self.assertLess(time.monotonic()-t,1);self.assertEqual(len(accepted),1);self.assertTrue(accepted[0].startswith(b'CONNECT model.example:443'))
   server.shutdown()

 def test_real_tls_still_requires_trusted_certificate_and_matching_hostname(self):
  import shutil,subprocess,tempfile,os
  from pathlib import Path
  from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
  if not shutil.which('openssl'):self.skipTest('openssl is required to generate the local test certificate')
  with tempfile.TemporaryDirectory() as temporary:
   root=Path(temporary);cert=root/'cert.pem';key=root/'key.pem'
   subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
    '-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1',
    '-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   calls=[]
   class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
     calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
     self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
     self.wfile.write(Reply().parts[0])
   server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
   context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
   server.socket=context.wrap_socket(server.socket,server_side=True)
   thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   try:
    for trust,hostname,success in [(False,'127.0.0.1',False),(True,'127.0.0.1',True),(True,'localhost',False)]:
     env=dict(os.environ)
     if trust:env['SSL_CERT_FILE']=str(cert)
     else:env.pop('SSL_CERT_FILE',None)
     with patch.dict(os.environ,env,clear=True):
      reader=RemoteReviewer('https://'+hostname+':'+str(server.server_port)+'/chat','controlled-tls',api_key='fixture-only',timeout=2)
      count=len(calls)
      if success:
       result=reader.review_conversation(['Complete retained thread'],_validator=lambda v:v)
       self.assertEqual(result['value'],'read');self.assertEqual(len(calls),count+1)
      else:
       with self.assertRaises(CaseError):reader.review_conversation(['Complete retained thread'])
       self.assertEqual(len(calls),count)
   finally:server.shutdown();server.server_close();thread.join()

if __name__ == '__main__':unittest.main()
