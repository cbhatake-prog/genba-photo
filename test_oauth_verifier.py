"""Unit test JWT signature, audience, issuer, expiry and scope without live identity provider."""
import asyncio,time
from unittest.mock import patch
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from oauth_verifier import OIDCTokenVerifier

key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
class SigningKey:
    def __init__(self,key):self.key=key
verifier=OIDCTokenVerifier("https://issuer.example.test","https://mcp.example.test","https://issuer.example.test/jwks")
claims={"iss":"https://issuer.example.test","aud":"https://mcp.example.test","sub":"test-user",
        "iat":int(time.time()),"exp":int(time.time())+600,"scope":"genba:upload","azp":"test-client"}
async def verify(data,expected):
    token=jwt.encode(data,key,algorithm="RS256")
    with patch.object(verifier.jwks,"get_signing_key_from_jwt",return_value=SigningKey(key.public_key())):
        result=await verifier.verify_token(token)
    assert (result is not None)==expected,(data,result)
async def main():
    await verify(claims,True)
    await verify({**claims,"aud":"https://other.example.test"},False)
    await verify({**claims,"iss":"https://other.example.test"},False)
    await verify({**claims,"scope":"read"},False)
    await verify({**claims,"exp":int(time.time())-300},False)
    print("OAUTH_JWT_TEST_PASS valid, wrong audience, wrong issuer, scope, expiry")
asyncio.run(main())
