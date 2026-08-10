"""seqcluster collapse wrapper for deduplicating trimmed small-RNA FASTQ files."""

from __future__ import annotations

import gzip
import os
import shutil
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
from anndata import AnnData

from ..._registry import register_function
from ..._utils import run_cli_cmd, run_threads


def _collapse_output_name(input_path: Path) -> str:
    """Match seqcluster's ``<input-stem>_trimmed.fastq`` output convention."""
    name = input_path.name
    if name.endswith(".gz"):
        name = name[:-3]
    stem = Path(name).stem
    return f"{stem}_trimmed.fastq"


def _compress_fastq(raw_path: Path) -> Path:
    compressed = raw_path.with_suffix(f"{raw_path.suffix}.gz")
    temporary = compressed.with_suffix(f"{compressed.suffix}.tmp")
    try:
        with raw_path.open("rb") as source, gzip.open(temporary, "wb") as destination:
            shutil.copyfileobj(source, destination)
        os.replace(temporary, compressed)
    finally:
        if temporary.exists():
            temporary.unlink()
    raw_path.unlink()
    return compressed


@register_function(
    aliases=[
        "seqcluster_collapse",
        "collapse_fastq",
        "collapse_sequences",
        "fastq_deduplicate",
        "序列去重合并",
    ],
    category="fastq",
    description=(
        "Collapse identical sequences in trimmed FASTQ files with `seqcluster collapse`. "
        "Reads per-sample paths from `adata.obs['trimmed_path']` (or `clean_fastq_path`), "
        "runs one isolated seqcluster job per sample, gzip-compresses the collapsed FASTQ, "
        "removes seqcluster's transient log directory, and writes the result to "
        "`adata.obs['collapsed_path']`. Sequence abundance is encoded by seqcluster in FASTQ headers."
    ),
    examples=[
        'adata = sa.fastq.seqcluster_collapse(adata, output_dir="collapsed_out", jobs=4)',
    ],
    related=["fastq.cutadapt", "alignment.bowtie"],
    produces={"obs": ["collapsed_path", "seqcluster_output_dir"]},
)
def seqcluster_collapse(
    adata: AnnData,
    output_dir: str = "collapsed_out",
    *,
    input_col: str = "trimmed_path",
    minimum: int = 1,
    min_size: int = 16,
    jobs: Optional[int] = None,
    overwrite: bool = False,
) -> AnnData:
    """Collapse duplicate sequences from each sample's trimmed FASTQ.

    Every sample receives its own output subdirectory so multi-sample jobs do
    not collide. seqcluster writes ``<input-stem>_trimmed.fastq``; this wrapper
    compresses it to ``.fastq.gz`` and removes the transient ``log/`` directory.
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")
    if minimum < 1:
        raise ValueError("minimum must be at least 1")
    if min_size < 0:
        raise ValueError("min_size must be non-negative")

    if input_col not in adata.obs.columns:
        if input_col == "trimmed_path" and "clean_fastq_path" in adata.obs.columns:
            input_col = "clean_fastq_path"
        else:
            raise KeyError(
                f"adata.obs must contain {input_col!r}; run adapter trimming first."
            )

    seqcluster = shutil.which("seqcluster")
    if not seqcluster:
        raise FileNotFoundError("seqcluster not found in PATH")

    out_root = Path(output_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    inputs: Dict[str, Path] = {}
    for sample in adata.obs_names:
        value = adata.obs.loc[sample, input_col]
        if pd.isna(value) or not str(value).strip():
            raise ValueError(f"Missing {input_col!r} for sample {sample!r}")
        path = Path(str(value)).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Trimmed FASTQ not found for {sample}: {path}")
        inputs[str(sample)] = path

    def _collapse_one(sample: str) -> dict[str, str]:
        input_path = inputs[sample]
        sample_dir = out_root / sample
        sample_dir.mkdir(parents=True, exist_ok=True)
        raw_output = sample_dir / _collapse_output_name(input_path)
        compressed_output = raw_output.with_suffix(f"{raw_output.suffix}.gz")
        log_dir = sample_dir / "log"

        if compressed_output.is_file() and compressed_output.stat().st_size > 0 and not overwrite:
            shutil.rmtree(log_dir, ignore_errors=True)
            return {
                "sample": sample,
                "input": str(input_path),
                "output": str(compressed_output),
                "output_dir": str(sample_dir),
            }

        command = [
            seqcluster,
            "collapse",
            "-f",
            str(input_path),
            "-o",
            str(sample_dir),
            "-m",
            str(minimum),
            "--min_size",
            str(min_size),
        ]
        run_cli_cmd(command)
        if not raw_output.is_file() or raw_output.stat().st_size == 0:
            raise FileNotFoundError(
                f"seqcluster did not produce the expected FASTQ for {sample}: {raw_output}"
            )
        output = _compress_fastq(raw_output)
        shutil.rmtree(log_dir, ignore_errors=True)
        return {
            "sample": sample,
            "input": str(input_path),
            "output": str(output),
            "output_dir": str(sample_dir),
        }

    results = run_threads(list(inputs), _collapse_one, jobs)
    result_by_sample = {result["sample"]: result for result in results}
    adata.obs["collapsed_path"] = pd.Series(
        {sample: result_by_sample[sample]["output"] for sample in adata.obs_names},
        index=adata.obs_names,
        dtype="object",
    )
    adata.obs["seqcluster_output_dir"] = pd.Series(
        {sample: result_by_sample[sample]["output_dir"] for sample in adata.obs_names},
        index=adata.obs_names,
        dtype="object",
    )
    adata.uns["seqcluster_collapse"] = {
        "input_col": input_col,
        "output_dir": str(out_root),
        "minimum": minimum,
        "min_size": min_size,
        "jobs": jobs,
        "outputs": {sample: result_by_sample[sample]["output"] for sample in adata.obs_names},
    }
    return adata
