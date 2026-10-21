# GENBA PHOTO ChatGPT bridge (prototype)

## Status
This branch adds an authenticated multipart file-ingestion endpoint to the existing Flask app.
It does **not** subscribe to ChatGPT Project attachment events and does **not** make Android ChatGPT attachments automatically sync.

## Configuration
Set a long, randomly generated secret in the server environment as `GENBA_BRIDGE_TOKEN`.
If unset, all requests to the bridge endpoint are rejected.
Do not place the token in the mobile app, browser code, Git, or a publicly readable file.

## API
`POST /api/chatgpt/ingest/<site_token>`
- Header: `Authorization: Bearer <GENBA_BRIDGE_TOKEN>`
- Multipart field: `files` (repeatable)
- Response: `uploaded` count and per-file status `uploaded`, `duplicate`, `unsupported`, or `too_large`.
- File size limit: 50 MiB per file.
- SHA-256 per-site duplicate detection backed by SQLite.
- Existing GENBA PHOTO site tokens are used; no new site is created.

## Verification
Run `python test_chatgpt_bridge.py` from the repository directory.

## Remaining before production
- Decide how ChatGPT legitimately supplies attachment bytes and user authorization to the bridge.
- Build and test the ChatGPT-facing MCP/App layer.
- Review API authentication, rate limits, file validation, database backup and logging.
- Deploy only after backup and staging verification.
- Test actual Android ChatGPT -> GENBA PHOTO end-to-end.

## MCP adapter prototype
chatgpt_mcp.py provides an opt-in MCP tool that accepts an explicitly supplied HTTPS file URL and filename, checks an allowlist (GENBA_SOURCE_HOSTS), and calls the authenticated ingest API. It is not yet deployed or connected to ChatGPT. It does not receive automatic Project attachment events.
