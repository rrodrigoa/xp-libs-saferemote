# GitHub and PyPI setup

Protect main and require reviews with CODEOWNERS. Require CI release-safety,
Security dependency-audit and CodeQL, Docs docs-skill, and Docker E2E
docker-e2e. Configure a tag
ruleset that restricts creation and movement of version tags to maintainers.

Create environments named `testpypi` and `pypi`, restricting both to protected
release tags. Configure a production required reviewer and prevent self-review.
Register the exact owner, repository, `release.yml` and corresponding environment
with the TestPyPI and PyPI Trusted Publishing settings.

Set the repository variable `RIR_RELEASE_ENABLED` only after reviewing the
security gates and confirming the required GitHub protections. A protected
tag must match the package version and descend from main. The workflow verifies
the same two distribution files after each publication before creating the
GitHub release.

[PyPI Trusted Publishing setup](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
