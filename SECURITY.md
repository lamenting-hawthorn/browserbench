# Security policy

## Scope

BrowserTransactionBench uses a local synthetic fixture and should not be pointed
at real accounts, recipients, payments, inventory, or production systems. It is
research software, not a production-safety certification.

Security-relevant areas include capability-token isolation, origin restrictions,
trace and receipt redaction, path handling, temporary-resource ownership,
dependency integrity, and any route that could expose the authoritative oracle
to an acting agent.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability that could expose
credentials, escape the local fixture boundary, access the oracle, or cause an
unintended external effect. Use GitHub's private vulnerability reporting flow:

<https://github.com/lamenting-hawthorn/browserbench/security/advisories/new>

Include the affected commit, a minimal reproduction, expected impact, and any
suggested containment. Do not include real secrets or personal data.

## Supported version

Only the current default branch is actively reviewed. There is no production
deployment or long-term support promise for the exploratory `0.1.0` line.
