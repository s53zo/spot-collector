# Repository Guidelines

## Project Structure & Module Organization
- `spot-collector.py`: Async Telnet relay handling upstream DX cluster links, downstream clients, and keepalive logic.
- `spot-collector.service`: Systemd unit pointing to the installed script under `/opt/spot-collector/`.
- `README.md`: Deployment instructions, argument reference, and example invocation.
Keep new Python modules alongside `spot-collector.py` unless they are reusable utilities, in which case group them under a new `spot_collector/` package directory.

## Build, Test, and Development Commands
- `python3 spot-collector.py --help`: Inspect CLI arguments locally.
- `PYTHONPYCACHEPREFIX=.pycache python3 -m py_compile spot-collector.py`: Quick syntax check without leaving stray cache files.
- `sudo systemctl restart spot-collector`: Refresh the production service after deploying changes (matches the bundled unit file).

## Coding Style & Naming Conventions
- Target Python 3.7+; follow PEP 8 with 4-space indentation.
- Prefer descriptive function names (`connect_to_server`, `send_status_to_clients`) and `snake_case` variables.
- Keep logging messages actionable; stick with `logging.debug` for traces and `logging.error` for failures.
- Comments should explain intent, not restate code; rely on docstrings for public helpers.

## Testing Guidelines
- There is no automated test suite yet. Before submitting changes, run the py_compile check above and exercise the main relay path against a staging DX server when possible.
- When adding tests, place them under `tests/` and name files `test_<feature>.py`; use `pytest` for consistency with typical Python async projects.
- Document any manual test steps in the pull request so operators can reproduce them.

## Commit & Pull Request Guidelines
- Use short, imperative commit messages (e.g., “Disable idle timeouts by default and add keepalive”).
- Each PR should summarize behavior changes, list test evidence, and link related issues or tickets.
- Include configuration updates (service files, deployment notes) in the same PR when they are required to activate the feature.

## Security & Configuration Tips
- Avoid hardcoding credentials or network endpoints; keep secrets in environment variables or systemd overrides.
- Validate new flags against real cluster hosts in a sandbox before rolling to production, and keep `--server-timeout` conservative to prevent reconnect storms.
