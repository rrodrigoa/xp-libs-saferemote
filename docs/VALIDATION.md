# Local validation

This report covers the staged SafeRemote 0.1.0 implementation. The staged
changes include the SDK and MCP adapter under `src/`, the test suites,
example consumer actions, generated contracts, release scripts, packaging
metadata, GitHub workflows and contributor documentation.

## Tests and checks

| Check | Result |
| --- | --- |
| Windows, Python 3.11.9 | 102 passed |
| Linux container, Python 3.12.16 | 102 passed |
| Per-run test counts | 52 contract, 17 property, 16 security, 15 MCP agent simulation, 2 recovery |
| Skips and expected failures | 0 skips, 0 xfails in each deterministic run |
| Ruff lint and format, strict mypy, pip check | Passed |
| Generated workflow schema and error index | Passed |
| Example signed artifact pipeline | Passed |
| Bandit on SDK and evaluator source | Zero findings, zero suppressed findings |
| Dependency audit of pinned versions | No known vulnerabilities, CycloneDX SBOM generated |
| Workflow static checks | All six parse, action references use full SHAs, OIDC write access confined to the two publisher jobs |
| Distribution builds, Twine and clean installs | Passed for wheel and sdist. Installed wheel MCP skill retrieval and catalog discovery passed. |
| Catalog and skill byte parity | Passed against source for both distributions |

Admission rejection tests observed zero action invocations and zero accepted
execution rows. MCP denial, missing approval, argument spoofing and discovery
rejections also observed zero action invocations. The runtime denies
privileged and mutating registrations before invocation. No privileged
broker was exercised.

## Coverage

| SDK area | Line | Branch |
| --- | ---: | ---: |
| Global | 98.46% | 95.96% |
| Action registry | 100% | 100% |
| Signed catalog | 100% | 100% |
| Compiler | 98.40% | 94.79% |
| Controller | 100% | 100% |
| Journal | 100% | 100% |
| YAML parser | 98.61% | 97.83% |
| Policy and approvals | 100% | 100% |
| Endpoint runtime | 95.92% | 95.00% |
| Signed plan verification | 100% | 100% |

The staged diff covered 1,149 of 1,167 changed executable lines (98.46%).
All measured coverage gates passed.

## Local artifacts

| File | SHA-256 |
| --- | --- |
| xp_libs_saferemote-0.1.0-py3-none-any.whl | `f468dc33683ee04c9aa49061eb2a205b932b4f306c0213cc1d908f2a147bb946` |
| xp_libs_saferemote-0.1.0.tar.gz | `e8c4669b906e3017be419f11438a04a2d8564d33b9d6134e79ff792ce0b203e7` |

## Release status

Live model evaluation, the full hosted compatibility matrix, CodeQL and
other hosted scans, mutation testing, a privileged broker review, TestPyPI
and PyPI publication and index verification were **NOT RUN**. No model
configuration was supplied at runtime.

Remaining implementation includes production worker isolation, a reviewed
privilege broker and mutation reconciliation, durable controller submissions,
automatic trusted-pack onboarding, artifact delivery and export, and complete
live-model scenario scoring. See the [endpoint isolation ADR](adr/0001-endpoint-isolation.md).
GitHub protection rules, publication environments and the PyPI trusted
publisher registrations require [maintainer setup](GITHUB_SETUP.md).
