"""MCP resource server for GENBA PHOTO. Requires trusted external OIDC issuer."""
import os
from urllib.parse import urlparse
from mcp.server.mcpserver import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from oauth_verifier import OIDCTokenVerifier
import chatgpt_mcp

def build_app():
    public=os.environ.get("GENBA_MCP_PUBLIC_URL","").rstrip("/")
    issuer=os.environ.get("GENBA_OIDC_ISSUER","").rstrip("/")
    jwks=os.environ.get("GENBA_OIDC_JWKS_URL","")
    hosts=os.environ.get("GENBA_SOURCE_HOSTS","")
    if not public.startswith("https://") or not urlparse(public).hostname:
        raise RuntimeError("GENBA_MCP_PUBLIC_URL must be HTTPS")
    if not issuer or not jwks or not hosts or not os.environ.get("GENBA_BRIDGE_TOKEN"):
        raise RuntimeError("OIDC issuer, JWKS, source allowlist and GENBA_BRIDGE_TOKEN required")
    verifier=OIDCTokenVerifier(issuer,public,jwks)
    server=MCPServer("genba-photo",token_verifier=verifier,
        auth=AuthSettings(issuer_url=issuer,resource_server_url=public,
            required_scopes=["genba:upload"],validate_token_resource=True))
    # Register the existing upload tool, preserving its explicit user-intent requirement.
    server.add_tool(chatgpt_mcp.register_project_file)
    return server.streamable_http_app(stateless_http=True,json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=[urlparse(public).netloc]))

def app_factory():
    return build_app()

if __name__=="__main__":
    build_app()
    print("MCP OAuth resource-server configuration validated")
