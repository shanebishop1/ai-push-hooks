# orders-service

Serves order records over HTTP. Components read through the typed client in
`src/api/client.ts`; schema changes live in `migrations/`.

## Running the checks

```bash
python tests/check_rules.py
```
