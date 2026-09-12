# Codex Notify invariants

- Keep application data in validated, atomic `settings.json` and `state.json`; do not add a database.
- Never call model generation, threads, turns, shell tools, or reset-credit consumption. The Codex
  adapter allowlist is a security boundary and requires a regression test.
- Use only the official Codex managed ChatGPT login. Never implement cookie scraping or custom OAuth.
- Every Telegram message and callback path must require the configured numeric owner ID and a private chat.
- Preserve `/app/data` and `/app/codex-home` across image replacement. Never restore an older `auth.json`.
- Store notifications before delivery. Treat successful Telegram acknowledgement as at-least-once with
  practical deduplication, not a mathematical exactly-once guarantee.
- Keep event detection network-independent and test baseline, ambiguity, account changes, nulls, and
  incomplete credit details.
- Changes to installation, state schema, healthcheck, CI promotion, or rollback require deployment tests.
- Keep Codex pinned to the stable version documented in `docs/architecture.md` and verify the official
  release checksum during image build.
- Keep English as the default and source language, with Russian in the centralized translation
  catalog. Domain events and the outbox must remain language-neutral and structured.
- Keep installer output, administrator logs, code comments, and primary documentation in English.
  Unauthorized Telegram traffic must be rejected silently before handlers and external calls.
