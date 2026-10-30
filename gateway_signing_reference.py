"""Reference for TRUSTED SERVER-SIDE adapter ONLY. Never run this in React/browser.
Call sign_identity only AFTER WSO2 validates the session/token and ownership.
The gateway/adapter must REMOVE client supplied identity and signature headers.
"""
import hashlib, hmac, time

def sign_identity(secret, method, path, user, application_id, roles="", api_id="", version="", operation=""):
    ts = str(int(time.time()))
    canonical = "\n".join([ts, method.upper(), path, user, application_id, roles, api_id, version, operation])
    signature = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Authenticated-User": user,
        "X-Authenticated-Application": application_id,
        "X-Authenticated-Roles": roles,
        "X-WSO2-API-ID": api_id,
        "X-WSO2-API-Version": version,
        "X-WSO2-Operation-ID": operation,
        "X-Consent-Timestamp": ts,
        "X-Consent-Signature": signature,
    }
