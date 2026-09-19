# AGENTS.md

House rules for this repository. Follow them in every change.

## 1. Use the typed API client

All network access goes through `src/api/client.ts`. Components must never call
`fetch()` directly: the client owns auth headers, retries, and error mapping, and
bypassing it produces requests that silently skip all three.

## 2. Migrations must stay backward-compatible

We deploy by rolling restart, so the previous release runs against the new schema
for several minutes. A migration must never drop or rename a column or table that
the current release still reads. Add the new shape, backfill it, and remove the
old one in a later release.

## 3. No marketing filler in documentation

Documentation states what the code does and what it requires. Do not add
superlatives or vague value claims ("blazing fast", "seamless", "world-class",
"enterprise-grade"). If a claim is not checkable against the code, cut it.
