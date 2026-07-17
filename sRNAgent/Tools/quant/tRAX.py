"""tRAX wrapper for tRNA-derived fragment quantification."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd
from anndata import AnnData

from ..._registry import register_function
from ..._utils import run_cli_cmd
from ._matrix import store_count_matrix


FASTQ_SUFFIXES = (".fastq.gz", ".fq.gz", ".fastq", ".fq")
TRAX_FRAGMENT_SUFFIXES = ("wholecounts", "fiveprime", "threeprime", "other")


def _trax_script() -> Path:
    return Path(__file__).resolve().parent / "external" / "tRAX" / "processsamples.py"


def _strip_fastq_suffix(path: Path) -> str:
    name = path.name
    for suffix in FASTQ_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _sanitize_sample_name(name: str) -> str:
    sample = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_")
    if not sample:
        raise ValueError(f"Cannot derive a valid sample name from {name!r}")
    if sample[0].isdigit():
        sample = f"S{sample}"
    return sample


def _is_fastq(path: Path) -> bool:
    return path.is_file() and any(path.name.endswith(suffix) for suffix in FASTQ_SUFFIXES)


def _find_fastqs(fastq_dir: str | Path) -> List[Path]:
    directory = Path(fastq_dir).expanduser()
    if not directory.is_dir():
        raise FileNotFoundError(f"FASTQ directory not found: {directory}")
    fastqs = sorted(path.resolve() for path in directory.iterdir() if _is_fastq(path))
    if not fastqs:
        raise FileNotFoundError(f"No FASTQ files found in: {directory}")
    return fastqs


def _nonempty(value: object) -> bool:
    return value is not None and str(value).strip() not in {"", "nan", "None"}


def _prepare_trax_fastqs(
    adata: AnnData,
    *,
    fastq_dir: Optional[str | Path],
    path_col: Optional[str],
) -> AnnData:
    """Create ``adata.obs['trax_fq']`` without changing source FASTQ columns."""
    fastq_by_sample: Dict[str, str] = {}
    if fastq_dir is not None:
        fastq_by_sample = {
            _strip_fastq_suffix(path): str(path)
            for path in _find_fastqs(fastq_dir)
        }

    source_cols: List[str]
    if path_col is not None:
        if path_col not in adata.obs.columns:
            raise ValueError(f"adata.obs does not contain {path_col!r}")
        source_cols = [path_col]
    else:
        source_cols = [
            col for col in ("clean_fastq_path", "fastq_path") if col in adata.obs.columns
        ]

    trax_fq: Dict[str, str] = {}
    for sample_name in adata.obs_names:
        chosen = ""
        for col in source_cols:
            value = adata.obs.loc[sample_name, col]
            if _nonempty(value):
                chosen = str(Path(str(value)).expanduser().resolve())
                break

        if (not chosen or not Path(chosen).is_file()) and sample_name in fastq_by_sample:
            chosen = fastq_by_sample[sample_name]

        if chosen and Path(chosen).is_file():
            trax_fq[str(sample_name)] = chosen

    if not trax_fq:
        detail = "clean_fastq_path/fastq_path"
        if fastq_dir is not None:
            detail += f" or FASTQs matched from {Path(fastq_dir).expanduser()}"
        raise ValueError(f"No usable tRAX FASTQ paths found from {detail}")

    prepared = adata[list(trax_fq)].copy()
    prepared.obs["trax_fq"] = [trax_fq[sample] for sample in prepared.obs_names]
    return prepared


def _sample_entries_from_adata(
    adata: AnnData,
    *,
    replicate_col: Optional[str] = None,
) -> List[Dict[str, str]]:
    if "trax_fq" not in adata.obs.columns:
        raise ValueError("adata.obs must contain 'trax_fq'")

    entries: List[Dict[str, str]] = []
    for sample_name in adata.obs_names:
        fastq = str(adata.obs.loc[sample_name, "trax_fq"]).strip()
        if not fastq:
            continue
        replicate = (
            str(adata.obs.loc[sample_name, replicate_col])
            if replicate_col and replicate_col in adata.obs.columns
            else str(sample_name)
        )
        entries.append({
            "sample": _sanitize_sample_name(str(sample_name)),
            "replicate": _sanitize_sample_name(replicate),
            "fastq": str(Path(fastq).expanduser().resolve()),
        })
    if not entries:
        raise ValueError("No usable FASTQ paths found in adata.obs['trax_fq']")
    for entry in entries:
        if not Path(entry["fastq"]).is_file():
            raise FileNotFoundError(f"FASTQ file not found: {entry['fastq']}")
    return entries


def _write_samplefile(entries: Iterable[Dict[str, str]], samplefile: Path) -> Path:
    samplefile.parent.mkdir(parents=True, exist_ok=True)
    with open(samplefile, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(
                f"{entry['sample']}\t{entry['replicate']}\t{entry['fastq']}\n"
            )
    return samplefile


def _validate_trnadb(databasename: str | Path) -> Path:
    prefix = Path(databasename).expanduser()
    if prefix.is_dir():
        tables = sorted(prefix.glob("*-trnatable.txt"))
        if not tables:
            raise FileNotFoundError(f"No *-trnatable.txt found in tRNAdb directory: {prefix}")
        if len(tables) > 1:
            names = ", ".join(path.name for path in tables)
            raise ValueError(
                "Multiple tRNAdb prefixes found; pass the desired prefix explicitly: "
                + names
            )
        prefix = prefix / tables[0].name[: -len("-trnatable.txt")]
    required = [
        f"{prefix}-trnatable.txt",
        f"{prefix}-maturetRNAs.bed",
        f"{prefix}-trnaloci.bed",
        f"{prefix}-tRNAgenome.fa",
    ]
    missing = [path for path in required if not Path(path).exists()]
    if missing:
        raise FileNotFoundError(
            "tRNAdb prefix is incomplete; missing: " + ", ".join(missing)
        )
    if not (
        Path(f"{prefix}-tRNAgenome.1.bt2").exists()
        or Path(f"{prefix}-tRNAgenome.1.bt2l").exists()
    ):
        raise FileNotFoundError(f"Bowtie2 index not found for tRNAdb prefix: {prefix}")
    return prefix.resolve()


def _split_trax_feature(feature: str) -> tuple[str, str]:
    for suffix in TRAX_FRAGMENT_SUFFIXES:
        marker = f"_{suffix}"
        if feature.endswith(marker):
            return feature[: -len(marker)], suffix
    return feature, ""


def _read_trax_counts(counts_path: str | Path) -> pd.DataFrame:
    path = Path(counts_path)
    if not path.exists():
        raise FileNotFoundError(f"tRAX trnacounts file not found: {path}")

    with open(path, encoding="utf-8") as handle:
        header = handle.readline().rstrip("\n").split("\t")
        if not header or not header[0]:
            raise ValueError(f"Invalid tRAX trnacounts header in: {path}")

        rows: list[str] = []
        values: list[list[float]] = []
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) != len(header) + 1:
                raise ValueError(
                    f"Invalid tRAX count row in {path}: expected "
                    f"{len(header) + 1} fields, got {len(fields)}"
                )
            rows.append(fields[0])
            values.append([float(value) for value in fields[1:]])

    return pd.DataFrame(values, index=rows, columns=header, dtype=np.float64)


def _store_trax_counts(
    adata: AnnData,
    counts_path: str | Path,
) -> AnnData:
    counts = _read_trax_counts(counts_path)
    missing = [sample for sample in adata.obs_names if sample not in counts.columns]
    if missing:
        raise ValueError(
            "tRAX count matrix is missing samples from adata.obs_names: "
            + ", ".join(missing)
        )

    counts = counts.loc[:, list(adata.obs_names)]
    matrix = counts.T.to_numpy(dtype=np.float64)
    parsed = [_split_trax_feature(feature) for feature in counts.index]
    trax_var = pd.DataFrame(index=counts.index)
    trax_var["trax_feature_id"] = list(counts.index)
    trax_var["trna_id"] = [item[0] for item in parsed]
    trax_var["fragment_type"] = [item[1] for item in parsed]

    stored = store_count_matrix(adata, matrix, trax_var, rna_type="tRNA")
    stored.uns["trax_count_matrix"] = str(Path(counts_path).resolve())
    return stored


@register_function(
    aliases=[
        "trax_quant",
        "tRAX",
        "quantify_trna_fragments",
        "quantify_tdr",
        "tRNA片段定量",
    ],
    category="quant",
    description=(
        "Quantify tRNA-derived fragments from an AnnData object using the "
        "bundled tRAX processesamples.py workflow. Input FASTQs are copied to "
        "adata.obs['trax_fq'] from adata.obs['clean_fastq_path'] first, then "
        "adata.obs['fastq_path'], and finally by matching fastq_dir basenames "
        "to adata.obs_names. Counts are merged into the shared "
        "adata.layers['counts'] expression matrix."
    ),
    examples=[
        (
            'adata = sa.quant.trax_quant(\n'
            '    adata,\n'
            '    fastq_dir="data/srna_fastq",  # optional fallback/matching\n'
            '    databasename="data/trnadb_test/hg38",\n'
            '    output_dir="testdata", cores=4,\n'
            ')'
        ),
    ],
    related=["reference.build_trnadb"],
    produces={"uns": ["trax_result"]},
)
def trax_quant(
    adata: AnnData,
    *,
    fastq_dir: Optional[str] = None,
    databasename: str,
    output_dir: str = "trax_out",
    experiment_name: str = "trax_quant",
    ensemblgtf: Optional[str] = None,
    bedfiles: Optional[Sequence[str]] = None,
    cores: int = 4,
    lazyremap: bool = False,
    maponly: bool = False,
    nofrag: bool = False,
    maxmismatches: Optional[int] = None,
    minnontrnasize: int = 20,
    local: bool = False,
    skipfqcheck: bool = False,
    path_col: Optional[str] = None,
    replicate_col: Optional[str] = None,
) -> AnnData:
    """Run tRAX tRNA fragment quantification from an AnnData object.

    ``databasename`` is the tRNAdb prefix, for example ``/path/to/tRNAdb/hg38``
    when files are named ``hg38-trnatable.txt`` etc. The selected FASTQ paths
    are stored in ``adata.obs["trax_fq"]``; existing FASTQ columns are not
    overwritten. Samples without a usable tRAX FASTQ path are dropped from the
    returned AnnData object. tRAX counts are merged into the shared
    ``adata.layers["counts"]`` matrix and annotated with
    ``adata.var["rna_type"]``.
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")

    adata = _prepare_trax_fastqs(adata, fastq_dir=fastq_dir, path_col=path_col)

    db_prefix = _validate_trnadb(databasename)
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    bam_dir = out_dir / "bam"
    bam_dir.mkdir(parents=True, exist_ok=True)
    exp_dir = out_dir / experiment_name

    entries = _sample_entries_from_adata(
        adata,
        replicate_col=replicate_col,
    )

    samplefile = _write_samplefile(entries, out_dir / f"{experiment_name}-samples.txt")
    script = _trax_script()
    if not script.exists():
        raise FileNotFoundError(f"processsamples.py not found at {script}")

    cmd = [
        "python3",
        str(script),
        f"--experimentname={experiment_name}",
        f"--databasename={db_prefix}",
        f"--samplefile={samplefile}",
        f"--bamdir={bam_dir}/",
        f"--cores={int(cores)}",
        f"--minnontrnasize={int(minnontrnasize)}",
    ]
    if ensemblgtf:
        cmd.append(f"--ensemblgtf={Path(ensemblgtf).expanduser().resolve()}")
    for bedfile in bedfiles or []:
        cmd.append(f"--bedfile={Path(bedfile).expanduser().resolve()}")
    if lazyremap:
        cmd.append("--lazyremap")
    if maponly:
        cmd.append("--maponly")
    if nofrag:
        cmd.append("--nofrag")
    if maxmismatches is not None:
        cmd.append(f"--maxmismatches={maxmismatches}")
    if local:
        cmd.append("--local")
    if skipfqcheck:
        cmd.append("--skipfqcheck")

    run_cli_cmd(cmd, cwd=str(out_dir))

    base = exp_dir / experiment_name
    trna_counts = str(base.with_name(f"{experiment_name}-trnacounts.txt"))
    result: Dict[str, object] = {
        "experiment": experiment_name,
        "output_dir": str(out_dir),
        "experiment_dir": str(exp_dir),
        "samplefile": str(samplefile),
        "bam_dir": str(bam_dir),
        "databasename": str(db_prefix),
        "samples": {
            "sample": [entry["sample"] for entry in entries],
            "replicate": [entry["replicate"] for entry in entries],
            "fastq": [entry["fastq"] for entry in entries],
        },
        "command": cmd,
        "files": {
            "trna_counts": trna_counts,
            "type_counts": str(base.with_name(f"{experiment_name}-typecounts.txt")),
            "type_real_counts": str(base.with_name(f"{experiment_name}-typerealcounts.txt")),
            "amino_counts": str(base.with_name(f"{experiment_name}-aminocounts.txt")),
            "anticodon_counts": str(base.with_name(f"{experiment_name}-anticodoncounts.txt")),
            "read_lengths": str(base.with_name(f"{experiment_name}-readlengths.txt")),
            "mismatches": str(base.with_name(f"{experiment_name}-mismatches.txt")),
            "map_stats": str(base.with_name(f"{experiment_name}-mapstats.txt")),
            "run_info": str(base.with_name(f"{experiment_name}-runinfo.txt")),
        },
    }

    adata.obs["trax_bam"] = [
        str(bam_dir / f"{entry['sample']}.bam") for entry in entries
    ]
    adata.obs["trax_sample"] = [entry["sample"] for entry in entries]
    adata.obs["trax_replicate"] = [entry["replicate"] for entry in entries]
    adata.uns["trax_result"] = result

    if maponly:
        return adata

    quantified = _store_trax_counts(adata, trna_counts)
    quantified.uns["trax_result"] = result
    return quantified


quantify_trna_fragments = trax_quant
tRAX = trax_quant
