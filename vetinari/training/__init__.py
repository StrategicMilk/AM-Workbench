"""Training subsystem: agent fine-tuning, curriculum design, and data generation."""

from __future__ import annotations

from vetinari.training.adapter_registry import (
    list_adapters_by_task_type,
    list_deployed_adapters,
)
from vetinari.training.agent_trainer import AgentTrainer
from vetinari.training.continual_learning import (
    LoRAAdapterManager,
    ReplayBuffer,
    STABLERegularizer,
)
from vetinari.training.curriculum import (
    CurriculumPhase,
    TrainingActivity,
    TrainingActivityType,
    TrainingCurriculum,
)
from vetinari.training.data_provenance import (
    ContaminationStatus,
    DataProvenance,
    LicenseClass,
    RedactionStatus,
)
from vetinari.training.data_seeder import SeedDataset, TrainingDataSeeder
from vetinari.training.external_data import DatasetInfo, DatasetSpec, ExternalDataManager
from vetinari.training.idle_scheduler import IdleDetector, IdleTrainingJob, TrainingScheduler
from vetinari.training.ledger import (
    TrainingLedgerEntry,
    append_ledger_entry,
    audit_promotion,
    load_training_ledger,
)
from vetinari.training.pipeline import ContextDistillationDatasetBuilder, DistillationDatasetInfo
from vetinari.training.quality_gate import TrainingGateDecision, TrainingQualityGate
from vetinari.training.synthetic_data import (
    MagpieGenerator,
    StrategyDistiller,
    SyntheticDataGenerator,
    generate_reasoning_chains,
)
from vetinari.training.synthetic_generators import (
    store_distilled_strategies,
)
from vetinari.training.validation import PostTrainingValidator, PreTrainingValidator

__all__ = [
    "AgentTrainer",
    "ContaminationStatus",
    "ContextDistillationDatasetBuilder",
    "CurriculumPhase",
    "DataProvenance",
    "DatasetInfo",
    "DatasetSpec",
    "DistillationDatasetInfo",
    "ExternalDataManager",
    "IdleDetector",
    "IdleTrainingJob",
    "LicenseClass",
    "LoRAAdapterManager",
    "MagpieGenerator",
    "PostTrainingValidator",
    "PreTrainingValidator",
    "RedactionStatus",
    "ReplayBuffer",
    "STABLERegularizer",
    "SeedDataset",
    "StrategyDistiller",
    "SyntheticDataGenerator",
    "TrainingActivity",
    "TrainingActivityType",
    "TrainingCurriculum",
    "TrainingDataSeeder",
    "TrainingGateDecision",
    "TrainingLedgerEntry",
    "TrainingQualityGate",
    "TrainingScheduler",
    "append_ledger_entry",
    "audit_promotion",
    "generate_reasoning_chains",
    "list_adapters_by_task_type",
    "list_deployed_adapters",
    "load_training_ledger",
    "store_distilled_strategies",
]
