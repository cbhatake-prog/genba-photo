"""Fail-closed deployment checks, without contacting production."""
import os
from unittest.mock import patch
from mcp_deploy import build_app
def expect_reject(env):
 with patch.dict(os.environ,env,clear=True):
  try:build_app()
  except (RuntimeError,ValueError):return
  raise AssertionError("unsafe configuration was accepted")
expect_reject({})
expect_reject({"GENBA_MCP_PUBLIC_URL":"http://mcp.example.test"})
expect_reject({"GENBA_MCP_PUBLIC_URL":"https://mcp.example.test"})
with patch.dict(os.environ,{
 "GENBA_MCP_PUBLIC_URL":"https://mcp.example.test",
 "GENBA_OIDC_ISSUER":"https://issuer.example.test",
 "GENBA_OIDC_JWKS_URL":"https://issuer.example.test/jwks",
 "GENBA_SOURCE_HOSTS":"files.example.test",
 "GENBA_BRIDGE_TOKEN":"test-token"
},clear=True):
 app=build_app()
 assert app is not None
print("DEPLOY_CONFIG_TEST_PASS")
