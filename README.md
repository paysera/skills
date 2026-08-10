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
claude plugin install <skill>@paysera-skills
```

To update later:

```bash
claude plugin marketplace update paysera-skills
```

## Available skills

No skills are published yet — the catalogue is empty and the first skill is in review.
Each published skill will be listed here with its install command.

## Security

Skills published here call public Paysera APIs from your own machine, using credentials you
create on your own account. They never ship credentials, and a skill that can initiate a
payment is scoped so that it cannot sign or execute one — signing stays in the Paysera app,
under 2FA.

To report a security issue with this repository, see [SECURITY.md](SECURITY.md).

## Contributing and publishing

This repository is public and is **not** a mirror of any internal repository. Skills are
added here deliberately, one at a time, after a publish-eligibility review. See
[CONTRIBUTING.md](CONTRIBUTING.md) for what may be published and how to release it.

## License

[MIT](LICENSE).
