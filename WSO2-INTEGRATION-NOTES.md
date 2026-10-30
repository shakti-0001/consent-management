# WSO2 4.7.0.13 integration notes

This folder contains integration guidance, not a drop-in mediation policy. Do not replace existing policies or `velocity_template.xml` with a generic snippet.

Before configuring:
1. Export the API's current OpenAPI definition and mediation policies.
2. Open the installed APIM 4.7.0.13 Publisher and DevPortal REST API specifications and verify exact endpoint/operation names and scopes.
3. Confirm how your Gateway exposes authenticated subject, subscribed application UUID, API UUID, API version, and operation/resource template in mediation context.
4. Configure the Gateway to strip incoming copies of internal identity headers, then set trusted headers from authenticated context.
5. Keep the Python app on a private network with a long internal key and firewall restrictions.
6. Keep WSO2 OAuth2 and subscription validation enabled.
7. Route only a staging API revision through the consent proxy.
8. Verify direct backend access is blocked.

No generic header name or scope in this package should be assumed to match your installed deployment until verified.
