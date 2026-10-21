"""Local HTTP MCP protocol tests. No production upload."""
import asyncio,json
import httpx
from chatgpt_mcp import server
from mcp.server.transport_security import TransportSecuritySettings

async def main():
    app=server.streamable_http_app(stateless_http=True,json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=["localhost"]))
    transport=httpx.ASGITransport(app=app)
    headers={"Accept":"application/json, text/event-stream","Content-Type":"application/json"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport,base_url="http://localhost") as client:
            init=await client.post("/mcp",headers=headers,json={"jsonrpc":"2.0","id":1,
                "method":"initialize","params":{"protocolVersion":"2025-03-26",
                "capabilities":{},"clientInfo":{"name":"genba-test","version":"1"}}})
            assert init.status_code==200,init.text
            assert init.json()["result"]["serverInfo"]["name"]=="genba-photo"
            tools=await client.post("/mcp",headers=headers,json={"jsonrpc":"2.0","id":2,
                "method":"tools/list","params":{}})
            assert tools.status_code==200,tools.text
            names=[x["name"] for x in tools.json()["result"]["tools"]]
            assert "register_project_file" in names,names
            call=await client.post("/mcp",headers=headers,json={"jsonrpc":"2.0","id":3,
                "method":"tools/call","params":{"name":"register_project_file",
                "arguments":{"site_token":"test","file_url":"https://example.test/photo.jpg","filename":"photo.jpg"}}})
            assert call.status_code==200,call.text
            assert "not configured" in call.text,call.text
            print("MCP_HTTP_INIT_TOOLS_AND_CALL_PASS",names)
asyncio.run(main())
