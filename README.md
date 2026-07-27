### Access Hub

Central issuer for ephemeral Frappe site access.

Pairs with [Access Guard](https://github.com/Aakvatech-Limited/access-guard), the client-side
enforcement and logging app installed on each site being granted access.

Docs (this repo is authoritative for the shared set):
- [`docs/CONTEXT.md`](docs/CONTEXT.md) — the problem being solved.
- [`docs/DESIGN.md`](docs/DESIGN.md) — architecture and rationale.
- [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — the frozen wire contract.
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — install, enroll, use, test.

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/Aakvatech-Limited/access-hub --branch develop
bench install-app access_hub
```

Install auto-generates the signing keypair and seeds the default policy — see
[`docs/OPERATIONS.md`](docs/OPERATIONS.md) to enroll a client and start granting access.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/access_hub
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

mit
