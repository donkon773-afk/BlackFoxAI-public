# BlackFox AI Workstation

**BlackFox AI Workstation** is a self-hosted web platform for operating a cluster of local LLM inference servers. One hub connects OpenAI-compatible servers such as LM Studio, llama.cpp, Ollama, and vLLM through a shared web interface.

The project is under development. Review the security guidance and validate it in an isolated environment before connecting production nodes or sensitive data. The current supported product is the web platform and PWA; a native desktop client is not part of this version.

## Features

- Route chats to one node, all nodes, a pipeline, a hybrid plan, or a consensus workflow.
- Manage models, prompt roles, generation settings, and conversation threads.
- Collect node telemetry through an optional agent.
- Use a responsive interface that can be installed as a PWA.
- Configure password authentication, user and administrator roles, signed node commands, one-time installation tokens, network restrictions, and TLS.

## Quick start

Requirements: Python 3.10 or newer. Core modules use the Python standard library.

```bash
python hub_server.py 8765
```

On the hub computer, open http://127.0.0.1:8765/ and set an administrator password. Add a separate user password if needed. Then go to **Settings → Nodes**, replace the example endpoints with your inference server addresses, and select models. The default nodes at 127.0.0.1:1234 and 127.0.0.1:1235 are placeholders; they do not launch model servers.

To collect telemetry from another computer, an administrator generates a one-time agent installer command in the hub interface. Run it on the intended machine and keep the agent reachable only over a trusted private network. Do not expose the hub or agent directly to the internet.

## Documentation

- [User guide](docs/USER_GUIDE_EN.md) — setup, nodes, chat, security, and troubleshooting.
- [Russian platform and API manual](CLUSTER_MANUAL.md).
- [Security policy](SECURITY.md).
- [MIT License](LICENSE).

## Tests

Install pytest and run from the repository root:

```bash
python -m pip install pytest
python -m pytest
```

Some agent checks are Windows-specific. The presence of tests does not prove that a revision passed. Run them against the exact commit you intend to use. CI is not yet configured for this clean copy.

## Data and privacy

The hub stores settings, conversations, credentials, certificates, uploaded files, and logs in data/, which is excluded from Git by default. Protect that directory and its backups. Prompts and attachments are sent to the selected inference server; use only servers you trust. Optional web search may retrieve third-party pages.

Use a private network, keep password authentication enabled, and configure HTTPS with a trusted certificate when connecting from other devices. Review network restrictions before installing agents.

## License

Distributed under the MIT License. See [LICENSE](LICENSE).
