# GitHub and PyPI setup

Protect main and require reviews with CODEOWNERS. Require CI release-safety,
Security dependency-audit and CodeQL, Docs docs-skill, and Docker E2E
docker-e2e for merges.

Enable GitHub Actions and create environments named `testpypi` and `pypi`.
If you configure deployment restrictions, allow version tags matching `v*`.
Register a Trusted Publisher on each package index with the following settings.

| Index | GitHub repository | Workflow filename | Environment |
| --- | --- | --- | --- |
| TestPyPI | Owner and name of the publishing repository | `release.yml` | `testpypi` |
| PyPI | Owner and name of the publishing repository | `release.yml` | `pypi` |

Use the repository that runs the release workflow. Trusted Publishing
matches that repository, workflow filename and environment exactly.

Push a version tag matching `pyproject.toml` on a commit reachable from
`origin/main`. For version `0.1.0`, use `v0.1.0`. The release workflow runs
its build, test, coverage and security checks, then publishes the wheel and
sdist to TestPyPI through GitHub OIDC. After verifying the uploaded bytes and
installation, it publishes the same artifacts to PyPI. Production verification
must succeed before the workflow creates the GitHub Release.

Production publishing proceeds automatically after TestPyPI verification
when the `pypi` environment has no approval requirement. Administrators can
optionally require reviewers through GitHub environment settings.

[PyPI Trusted Publishing setup](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
