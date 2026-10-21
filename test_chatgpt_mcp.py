"""Offline integration test of MCP adapter, no production uploads."""
import os,asyncio
from unittest.mock import patch
import chatgpt_mcp
os.environ["GENBA_SOURCE_HOSTS"]="files.example.test"
chatgpt_mcp.TOKEN="local-test-token"
class Response:
 def __init__(self,data=None):self.content=b"test image bytes";self.data=data or {"uploaded":1,"results":[{"status":"uploaded"}]}
 def raise_for_status(self):pass
 def json(self):return self.data
class FakeClient:
 def __init__(self,**kwargs):self.kwargs=kwargs
 async def __aenter__(self):return self
 async def __aexit__(self,*args):pass
 async def get(self,url):assert url=="https://files.example.test/photo.jpg";return Response()
 async def post(self,url,headers,files):
  assert url.endswith("/api/chatgpt/ingest/testsite")
  assert headers["Authorization"]=="Bearer local-test-token"
  assert files["files"][0]=="photo.jpg"
  assert files["files"][1]==b"test image bytes"
  return Response()
async def main():
 with patch.object(chatgpt_mcp.httpx,"AsyncClient",FakeClient):
  r=await chatgpt_mcp.register_project_file("testsite","https://files.example.test/photo.jpg","photo.jpg")
  assert r["uploaded"]==1,r
  for url in ["http://files.example.test/photo.jpg","https://127.0.0.1/private","https://not-allowed.test/photo.jpg"]:
   bad=await chatgpt_mcp.register_project_file("testsite",url,"photo.jpg")
   assert bad["ok"] is False,bad
 print("MCP_ADAPTER_TEST_PASSED upload call, HTTPS check, host allowlist")
asyncio.run(main())
