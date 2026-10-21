"""OAuth2 resource-server token verification for GENBA PHOTO MCP.
Requires external trusted OIDC issuer and JWKS. No shared static bearer tokens.
"""
import os,time
from urllib.parse import urlparse
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken

class OIDCTokenVerifier:
    def __init__(self,issuer,audience,jwks_url):
        for url in (issuer,jwks_url):
            parsed=urlparse(url)
            if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Issuer and JWKS must be HTTPS")
        if not audience:raise ValueError("Audience required")
        self.issuer=issuer.rstrip("/")
        self.audience=audience
        self.jwks=PyJWKClient(jwks_url,cache_jwk_set=True,lifespan=300)
    async def verify_token(self,token):
        try:
            key=self.jwks.get_signing_key_from_jwt(token)
            claims=jwt.decode(token,key.key,algorithms=["RS256","ES256"],
                issuer=self.issuer,audience=self.audience,
                options={"require":["exp","iat","iss","aud","sub"]},
                leeway=30)
            scope=claims.get("scope","")
            scopes=scope.split() if isinstance(scope,str) else []
            if "genba:upload" not in scopes:return None
            return AccessToken(token=token,client_id=str(claims.get("azp") or claims.get("client_id") or "oidc-client"),
                scopes=scopes,expires_at=int(claims["exp"]),resource=self.audience,
                subject=str(claims["sub"]))
        except (jwt.PyJWTError,ValueError,KeyError,TypeError):
            return None
