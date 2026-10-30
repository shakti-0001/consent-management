# Consent service — trusted portal adapter edition

## What is included
- Existing FastAPI endpoints and DB schema retained.
- FastAPI does not validate browser JWTs or require a separate user login.
- All user-facing endpoints REQUIRE signed identity from a trusted server-side adapter.
- Identity HMAC covers timestamp, HTTP method, path, user, app, roles and API operation context.
- My requests/grants are scoped by both user AND application.
- Internal registry registration still uses a separate internal key.

## Deployment
1. Install `pip install -r requirements.txt`.
2. Set a fresh random GATEWAY_IDENTITY_SIGNING_KEY (>=32 chars) in .env; NEVER send it to the browser.
3. Bind FastAPI to 127.0.0.1:8100; firewall it from browsers and untrusted hosts.
4. Implement a trusted server-side WSO2 portal adapter or validated Gateway mediator. It MUST validate the portal session/token, determine real user, application and roles, reject spoofed incoming identity headers, then generate signature per gateway_signing_reference.py.
5. Forward signed requests to FastAPI. Route /api/* preserving exact method and path, or re-sign after rewriting.
6. Publisher adapter MUST obtain publisher identity and roles from trusted WSO2 user context and enforce API ownership.
7. Test GET /api/registry, GET /api/consent/requests/mine, POST request, pending, decision, grant, proxy, revoke.

## Important integration gaps
This bundle is NOT a drop-in WSO2 Synapse mediation sequence. It does NOT install an adapter into WSO2 4.7.0.13. Your live 401 will persist until the authenticated adapter/mediation is deployed. Do NOT manually set trusted headers from the browser.
Automatic WSO2 API metadata sync and business-API subscription verification are NOT implemented in the supplied original service. The consent proxy's recursive filtering is POC-only; production requires explicit per-operation response schemas. WSO2 portal session cookies do not automatically authorize calls to OAuth2-protected gateway APIs. Use a backend-for-frontend with session authentication for seamless login.
