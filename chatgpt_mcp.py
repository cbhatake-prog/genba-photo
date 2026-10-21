"""GENBA PHOTO MCP adapter (local prototype).
Receives a file URL only when explicitly supplied by the calling client.
It does NOT automatically access ChatGPT Project attachments.
"""
import os
from urllib.parse import urlparse
from pathlib import Path
import httpx
from mcp.server.mcpserver import MCPServer

server=MCPServer("genba-photo")
BASE=os.environ.get("GENBA_PHOTO_BASE","https://photo.j-cb.com").rstrip("/")
TOKEN=os.environ.get("GENBA_BRIDGE_TOKEN","")
MAX_BYTES=50*1024*1024

@server.tool()
async def register_project_file(site_token:str, file_url:str, filename:str)->dict:
    """Register one authorized file URL in an existing GENBA PHOTO site.
    This action uploads data; invoke only when user requested registration.
    """
    if not TOKEN:
        return {"ok":False,"error":"GENBA_BRIDGE_TOKEN not configured"}
    parsed=urlparse(file_url)
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password:
        return {"ok":False,"error":"Only authenticated HTTPS file URLs are accepted"}
    # Explicit allowlist prevents the MCP server from fetching arbitrary hosts.
    allowed={x.strip().lower() for x in os.environ.get("GENBA_SOURCE_HOSTS","").split(",") if x.strip()}
    if parsed.hostname.lower() not in allowed:
        return {"ok":False,"error":"Source hostname is not allowlisted"}
    if not site_token or "/" in site_token or not filename or Path(filename).name!=filename:
        return {"ok":False,"error":"Invalid site or filename"}
    async with httpx.AsyncClient(timeout=45,follow_redirects=False) as client:
        source=await client.get(file_url)
        source.raise_for_status()
        if len(source.content)>MAX_BYTES:
            return {"ok":False,"error":"File exceeds 50 MiB"}
        result=await client.post(f"{BASE}/api/chatgpt/ingest/{site_token}",
            headers={"Authorization":"Bearer "+TOKEN},
            files={"files":(filename,source.content,"application/octet-stream")})
        result.raise_for_status()
        return result.json()

if __name__=="__main__":
    server.run(transport="stdio")
