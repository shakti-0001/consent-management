# Deployment checklist

- Keep FastAPI bound to 127.0.0.1 or a private network.
- Never expose the internal key or signing key in React.
- Configure WSO2 introspection URL and confidential client on the server.
- Confirm introspection returns `active=true`, a user claim, and `client_id`.
- Confirm role claims; if absent, add trusted role lookup before Publisher operations.
- Configure the gateway adapter to sign requests to the proxy with the exact canonical method/path/user/app/roles/API/version/operation fields.
- Make sure the application identifier is consistent: management currently keys grants by OAuth `client_id`; the proxy adapter must use that same value.
- Implement WSO2 subscription checks and automatic Publisher API metadata sync before production.
- Configure Classic Gateway mediation so all protected responses pass through consent enforcement; block direct backend bypass.
- Replace recursive key-name filtering with per-operation schema-aware filtering.
