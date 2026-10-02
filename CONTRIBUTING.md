# Contributing

Thank you for your interest in BlackFox AI Workstation. The project is under active development, so larger changes are best discussed in an issue before implementation.

## Development setup

1. Use Python 3.10 or newer.
2. Install the test dependency: `python -m pip install pytest`.
3. Start a local hub with `python hub_server.py 8765`.
4. Keep runtime state in an ignored `data/` directory. For isolated work, set `BLACKFOX_DATA` to a separate temporary directory.

## Before submitting a change

- Keep the core (`hub_server.py`, `node_agent.py`, `security_store.py`, and `blackfox_core.py`) on the Python standard library.
- Do not commit secrets, certificates, runtime data, personal node details, chat exports, or logs.
- Preserve authentication, signed node commands, one-time installation tokens, TLS support, and network restrictions.
- Run `python -m pytest` where the environment supports it and report the exact command and result. Do not describe unrun checks as passing.
- For hub changes, validate against an isolated copy with a separate port and data directory; do not use a live hub as a test target.
- Keep user-facing documentation in sync with behavior and clearly label platform-specific requirements.

## Pull requests

Describe the user problem, the change, relevant security implications, and how it was checked. Include screenshots only when they help explain a user-interface change, and ensure they contain no private data.
