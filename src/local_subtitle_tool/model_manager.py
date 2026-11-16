from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class ModelSpec:
    name: str
    repo_id: str
    display_name: str
    estimated_download_bytes: int
    required_files: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class ModelCacheStatus:
    spec: ModelSpec
    cache_dir: Path
    snapshot_dir: Path | None
    ready: bool
    downloaded_bytes: int
    missing_files: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["cache_dir"] = str(self.cache_dir)
        data["snapshot_dir"] = str(self.snapshot_dir) if self.snapshot_dir else None
        return data


MODEL_SPECS = {
    "large-v3": ModelSpec(
        name="large-v3",
        repo_id="Systran/faster-whisper-large-v3",
        display_name="faster-whisper large-v3",
        estimated_download_bytes=3_100_000_000,
        required_files=(
            "config.json",
            "model.bin",
            "preprocessor_config.json",
            "tokenizer.json",
            "vocabulary.json",
        ),
    )
}


def model_spec(name: str) -> ModelSpec:
    try:
        return MODEL_SPECS[name]
    except KeyError as exc:
        raise ValueError(f"지원하지 않는 전사 모델입니다: {name}") from exc


def default_model_cache_dir() -> Path:
    explicit = os.environ.get("HF_HUB_CACHE")
    if explicit:
        return Path(explicit).expanduser().resolve()
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return (Path(hf_home).expanduser() / "hub").resolve()
    try:
        from huggingface_hub import constants

        return Path(constants.HF_HUB_CACHE).expanduser().resolve()
    except ImportError:
        return (Path.home() / ".cache" / "huggingface" / "hub").resolve()


def _repo_cache_dir(spec: ModelSpec, cache_dir: Path) -> Path:
    repo_folder = f"models--{spec.repo_id.replace('/', '--')}"
    return cache_dir / repo_folder


def _snapshot_size(snapshot: Path) -> int:
    total = 0
    for item in snapshot.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def inspect_model_cache(
    name: str = "large-v3", *, cache_dir: Path | None = None
) -> ModelCacheStatus:
    spec = model_spec(name)
    resolved_cache = (cache_dir or default_model_cache_dir()).expanduser().resolve()
    snapshots_root = _repo_cache_dir(spec, resolved_cache) / "snapshots"
    candidates: list[Path] = []
    if snapshots_root.is_dir():
        candidates = [item for item in snapshots_root.iterdir() if item.is_dir()]
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)

    selected: Path | None = None
    missing_files = spec.required_files
    downloaded_bytes = 0
    for candidate in candidates:
        missing = tuple(
            filename
            for filename in spec.required_files
            if not (candidate / filename).is_file() or (candidate / filename).stat().st_size == 0
        )
        size = _snapshot_size(candidate)
        if selected is None:
            selected = candidate
            missing_files = missing
            downloaded_bytes = size
        if not missing:
            selected = candidate
            missing_files = ()
            downloaded_bytes = size
            break

    return ModelCacheStatus(
        spec=spec,
        cache_dir=resolved_cache,
        snapshot_dir=selected,
        ready=selected is not None and not missing_files,
        downloaded_bytes=downloaded_bytes,
        missing_files=missing_files,
    )


def download_model(
    name: str = "large-v3",
    *,
    cache_dir: Path | None = None,
    confirmed: bool = False,
) -> ModelCacheStatus:
    if not confirmed:
        raise PermissionError("모델 용량과 저장 위치를 확인한 뒤 다운로드를 승인해야 합니다.")
    spec = model_spec(name)
    resolved_cache = (cache_dir or default_model_cache_dir()).expanduser().resolve()
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError("huggingface-hub가 설치되어 있지 않습니다.") from exc
    snapshot_download(
        repo_id=spec.repo_id,
        cache_dir=resolved_cache,
        local_files_only=False,
    )
    status = inspect_model_cache(name, cache_dir=resolved_cache)
    if not status.ready:
        missing = ", ".join(status.missing_files)
        raise RuntimeError(f"모델 다운로드 후 필수 파일을 찾을 수 없습니다: {missing}")
    return status
