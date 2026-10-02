# Security policy

## Supported versions

BlackFox AI Workstation is under active development and does not yet publish a supported-version matrix. Check the repository for the latest maintainer-reviewed source.

## Reporting a vulnerability

Do not post exploit details, credentials, private node addresses, chat data, or security logs in a public issue. Use GitHub's **Report a vulnerability** option on the Security tab if it is enabled. Otherwise, open a minimal issue asking the maintainer for a private reporting channel without including vulnerability details.

Include the affected commit, prerequisites, impact, and a safe reproduction description. Remove secrets and personal data. Maintainers can coordinate validation and remediation before public disclosure.

## Deployment guidance

- Keep the hub and agents on a trusted private network; do not expose them directly to the public internet.
- Complete first-run password setup on the hub computer and keep authentication enabled.
- Use HTTPS with a certificate trusted by client devices.
- Restrict the listening interface and firewall rules to intended devices.
- Treat data/ as sensitive: it may contain credentials, sessions, certificates, chat history, uploads, and security logs.
- Protect and rotate node credentials if a hub or agent computer is compromised.
- Review one-time agent installation commands before running them; tokens are secrets.

These recommendations are not a security audit and do not guarantee that every deployment is secure.
