"""Compatibility imports for the pre-v0.3 single-file core.

New code should import from orchestrator.engine/models/project/state directly.
"""
from .engine import OrchestratorEngine as IssueOrchestrator
from .models import Settings, canonical_task_hash, parse_task
from .project import Registry, load_catalog
from .state import StateStore as State

__all__ = ["IssueOrchestrator", "Settings", "Registry", "State", "parse_task", "canonical_task_hash", "load_catalog"]
