# Test flow

## Developer
1. Log in to WSO2 DevPortal.
2. Subscribe the selected application to the API with a normal WSO2 tier.
3. Open the consent UI (the supplied `devportal/ConsentManagement.jsx` is a UI example only).
4. Request allowed fields and a permitted purpose.
5. See Pending, Approved/Partially approved, Rejected, Expired or Revoked.

## Publisher
1. Log in to Publisher.
2. Open consent requests for an API owned by the current publisher.
3. Review developer, application, API/version, operation, purpose, duration and fields.
4. Approve a subset or reject.
5. Verify grant expiry and revocation.

## API caller
1. Send a valid OAuth2 token from the WSO2 application and confirm normal subscription checks still apply.
2. Before consent approval, expect 403 from the consent proxy.
3. Approve fields `name` only.
4. Confirm response filtering returns only approved keys.
5. Revoke or expire the grant and confirm subsequent calls fail.
6. Attempt forged identity/application headers and direct backend access; these must be blocked by WSO2/network configuration.

## Do not claim full integration until
- WSO2 mediation identity property names are confirmed on APIM 4.7.0.13.
- Real API metadata synchronizer is implemented.
- DevPortal and Publisher UI changes are merged/built against the exact installed source.
- API subscription status is independently verified in the gateway flow.
