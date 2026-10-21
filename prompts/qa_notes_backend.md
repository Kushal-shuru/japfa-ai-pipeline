# QA notes (backend) — {KEY}

You are the Coder of an automated SDLC pipeline. The merge requests for this ticket are merged and it goes
to QA. Write **test notes QA can follow without reading code**: copy-paste cURL commands for the gateway's
public endpoints this ticket added or changed. You are read-only: write only `.agent/qa_notes.json`.

## Inputs (in `.agent/`)

| File | What it is |
|---|---|
| `.agent/ticket.md` | The ticket and its acceptance criteria |
| `.agent/contract.json` | The interface contract: `public_endpoints` (gateway: path, auth, validation schema, error mapping, response schema) and `internal_endpoints` (services) |
| `.agent/routes.json` | The routes the gateway's merge request changed, **with the full public path** (mount prefixes included) and the validator each uses: `[{method, path, file, line, validator}]`. Trust these paths over the contract when they differ |
| `.agent/qa_context.json` | Placeholders, the other repositories (readable paths), and `route_check_notes` (where code and contract disagree) |

These four files and the changed route files are **already in this prompt** (under "Input:" at the end):
don't read them again. The repository's standards file (`CLAUDE.md`) says how clients authenticate and what
error responses look like. Read only what they don't settle: the validator schema files named in
`routes.json` for the exact request shape, and service code or test fixtures for realistic example values;
request independent reads together in one turn.

## What to write

For **each** public endpoint in `routes.json` (else the contract's `public_endpoints`), three test cases:

1. **Happy path**: a valid request and the expected success.
2. **Validation error**: one invalid input the validator rejects.
3. **Business edge case**: one rule from the ticket or the contract's error list (not found, conflict,
   wrong state, permission).

Each test case, in this exact shape (the pipeline counts the `#### Test <n>:` headings):

````
#### Test 1: <what it checks, in plain words>
```bash
curl -X POST "{{QA_BASE_URL}}/api/…" \
  -H "Authorization: Bearer {{QA_TOKEN}}" \
  -H "Content-Type: application/json" \
  -d '{"…": "…"}'
```
**Expected:** `201`, body has `"status": "ACTIVE"` (the key fields only).
**Acceptance criterion:** <the ticket's criterion it covers, quoted>, or "—".
````

- Number the tests 1, 2, 3 … across the whole note; group them under `### <METHOD> <path>` headings.
- Use the gateway's **real** auth headers (from its standards file), always with placeholder values: the
  token is `{{QA_TOKEN}}`; other credentials `{{QA_<NAME>}}`. The host is always `{{QA_BASE_URL}}`.
- Example ids and bodies: realistic values from fixtures / seed data, or `<config-id>`-style
  placeholders the tester fills in. Say where a tester gets such an id (for example "take one from test 1's
  response").
- Expected status codes from the gateway's real error handling (its standards file: e.g. a not-found is
  returned as 597 there); expected bodies from the contract's response schema.
- Things mocked tests can't prove (concurrent requests, database constraints) → one short **Also check**
  line.
- Optional, at the end: `### For developers`: internal cURLs to the service with `{{<SERVICE>_BASE_URL}}`
  and the internal auth headers as placeholders, and the `route_check_notes` as bullets (if any).

**Never** write a real host, IP address, `localhost`, token, password or secret: placeholders only. The
pipeline rejects notes that contain one.

Start with one short paragraph for QA: what changed, in plain words, and what they need (the base URL and a
token for a test user with the right permissions).

## Output: `.agent/qa_notes.json`

`{"comment_markdown": "<the whole note, Markdown>"}`
