# Community

AM Workbench is a local-first GenAI workbench. This page collects the places where
users ask questions, share workflows, and contribute.

## Where to get help

- **GitHub Discussions** — ask questions, share ideas, and read answers from
  the community:
  <https://github.com/StrategicMilk/AM-Workbench/discussions>
- **Issue tracker** — bug reports and feature requests:
  <https://github.com/StrategicMilk/AM-Workbench/issues>

You can also run `python -m vetinari community` to print the full list of
resource URLs in your terminal.

## Showcase

Built something interesting with Vetinari? Share it using the **Showcase**
discussion template:

1. Go to <https://github.com/StrategicMilk/AM-Workbench/discussions/new?category=showcase>
2. Write one paragraph describing what you built (agent pipeline, training run,
   RAG workflow, etc.).
3. Include a screenshot, asciinema recording, or a gist link so others can
   reproduce it.

## Contributing

Contributions are welcome. Read the contributing guide before opening a pull
request:

[CONTRIBUTING.md](../../CONTRIBUTING.md)

The guide covers the development setup, test conventions, commit-message format,
and the Definition of Done checklist every PR must satisfy.

## Roadmap

Private development ledgers describe in-flight work and wave status. The
public-facing known-limitations page is at
[`docs/status/known-limitations.md`](../status/known-limitations.md).

There is no hosted public roadmap at this time. Feature requests go in GitHub
Discussions or the issue tracker where they can be discussed before work begins.

## Internal Evidence Notes

- `wave2-direct-surface-followup-01-P04` cites this page as documentation evidence for `FSA-1743` without changing community process. The closure proof is executable: `tests/test_capability_parity_matrix.py` checks the pack closure rows against the generated evidence contract and the generated validation command must pass.

## Code of Conduct

This project follows a straightforward standard: be respectful, assume good
intent, and focus on the work. Harassment, personal attacks, and off-topic
noise will be removed. Reports go to the repository maintainer via a private
GitHub issue or by email.
