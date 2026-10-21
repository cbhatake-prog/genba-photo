"""HTTP transport integration smoke test (localhost only, no production data)."""
import asyncio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from chatgpt_mcp import server
from mcp.server.transport_security import TransportSecuritySettings

async def main():
    app=server.streamable_http_app(stateless_http=True,json_response=True,transport_security=TransportSecuritySettings(allowed_hosts=["localhost"]))
    # Test Starlette ASGI endpoint locally without publishing it to internet.
    transport=httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport,base_url="http://localhost") as client:
            response=await client.post("/mcp",headers={"Accept":"application/json, text/event-stream","Content-Type":"application/json"},json={"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1"}}})
            print("HTTP_STATUS",response.status_code)
            print("BODY",response.text[:500])
            assert response.status_code==200
asyncio.run(main())
