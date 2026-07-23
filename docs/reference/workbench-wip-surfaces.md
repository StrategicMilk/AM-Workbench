# Workbench Backend-Backed Surfaces

These Workbench surfaces are visible in the product shell and have backend
routes or native-kernel bridge authority. Treat this page as the status and
operator-semantics reference for the Workbench row in the README.

| Surface | Current state | Operator caveat |
|---|---|---|
| Method Library | Catalog and read paths are present. | Methods are reference material until a workflow explicitly wires a method into execution. |
| Adaptive Tuning | UI and approval handoff records exist. | Tuning changes require registered applicators and verification before they can affect runtime behavior. |
| Resource Cockpit | Resource views and policy handoff records exist. | Hardware snapshots can report unavailable providers; do not treat suggested policy changes as applied until the approval chain records the application. |
| Capability Packs | Discovery and action surfaces exist. | Enable, disable, uninstall, and smoke-test behavior must be verified through the backing API before claiming a pack changed runtime state. |
| Domain Kits | Support and evidence surfaces exist. | Domain-kit claims require existing evidence refs; missing refs are a product defect, not proof. |
| Workflow Builder | Graph preview and save surfaces exist. | Previewed workflows are not executable automations unless a backing execution route records the run. |
| Channels | Channel read/write surfaces exist. | Channels are coordination surfaces and do not by themselves prove delivery to an external provider. |
| Benchmark Importer | Import surface exists. | Imported benchmark data must pass schema and provenance validation before it can be used as release or model-quality evidence. |
| RAG Debugger and Ingestion | Admin-gated ingestion, replay, trace, experiment, and eval-promotion API routes exist. | Ingestion writes to the shared knowledge base; debugger verdicts expose faithfulness, answer-relevance, context-recall, and context-precision scores, but quality claims still require eval evidence. |
| Prompt Engineering | Native-kernel route surface exists, while Python `PromptMutator` and `PromptOptimizer` remain service-level helpers. | Treat the native route as a status/experiment surface until a route test proves it calls the Python mutation and optimization services; promotion to runtime prompts still requires the separate approval/promotion flow. |
| Conversation History | Export and search routes exist for project conversation logs. | Export and search responses redact sensitive values and should be treated as local/operator read surfaces. |
| MCP Transport | `/mcp/tools`, `/mcp/message`, and `/mcp/resources/stream` are mounted in the native Rust kernel API. | Tool availability depends on registered MCP server capabilities at runtime; resource streaming requires an initialized resource session. |
