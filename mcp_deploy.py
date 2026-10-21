"""Safe MCP deployment entrypoint.
Fails closed unless HTTPS public URL, OAuth token verification and host allowlist
are configured. Do not expose unauthenticated MCP tool endpoints publicly.
"""
import os
from urllib.parse import urlparse
from chatgpt_mcp import server
from mcp.server.transport_security import TransportSecuritySettings

def validate_config():
    public=os.environ.get("GENBA_MCP_PUBLIC_URL","")
    parsed=urlparse(public)
    if parsed.scheme!="https" or not parsed.hostname:
        raise RuntimeError("GENBA_MCP_PUBLIC_URL must be a public HTTPS URL")
    if not os.environ.get("GENBA_BRIDGE_TOKEN"):
        raise RuntimeError("GENBA_BRIDGE_TOKEN is required")
    if not os.environ.get("GENBA_SOURCE_HOSTS"):
        raise RuntimeError("GENBA_SOURCE_HOSTS must be explicitly allowlisted")
    if os.environ.get("GENBA_MCP_OAUTH_READY")!="true":
        raise RuntimeError("OAuth verification is not implemented; public deployment blocked")
    raise RuntimeError("OAuth verification must be implemented and reviewed before deployment")

def app_factory():
    validate_config()
    # Intentionally unreachable until OAuth verification is implemented.
    return server.streamable_http_app(stateless_http=True,json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=[urlparse(os.environ["GENBA_MCP_PUBLIC_URL"]).netloc]))

if __name__=="__main__":
    validate_config()
