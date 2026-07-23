"""Workflow Builder package — typed pipeline graph CRUD for Vetinari.

Provides create / save / load / list / validate operations over pipeline
definitions stored as YAML under ``outputs/workflows/``.  This package is
the pipeline-definition backend that backs the visual Workflow Builder surface.
"""

from __future__ import annotations

from vetinari.workflows import builder

__all__ = ["builder"]
