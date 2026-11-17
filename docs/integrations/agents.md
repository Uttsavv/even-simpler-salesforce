# Coding agents

The library ships agent skills and an `llms.txt` index, so coding agents such as Claude Code and
Codex know how to use it. A skill tells the agent which API to use for a task and which mistakes
to avoid.

```sh
esf skills install                       # Claude Code: writes .claude/skills/esf-*/
esf skills install --dir .agents/skills  # Codex and other Agent Skills clients
```

Commit the installed skills if your whole team uses the agent, or install them in your home
directory for yourself only.

## Skills

| Skill | Use it to |
|---|---|
| `esf-usage` | write application code: login flows, the client, records, SOQL with placeholders, Collections, Composite and Bulk, errors |
| `esf-probe` | inspect an org without changing it: triggers, Apex bodies, flows, validation rules, fields and records; ships a read-only script template |
| `esf-migrate` | move code from simple-salesforce with [`esf migrate`](../migrate.md) and resolve its markers |
| `esf-testing` | test code with the [fakes, the recorder and the pytest plugin](../testing.md) |
| `esf-debug` | diagnose failures: error classes and hints, `ESF_DEBUG`, events, usage and login problems |

Each skill is a directory with a `SKILL.md` file in the open
[Agent Skills](https://agentskills.io/specification) format. The `name` and `description` in its
header tell the agent when to load it. `esf-probe` also has `probe_template.py`.

## List and read the skills

```sh
esf skills list             # name and description of each skill
esf skills show esf-probe   # print one SKILL.md
```

## Install the skills

`esf skills install` copies each skill directory into a skills directory. Without names, it
installs every skill. It prints each file it writes.

```sh
esf skills install esf-usage esf-testing
esf skills install --dir ~/.claude/skills    # for you only, in every project
```

| Agent | Project directory | Personal directory |
|---|---|---|
| Claude Code | `.claude/skills` (the default) | `~/.claude/skills` |
| Codex, and other clients of the Agent Skills format | `.agents/skills` | `~/.agents/skills` |

If a skill directory already exists, the command stops and writes nothing. Add `--force` to
replace it. `--force` deletes the old directory first, so don't edit installed skills in place.

| Option | Default | Description |
|---|---|---|
| `NAME ...` | every skill | the skills to install |
| `--dir DIR` | `.claude/skills` | the skills directory |
| `--force` | off | replace skills that are already installed |

## Keep skills in step with the library

The skills ship inside the package, so `esf skills install` always writes the skills of the
installed version. They name no version themselves. After you upgrade the library, install
them again:

```sh
pip install -U even-simpler-salesforce
esf skills install --force
```

## llms.txt

[`llms.txt`](https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/llms.txt)
is an index of these docs in the [llms.txt](https://llmstxt.org/) format. It lists every page
with a one-line summary and a link to the page's Markdown source on GitHub. Give the link to an
agent that can fetch web pages, or paste the file into a chat. The docs site also serves
it at `/llms.txt`.

The links point to the `main` branch, which can be newer than your installed version. The
skills match your installed version, so prefer them when the two differ.

## How it works

`esf skills` reads the files from the installed package with `importlib.resources`, so it
needs no network and no login. `docs/llms.txt` is generated from the docs navigation, the
first paragraph of each page and the README by `scripts/render_llms_txt.py`. A test fails if
the file is out of date.
