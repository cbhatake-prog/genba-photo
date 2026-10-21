"""Verify the actual MCP stdio protocol exposes the tool."""
import asyncio,sys
from mcp import ClientSession
from mcp.client.stdio import stdio_client,StdioServerParameters
async def main():
 params=StdioServerParameters(command=sys.executable,args=["chatgpt_mcp.py"])
 async with stdio_client(params) as (reader,writer):
  async with ClientSession(reader,writer) as session:
   await session.initialize()
   tools=await session.list_tools()
   names=[t.name for t in tools.tools]
   assert "register_project_file" in names,names
   print("MCP_PROTOCOL_PASS",names,flush=True)
asyncio.run(main())
