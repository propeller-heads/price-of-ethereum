# Security Policy

## Reporting a Vulnerability

Please report security issues privately rather than opening a public GitHub
issue.

Use GitHub's private vulnerability reporting:
[report a vulnerability](https://github.com/propeller-heads/price-of-ethereum/security/advisories/new).
It goes to the maintainers without disclosing the issue publicly, and needs no
address to be kept monitored separately.

Include a description of the issue, steps to reproduce, and the affected
version. We'll acknowledge receipt and follow up with a timeline for a fix.

## Scope

This package is a client library and CLI: it talks to a Fynd instance you run
yourself and to the hosted Tycho API for token metadata. It holds no private
keys and executes no on-chain transactions. Reports involving the Fynd or
Tycho services themselves should go to their respective maintainers.
