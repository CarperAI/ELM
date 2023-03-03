from dataclasses import dataclass, field
from typing import Any, Optional

from hydra.core.config_store import ConfigStore
from omegaconf import MISSING


@dataclass
class BaseConfig:
    pass


@dataclass
class ModelConfig(BaseConfig):
    fp16: bool = True
    cuda: bool = True
    gpus: int = 1
    seed: Optional[int] = None
    deterministic: bool = False
    top_p: float = 0.95
    temp: float = 0.85
    gen_max_len: int = 768
    batch_size: int = 32
    model_path: str = MISSING  # Can be HF model name or path to local model


@dataclass
class PromptModelConfig(ModelConfig):
    model_path: str = "Salesforce/codegen-350M-mono"


@dataclass
class DiffModelConfig(ModelConfig):
    model_path: str = "CarperAI/diff-codegen-350m-v2"


@dataclass
class QDConfig(BaseConfig):
    init_steps: int = 2
    total_steps: int = 5


@dataclass
class MAPElitesConfig(QDConfig):
    history_length: int = 1
    save_history: bool = False
    map_grid_size: tuple[int, ...] = field(default_factory=lambda: (12,))


@dataclass
class EnvConfig(BaseConfig):
    timeout: float = 5.0  # Seconds
    sandbox: bool = False
    sandbox_server: str = "http://localhost:5000"
    processes: int = 12
    batch_size: int = 32  # Batch size of MAP-Elites
    env_name: str = MISSING
    debug: bool = False


@dataclass
class SodaraceEnvConfig(EnvConfig):
    env_name: str = "sodarace"
    eval_ms: int = 1000  # Milliseconds
    behavior_space: list[list[float]] = field(
        default_factory=lambda: [
            # Height, Width, Mass dimensions
            [0, 1000],
            [0, 1000],
            [0, 2000],
        ]
    )
    starting_seeds: list[str] = field(default_factory=lambda: ["square"])
    instruction: int = 1
    crossover: bool = True


@dataclass
class ImageEnvConfig(EnvConfig):
    env_name: str = "image_evolution"


defaults = [
    {"model": "prompt"},
    {"qd": "mapelites"},
    {"env": "sodarace"},
    "_self_",
]


@dataclass
class ELMConfig(BaseConfig):
    hydra: Any = field(
        default_factory=lambda: {
            "run": {"dir": "logs/elm/${hydra.job.override_dirname}"}
        }
    )
    defaults: list[Any] = field(default_factory=lambda: defaults)
    model: Any = MISSING
    qd: Any = MISSING
    env: Any = MISSING
    run_name: Optional[str] = None


cs = ConfigStore.instance()
cs.store(group="env", name="sodarace", node=SodaraceEnvConfig)
cs.store(group="env", name="image_evolution", node=ImageEnvConfig)
cs.store(group="qd", name="mapelites", node=MAPElitesConfig)
cs.store(group="model", name="prompt", node=PromptModelConfig)
cs.store(group="model", name="diff", node=DiffModelConfig)
cs.store(name="elmconfig", node=ELMConfig)
