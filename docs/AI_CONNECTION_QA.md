# AI connection — Windows acceptance checks

Branch: feat/provider-auto-connect. No Windows runtime verification yet.

1. Open tab 5, connect Groq, verify embedded console loads. If login is blocked, use external browser.
2. Create a key on the official console and copy it. Use explicit import; verify invalid clipboard is rejected.
3. Verify valid key and selected model activate provider. Invalid key must not be saved.
4. Close during validation: dialog must remain safe and responsive.
5. Restart app; verify provider remains active. Inspect settings.json: on Windows per_provider keys should be stored as keys_dpapi, not plaintext keys.
6. Test translation and dubbing with the same provider configuration.
7. Verify clipboard watching is off by default and other providers still work.
8. Verify legacy settings migrate on save. Keep a secure backup before testing.

Limitations: no verified official automated Groq key provisioning; user creates and copies key. Embedded login may be blocked by identity providers. DPAPI is Windows-user scoped; non-Windows retains legacy storage. No runtime tests have been performed by the implementer.
