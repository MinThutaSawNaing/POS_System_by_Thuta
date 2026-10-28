# Optional email two-step login

## Configure delivery

1. In Resend, verify a sending domain and create a sending-only API key scoped to that domain.
2. Sign in as a manager or owner and open **Settings → Email**.
3. Enter the sender address on that domain, the API key, and your current account password, then select **Save email settings**. Settings apply to all branches. A blank API key preserves the existing key. Password confirmation prevents an unattended manager session alone from replacing the delivery provider.
4. Use a test user and an inbox you control to verify enrollment and login before enabling this on operational accounts. A configured status confirms local configuration, not inbox delivery or provider acceptance.

Alternatively set `RESEND_API_KEY` and `RESEND_FROM_EMAIL` in the deployment environment. Saved settings override environment values. Docker Compose passes these variables through. Keep `SECRET_KEY` private and stable: it protects sessions, stored credentials, and verification-code hashes. Back it up separately from the database. Email enrollment/configuration refuses the built-in placeholder secret. For HTTPS production, set `SESSION_COOKIE_SECURE=true`; leave it false only for local HTTP development.

Provider documentation: https://resend.com/docs/api-reference/emails/send-email and https://resend.com/docs/dashboard/domains/introduction.

## Enable or remove protection

- **User Management → Add email**: enter the user's address and that account's current password. Enter the code delivered to the address. Only successful verification links the email and enables two-step login.
- Every role can also use **My login security** in the sidebar for its own account, without access to administrative settings.
- Managers can assist users in the active branch, but must still have the account owner confirm the target account's password and inbox code.
- A linked user sees **Unlink email**. This requires the current password and a fresh code sent to the existing inbox. Successful unlinking restores password-only login. To change addresses, unlink the old one first, then verify the new one.
- Users without a linked email continue to sign in with their password as before.

After linking, login checks the password first and then asks for a six-digit email code. No authenticated session is established before the second step. “Remember me” applies only after both steps succeed. Closing the setup dialog clears passwords/codes; it does not enable or disable protection.

## Code and session controls

- Codes expire after 10 minutes and accept at most five submissions, including malformed values. Resending does not reset failed attempts.
- A new code invalidates the previous code. Successful verification is single-use, including when an older browser cookie is replayed.
- Sends are limited to one per account per 60 seconds, five per account and recipient per hour, and 20 per source IP per hour. Limits are stored in SQLite and shared across workers and restarts; failed deliveries also consume the allowance to prevent abuse.
- Source IP comes from the direct connection, not an untrusted forwarded header. Behind a reverse proxy, the IP limit may be shared by users through that proxy. Account/recipient limits remain independent.
- Challenges are bound to the browser session, purpose, target account credentials, branch, and acting user. Password, role, branch-assignment, or linked-email changes invalidate pending challenges. Completed sessions are revoked when their security fingerprint changes. A persistent authentication version increments on every factor change, so unlinking or relinking the same address cannot revive old cookies. After enrollment, old pre-feature sessions without a fingerprint remain rejected even if the email is later unlinked.
- SQLite transactions serialize code verification and send reservations. Provider calls use bounded timeouts and occur outside database write locks.
- Codes are stored as keyed hashes; the Resend key is encrypted using the existing application secret-storage mechanism. Codes, passwords, and API keys never enter browser storage. Mutations require a session CSRF token. Authentication pages/API responses are not cached.
- Factor changes and email-settings updates appear in audit logs without codes, passwords, or API keys.

## Delivery failures and recovery

Email is an external dependency. If Resend is unavailable, credentials are invalid, or the user cannot access the linked inbox, login does **not** fall back to password-only. Provider success means the request was accepted, not that inbox delivery is guaranteed. Check spam, verify the sender/domain and Resend logs, and retry after the displayed wait.

There is intentionally no password-only administrative unlink endpoint, universal override code, or self-service lost-inbox bypass. Such a bypass would defeat the protection. Before rollout, establish an identity-verified operator recovery procedure and maintain protected database backups. A deployment operator may need to perform a controlled account recovery after verifying identity; record the incident and invalidate affected sessions. Do not use database restore or secret rotation as a routine unlink method.

Email verification adds a useful second step but is not phishing-resistant and relies on the security of the user's inbox. A future high-assurance rollout should consider passkeys or authenticator-based factors and one-time recovery codes.

## Verification

From the repository root, run `python run_tests.py test_email_auth.py test_email_security_javascript.py -q --tb=short`. The runner copies source to a disposable directory, strips Resend/deployment credentials, and avoids the local POS database. Provider calls in tests are mocked; real delivery must be checked separately with your own verified Resend account.