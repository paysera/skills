# Paysera Skills

Official Paysera AI agent skills. This repository is a
[Claude Code plugin marketplace](https://code.claude.com/docs/en/plugin-marketplaces) —
a plain git repository with a `.claude-plugin/marketplace.json` catalogue at its root — so
it can be installed directly from a public network, with no Paysera account or VPN required
to read it.

The skills follow the open [Agent Skills](https://agentskills.io) format, so the same
`SKILL.md` packages are consumable by other agent runtimes as well.

## Install

```bash
claude plugin marketplace add paysera/skills
claude plugin install paysera-payments@paysera-skills
```

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

## Security

Skills published here call public Paysera APIs from your own machine, using credentials you
create on your own account. They never ship credentials. The token lives only on your
machine (`~/.config/paysera-payments/token`, mode `0600`) and is sent only to Paysera API
hosts.

To report a security issue with this repository, see [SECURITY.md](SECURITY.md).

## Contributing and publishing

This repository is public and is **not** a mirror of any internal repository. Skills are
added here deliberately, one at a time, after a publish-eligibility review. See
[CONTRIBUTING.md](CONTRIBUTING.md) for what may be published and how to release it.

## License

[MIT](LICENSE).
