from pathlib import Path

import torch
from pydantic import BaseModel, ConfigDict
from torch._prims_common import DeviceLikeType


class HeatmapConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_dir: Path

    feature_dir: Path
    wsi_dir: Path
    checkpoint_path: Path

    slide_paths: list[Path] | None = None

    device: DeviceLikeType = "cuda" if torch.cuda.is_available() else "cpu"

    topk: int = 0
    bottomk: int = 0
