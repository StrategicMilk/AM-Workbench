# AI Security Baseline

Last verified: 2026-05-21.

Vetinari security audits and AI workflow reviews use these current primary
sources for LLM and agentic application risk mapping:

| Source | Current baseline | Use in Vetinari reviews |
|---|---|---|
| OWASP Top 10 for LLM Applications 2025 | OWASP GenAI Security Project resource page, dated 2024-11-17 for the English 2025 edition. | Baseline for LLM app risks such as prompt injection, excessive agency, supply chain, sensitive information disclosure, and model theft. |
| OWASP Top 10 for Agentic Applications | OWASP GenAI Security Project release dated 2025-12-09. | Baseline for agent-specific risks such as agent goal hijack, tool misuse, identity and privilege abuse, agentic supply chain vulnerabilities, unexpected code execution, memory/context poisoning, insecure inter-agent communication, cascading failures, human-agent trust exploitation, and rogue agents. |
| OWASP AI Agent Security Cheat Sheet | OWASP Cheat Sheet Series. | Implementation guidance for least-privilege tools, human approval boundaries, memory/context controls, output validation, monitoring, and multi-agent safeguards. |

Older ASI/AGEN shorthand in historical planning files is retained as archival
evidence only. New security findings and release gates should cite the current
OWASP source names above and map controls to concrete Vetinari routes, tools,
agents, prompts, memory paths, or release artifacts.
