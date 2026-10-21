"""Local end-to-end adapter -> ingest route -> storage test, no production writes."""
import asyncio,os,tempfile
from pathlib import Path
from unittest.mock import patch
from flask import Flask
import httpx
import chatgpt_mcp
from chatgpt_bridge import register_bridge

class SourceResponse:
    content=b"fake-image-content"
    def raise_for_status(self):pass
class UploadResponse:
    def __init__(self,status,payload):self.status_code=status;self.payload=payload
    def raise_for_status(self):
        if self.status_code>=400:raise RuntimeError(self.payload)
    def json(self):return self.payload

async def main():
 with tempfile.TemporaryDirectory() as tmp:
  flask=Flask("test")
  register_bridge(flask,lambda t:{"name":"test"} if t=="testsite" else None,
                  Path(tmp),{"photos":"photos","docs":"docs"},{".jpg",".png"})
  os.environ["GENBA_BRIDGE_TOKEN"]="test-secret"
  os.environ["GENBA_SOURCE_HOSTS"]="files.example.test"
  chatgpt_mcp.TOKEN="test-secret"
  class Client:
   def __init__(self,**kw):pass
   async def __aenter__(self):return self
   async def __aexit__(self,*args):pass
   async def get(self,url):
    assert url=="https://files.example.test/photo.jpg"
    return SourceResponse()
   async def post(self,url,headers,files):
    assert url.endswith("/api/chatgpt/ingest/testsite")
    with flask.test_client() as c:
     name,data,mime=files["files"]
     from io import BytesIO
     response=c.post("/api/chatgpt/ingest/testsite",headers=headers,
                     data={"files":(BytesIO(data),name)},content_type="multipart/form-data")
     return UploadResponse(response.status_code,response.json)
  with patch.object(chatgpt_mcp.httpx,"AsyncClient",Client):
   first=await chatgpt_mcp.register_project_file("testsite","https://files.example.test/photo.jpg","photo.jpg")
   second=await chatgpt_mcp.register_project_file("testsite","https://files.example.test/photo.jpg","photo.jpg")
  assert first["uploaded"]==1,first
  assert second["uploaded"]==0 and second["results"][0]["status"]=="duplicate",second
  saved=list((Path(tmp)/"testsite"/"photos").glob("*.jpg"))
  assert len(saved)==1 and saved[0].read_bytes()==b"fake-image-content",saved
  print("END_TO_END_PASS MCP adapter -> Flask ingest -> file saved -> duplicate rejected")
asyncio.run(main())
