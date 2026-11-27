# Paysera Skills

Official Paysera AI agent skills. This repository is a
[Claude Code plugin marketplace](https://code.claude.com/docs/en/plugin-marketplaces) —
a plain git repository with a `.claude-plugin/marketplace.json` catalogue at its root — so
it can be installed directly from a public network, with no Paysera account or VPN required
to read it.

The skills follow the open [Agent Skills](https://agentskills.io) format, so the same
`SKILL.md` packages are consumable by other agent runtimes as well.

## Install

Claude Code:

```bash
claude plugin marketplace add paysera/skills
claude plugin install paysera-payments@paysera-skills
claude plugin install paysera-checkout@paysera-skills
```

Codex (`paysera-checkout` only):

```bash
codex plugin marketplace add paysera/skills
```

Then install `paysera-checkout` from the `paysera-skills` marketplace in the Codex plugin
browser. Cursor reads the same repository through `.cursor-plugin/marketplace.json` and the
plugin's Agent Plugins manifest (`plugins/paysera-checkout/plugin.json`).

To update later:

```bash
claude plugin marketplace update paysera-skills
```

Then use it from Claude Code:

```
/paysera-payments pay the attached invoice from my company account
```

## Available skills

| Skill | What it does |
|-------|--------------|
| [`paysera-payments`](plugins/paysera-payments) | Creates **draft** Paysera transfers from an invoice or a plain-language request, and cancels/deletes unsigned drafts, via the public Transfer API. |
| [`paysera-checkout`](plugins/paysera-checkout) | Helps a coding agent build a merchant's **Paysera Checkout** integration: payments, verified webhooks, refunds, payment links and Test Mode, with PHP SDK and REST examples. |

## Before you use `paysera-payments`

The skill authenticates with a **Personal Access Token** you create yourself on your own
Paysera account, scoped to the accounts you choose. Setup steps are in
[the skill's SKILL.md](plugins/paysera-payments/skills/paysera-payments/SKILL.md).

Two properties are deliberate and worth stating up front:

- **It cannot move money.** The token is created without the `transfers:sign` scope, so
  everything the skill creates is an unsigned draft. Money moves only when you sign the
  transfer yourself in the Paysera app or web bank, under 2FA and your account's own limits.
- **It is dry-run by default.** The helper scripts print the payload they would send and
  do nothing until you pass `--confirm`.

Never grant an agent a token with `transfers:sign`.

## Before you use `paysera-checkout`

The skill only guides the code your agent writes. It holds no credentials, calls no API and
never asks for your `client_secret`. Your integration reads that secret from your server's
environment; the secret also signs Paysera's webhooks, so keep it out of code, repositories, logs and
the browser. Test with your Checkout project in Test Mode before you switch it to Live.

## Security

Skills published here never ship credentials. `paysera-payments` calls public Paysera APIs
from your own machine, using credentials you create on your own account. Its token lives only on your
machine (`~/.config/paysera-payments/token`, mode `0600`) and is sent only to Paysera API
hosts. That mode is enforced, not just documented: the scripts refuse to run if the token
file is readable by group or other.

To report a security issue with this repository, see [SECURITY.md](SECURITY.md).

## Contributing and publishing

This repository is public and is **not** a mirror of any internal repository. Skills are
added here deliberately, one at a time, after a publish-eligibility review. See
[CONTRIBUTING.md](CONTRIBUTING.md) for what may be published and how to release it.

## License

[MIT](LICENSE).
