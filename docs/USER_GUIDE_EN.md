# BlackFox AI Workstation — User Guide

BlackFox AI Workstation is a self-hosted web platform for operating local LLM inference servers from one hub. It can route prompts to OpenAI-compatible servers such as LM Studio, llama.cpp, Ollama, and vLLM. Optional node agents report telemetry and support approved management actions.

## Requirements

- Python 3.10 or newer on the hub computer.
- One or more inference servers with an API endpoint reachable from the hub.
- For remote telemetry, Python on the node computer and network access to the agent port (8766 by default).
- A trusted private network for connecting other devices.

Core modules use Python's standard library. Install pytest only if you want to run the test suite. Some hardware telemetry depends on the operating system and optional sensor tools.

## Start the hub

From the project directory, run:

```bash
python hub_server.py 8765
```

On the hub computer, open http://127.0.0.1:8765/. At first run, set a strong administrator password and, if needed, a separate user password. Initial setup is restricted to the hub computer. Keep authentication enabled.

The hub stores settings, chat threads, credentials, certificates, uploaded files, and security logs in the `data/` directory by default. Treat it as sensitive. You can select another location with the `BLACKFOX_DATA` environment variable.

## Connect a model server

The initial `node_1` and `node_2` endpoints (127.0.0.1:1234 and 127.0.0.1:1235) are examples. They do not start an inference server. Start your model server, then open **Settings → Nodes** in BlackFox, enter an address reachable from the hub, choose the model and role, and enable the node. Remember: 127.0.0.1 always means the computer making the connection.

## Add a remote agent

An administrator can generate a one-time installation command from a node card or **Settings → Nodes**. Review the command and run it on the intended computer. Treat the installation token as a secret; it expires and is intended for one use. Restrict the agent port in the computer's firewall to the private network. Never expose the hub or agent directly to the public internet.

For manual startup, run `python3 node_agent.py 8766` on the node computer. Use the hub's supported setup to register agents and protect command traffic; do not copy cluster secrets into public issues or shared logs.

## Chat modes

Create or select a conversation, choose a node or execution mode, select a role or skill if useful, enter a prompt, and send it. Available modes include:

- **Single node:** one selected server answers.
- **All nodes:** enabled servers answer in parallel.
- **Pipeline:** nodes process the request in configured sequence.
- **Hybrid:** sequential stages can contain parallel nodes.
- **Consensus:** nodes draft and discuss responses before a moderator returns a final answer.

Prompts and attachments are sent to the configured inference server. Use only servers you trust. Optional web-search skills may fetch third-party pages.

## Connect from another device

Use a private network and a trusted HTTPS certificate before connecting other computers or phones. Open the hub using the hostname covered by the certificate. Do not ignore browser certificate warnings. The responsive interface can be installed as a PWA; HTTPS without certificate warnings is required for installation and some browser features. Review the network binding and access settings before inviting users.

## Security and privacy

- Keep `data/` private and back it up only to a protected location.
- Do not commit runtime data, credentials, certificates, logs, private node details, or exported conversations.
- Reserve the administrator account for configuration; give regular users a separate user password.
- Treat one-time installation commands and tokens as secrets.
- Limit connections to trusted private-network devices and use HTTPS when connecting remotely.
- Check the logging and retention settings of every inference server that receives prompts.

For vulnerability reporting and further deployment advice, see [SECURITY.md](../SECURITY.md).

## Tests

Install pytest and run `python -m pytest` from the project directory. Some agent checks require Windows. Check the GitHub Actions result for the exact branch or commit you plan to use. This guide does not claim that any unrun test passed.

## Troubleshooting

**Hub page does not open:** confirm the hub process is running, use the configured port, and check local firewall rules.

**Model does not respond:** verify that the model server and API are running, the endpoint is reachable from the hub, and the model name matches the server.

**Agent is unavailable:** check that the agent is running, the hub can reach its configured port, and the node firewall permits that private-network connection. Generate a new token if the earlier one expired or was used.

**Certificate warning:** use the hostname covered by the certificate and install a certificate trusted by the client device.

**Forgot the administrator password:** on the hub computer, run `python hub_server.py --reset-auth`, then open the interface locally and set new passwords.

## Further documentation

The detailed platform and API manual is in Russian: [CLUSTER_MANUAL.md](../CLUSTER_MANUAL.md). See also [CONTRIBUTING.md](../CONTRIBUTING.md), [SECURITY.md](../SECURITY.md), and [LICENSE](../LICENSE).
