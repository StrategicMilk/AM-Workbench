# Workbench Update Manifests

This directory contains static update-manifest authoring examples for the Workbench update safety gate. The runtime gate reads release metadata, public export provenance, SHA-256 artifact digests, and signature evidence, then returns readiness only. It does not install, roll back, publish, or mutate the public export builder.

Stable manifests must include signature evidence and pass the injected signature verifier before they can be marked ready. Beta manifests may omit signature evidence, but checksum and artifact evidence still fail closed.
