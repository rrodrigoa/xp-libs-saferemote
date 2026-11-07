# Contributing

Use a branch from main and open a focused pull request. Run the lint, format,
strict typing and deterministic test commands in the README. For changes at a
trust boundary, show that rejected requests produce zero action effects. Update
the generated workflow schema and error code index with
`python scripts/generate_contracts.py`.

Record changes under Unreleased in the changelog. Prefer commit prefixes
`feat`, `fix`, `docs`, `test` or `ci`.
