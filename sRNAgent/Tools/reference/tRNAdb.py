"""tRAX reference helper utilities.

This module builds the small-RNA GTF input expected by the tRAX
quantification workflow. It intentionally mirrors the historical shell filter:

    grep -v '^#' | awk '{print "chr" $0;}' |
    grep -e Mt_rRNA -e miRNA -e misc_RNA -e rRNA -e snRNA \
        -e snoRNA -e ribozyme -e sRNA -e scaRNA
"""

from __future__ import annotations

import gzip
import tarfile
from pathlib import Path
from typing import Dict, Iterable, Optional

from ..._registry import register_function
from ..._utils import run_cli_cmd
from . import genome


TRAX_GTF_FEATURE_TERMS = (
    "Mt_rRNA",
    "miRNA",
    "misc_RNA",
    "rRNA",
    "snRNA",
    "snoRNA",
    "ribozyme",
    "sRNA",
    "scaRNA",
)

TRNASCAN_HG38_URL = (
    "http://gtrnadb.ucsc.edu/GtRNAdb2/genomes/eukaryota/"
    "Hsapi38/hg38-tRNAs.tar.gz"
)

TRNASCAN_HG38_FILES = (
    "hg38-filtered-tRNAs.fa",
    "hg38-mature-tRNAs.fa",
    "hg38-tRNAs.bed",
    "hg38-tRNAs-confidence-set.out",
    "hg38-tRNAs-confidence-set.ss",
    "hg38-tRNAs-detailed.out",
    "hg38-tRNAs-detailed.ss",
    "hg38-tRNAs.fa",
    "hg38-tRNAs_name_map.txt",
)

_BOWTIE2_INDEX_SUFFIXES = (
    (".1.bt2", ".2.bt2", ".3.bt2", ".4.bt2", ".rev.1.bt2", ".rev.2.bt2"),
    (".1.bt2l", ".2.bt2l", ".3.bt2l", ".4.bt2l", ".rev.1.bt2l", ".rev.2.bt2l"),
)


def _ensure_human_hg38(assembly: Optional[str]) -> None:
    """Reject unsupported assemblies; this tRAX helper is hg38-only."""
    if assembly is None:
        return
    if assembly.lower() not in {"hg38", "grch38"}:
        raise ValueError("Only human hg38/GRCh38 is supported.")


def _iter_filtered_gtf_lines(gtf_gz: str | Path, terms: Iterable[str]) -> Iterable[str]:
    """Yield Ensembl GTF lines after applying the legacy tRAX filter."""
    terms = tuple(terms)
    with gzip.open(gtf_gz, "rt") as handle:
        for raw_line in handle:
            if raw_line.startswith("#"):
                continue
            line = "chr" + raw_line.rstrip("\n")
            if any(term in line for term in terms):
                yield line + "\n"


def _safe_extract_tar(archive: str | Path, output_dir: str | Path) -> None:
    """Extract a tar archive without allowing path traversal."""
    out_dir = Path(output_dir).resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (out_dir / member.name).resolve()
            if not str(target).startswith(str(out_dir) + "/") and target != out_dir:
                raise RuntimeError(f"Refusing unsafe tar member: {member.name}")
        tar.extractall(out_dir)


def _find_expected_file(output_dir: Path, filename: str) -> Optional[Path]:
    """Find an extracted expected file, allowing archives with one subdirectory."""
    direct = output_dir / filename
    if direct.exists():
        return direct
    matches = list(output_dir.rglob(filename))
    if matches:
        return matches[0]
    return None


def _db_prefix(databasename: str) -> Path:
    return Path(databasename).expanduser()


def _parse_dbinfo(dbinfo_path: Path) -> Dict[str, str]:
    if not dbinfo_path.exists():
        return {}
    info: Dict[str, str] = {}
    for raw_line in dbinfo_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or "\t" not in line:
            continue
        key, value = line.split("\t", 1)
        info[key.strip()] = value.strip()
    return info


def _bowtie_index_files(prefix: Path) -> list[Path]:
    for suffixes in _BOWTIE2_INDEX_SUFFIXES:
        files = [Path(f"{prefix}{suffix}") for suffix in suffixes]
        if all(path.exists() for path in files):
            return files
    return []


def _collect_trnadb_manifest(prefix: Path) -> Dict[str, object]:
    db_dir = prefix.parent if str(prefix.parent) not in ("", ".") else Path.cwd()
    base = prefix.name
    files: Dict[str, object] = {}
    candidates = {
        "dbinfo": db_dir / f"{base}-dbinfo.txt",
        "tRNAgenome": db_dir / f"{base}-tRNAgenome.fa",
        "maturetRNAs": db_dir / f"{base}-maturetRNAs.fa",
        "maturetRNAs_bed": db_dir / f"{base}-maturetRNAs.bed",
        "trnaloci_bed": db_dir / f"{base}-trnaloci.bed",
        "trnaalign_stk": db_dir / f"{base}-trnaalign.stk",
        "trnaloci_stk": db_dir / f"{base}-trnaloci.stk",
        "trnatable": db_dir / f"{base}-trnatable.txt",
        "alignnum": db_dir / f"{base}-alignnum.txt",
        "locusnum": db_dir / f"{base}-locusnum.txt",
        "otherseqs": db_dir / f"{base}-otherseqs.txt",
        "additionals": db_dir / f"{base}-additionals.fa",
    }
    for key, path in candidates.items():
        if path.exists():
            files[key] = str(path.resolve())

    index_files = _bowtie_index_files(db_dir / f"{base}-tRNAgenome")
    if index_files:
        files["bowtie2_index"] = str(index_files[0].parent.resolve())
        files["bowtie2_index_files"] = [str(p.resolve()) for p in index_files]

    dbinfo_path = candidates["dbinfo"]
    dbinfo = _parse_dbinfo(dbinfo_path)
    complete = all(key in files for key in (
        "dbinfo",
        "tRNAgenome",
        "maturetRNAs",
        "maturetRNAs_bed",
        "trnaloci_bed",
        "trnaalign_stk",
        "trnaloci_stk",
        "trnatable",
        "alignnum",
        "locusnum",
    )) and "bowtie2_index" in files

    return {
        "databasename": str(prefix),
        "exists": bool(files),
        "complete": complete,
        "files": files,
        "dbinfo": dbinfo,
    }


def _build_maketrnadb_command(
    *,
    script: Path,
    databasename: str,
    genomefile: str,
    trnascanfile: str,
    namemapfile: str,
    gtrnafafile: Optional[str],
    orgmode: Optional[str],
    forcecca: bool,
    addtrnas: Optional[str],
    addseqs: Optional[str],
) -> list[str]:
    cmd = [
        "python3",
        str(script),
        f"--databasename={databasename}",
        f"--genomefile={genomefile}",
        f"--trnascanfile={trnascanfile}",
        f"--namemapfile={namemapfile}",
    ]
    if gtrnafafile:
        cmd.append(f"--gtrnafafile={gtrnafafile}")
    if orgmode:
        cmd.append(f"--orgmode={orgmode}")
    if forcecca:
        cmd.append("--forcecca")
    if addtrnas:
        cmd.append(f"--addtrnas={addtrnas}")
    if addseqs:
        cmd.append(f"--addseqs={addseqs}")
    return cmd


def _prepare_genomefile_for_maketrnadb(genomefile: str, work_dir: Path) -> str:
    genome_path = Path(genomefile).expanduser()
    if Path(f"{genome_path}.fai").exists():
        return str(genome_path)

    work_dir.mkdir(parents=True, exist_ok=True)
    link_path = work_dir / genome_path.name
    if not link_path.exists():
        try:
            link_path.symlink_to(genome_path)
        except OSError:
            link_path.hardlink_to(genome_path)
    return str(link_path)


@register_function(
    aliases=[
        "download_trax_human_gtf",
        "download_human_trax_gtf",
        "build_trax_human_gtf",
        "下载tRAX人类GTF",
    ],
    category="reference",
    description=(
        "Download the current human Ensembl GTF through reference.genome and "
        "write the filtered small-RNA GTF used by tRAX quantification. The "
        "filter is equivalent to: grep -v '^#' | awk '{print \"chr\" $0;}' | "
        "grep -e Mt_rRNA -e miRNA -e misc_RNA -e rRNA -e snRNA -e snoRNA "
        "-e ribozyme -e sRNA -e scaRNA."
    ),
    examples=[
        'sa.reference.download_trax_human_gtf(output_dir="ref")',
        (
            'sa.reference.build_trax_human_gtf('
            'output_dir="ref", output_name="hg38-genes.gtf")'
        ),
    ],
    related=["reference.download_genome", "reference.download_gtf"],
    produces={"uns": ["trax_gtf"]},
)
def download_trax_human_gtf(
    output_dir: str = ".",
    output_name: str = "hg38-genes.gtf",
    assembly: Optional[str] = "GRCh38",
    jobs: int = 4,
    force: bool = False,
) -> Dict[str, str]:
    """Download human Ensembl GTF and save the tRAX-filtered GTF.

    Parameters
    ----------
    output_dir
        Directory to save the downloaded ``.gtf.gz`` and filtered ``.gtf``.
    output_name
        Filename for the filtered GTF.
    assembly
        Assembly name passed to the Ensembl finder. Defaults to ``"GRCh38"``.
    jobs
        Number of download threads.
    force
        Re-download and regenerate even if output files already exist.

    Returns
    -------
    dict
        ``{"source_gtf_gz": "<downloaded .gtf.gz>", "trax_gtf": "<filtered .gtf>"}``
    """
    _ensure_human_hg38(assembly)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    species = "homo_sapiens"
    filename = genome._find_ensembl_gtf_file(species, assembly)
    url = f"{genome.GTF_BASE}/{genome._species_dirname(species)}/{filename}"
    source_gtf = Path(
        genome.resumable_download(url, out_dir / filename, jobs=jobs, force=force)
    )

    filtered_gtf = out_dir / output_name
    if force or not filtered_gtf.exists():
        with open(filtered_gtf, "w") as out_handle:
            for line in _iter_filtered_gtf_lines(source_gtf, TRAX_GTF_FEATURE_TERMS):
                out_handle.write(line)

    return {
        "source_gtf_gz": str(source_gtf),
        "trax_gtf": str(filtered_gtf),
    }


build_trax_human_gtf = download_trax_human_gtf


@register_function(
    aliases=[
        "download_trnascan_hg38",
        "download_human_trnascan",
        "download_hg38_trnascan",
        "下载人类tRNAscan",
    ],
    category="reference",
    description=(
        "Download and extract the human hg38 tRNAscan-SE files from GtRNAdb. "
        "Only human hg38/GRCh38 is supported."
    ),
    examples=[
        'sa.reference.download_trnascan_hg38(output_dir="ref")',
    ],
    related=["reference.download_trax_human_gtf"],
    produces={"uns": ["trnascan_hg38_files"]},
)
def download_trnascan_hg38(
    output_dir: str = ".",
    assembly: Optional[str] = "hg38",
    jobs: int = 4,
    force: bool = False,
) -> Dict[str, object]:
    """Download and extract the human hg38 tRNAscan-SE archive.

    Parameters
    ----------
    output_dir
        Directory to save ``hg38-tRNAs.tar.gz`` and the extracted files.
    assembly
        Assembly selector. Only ``"hg38"`` or ``"GRCh38"`` is accepted.
    jobs
        Number of download threads.
    force
        Re-download and re-extract even if files already exist.

    Returns
    -------
    dict
        ``{"archive": "<tar.gz>", "output_dir": "<dir>", "files": {...}}``
    """
    _ensure_human_hg38(assembly)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    archive = Path(
        genome.resumable_download(
            TRNASCAN_HG38_URL,
            out_dir / "hg38-tRNAs.tar.gz",
            jobs=jobs,
            force=force,
        )
    )

    extracted = {
        filename: _find_expected_file(out_dir, filename)
        for filename in TRNASCAN_HG38_FILES
    }
    if force or any(path is None for path in extracted.values()):
        _safe_extract_tar(archive, out_dir)
        extracted = {
            filename: _find_expected_file(out_dir, filename)
            for filename in TRNASCAN_HG38_FILES
        }

    missing = [filename for filename, path in extracted.items() if path is None]
    if missing:
        raise FileNotFoundError(
            "tRNAscan-SE archive did not contain expected files: "
            + ", ".join(missing)
        )

    return {
        "archive": str(archive),
        "output_dir": str(out_dir),
        "files": {filename: str(path) for filename, path in extracted.items()},
    }


@register_function(
    aliases=[
        "build_trnadb",
        "build_tRNAdb",
        "maketrnadb",
        "build_trna_database",
        "构建tRNAdb",
    ],
    category="reference",
    description=(
        "Build a tRAX tRNAdb reference database by calling the bundled "
        "maketrnadb.py script on pre-downloaded inputs. Requires a genome "
        "FASTA, a tRNAscan-SE output file, and the GtRNAdb name-map file. "
        "If the database already exists, the function only records the "
        "existing files and parsed dbinfo instead of rebuilding."
    ),
    examples=[
        'sa.reference.build_trnadb("ref/hg38-trna", "ref/GRCh38.primary_assembly.genome.fa", "ref/hg38-tRNAs.txt", "ref/hg38-tRNAs_name_map.txt")',
    ],
    related=["reference.download_trnascan_hg38", "reference.download_trax_human_gtf"],
    produces={"uns": ["tRNAdb_dbinfo", "tRNAdb_files"]},
)
def build_trnadb(
    databasename: str,
    genomefile: str,
    trnascanfile: str,
    namemapfile: str,
    gtrnafafile: Optional[str] = None,
    orgmode: Optional[str] = None,
    forcecca: bool = False,
    addtrnas: Optional[str] = None,
    addseqs: Optional[str] = None,
    overwrite: bool = False,
) -> Dict[str, object]:
    """Build a tRAX tRNAdb database from pre-downloaded input files."""
    prefix = _db_prefix(databasename).resolve()
    manifest = _collect_trnadb_manifest(prefix)
    if manifest.get("complete") and not overwrite:
        manifest["status"] = "existing"
        return manifest

    prefix.parent.mkdir(parents=True, exist_ok=True)

    script = Path(__file__).resolve().parents[1] / "quant" / "external" / "tRAX" / "maketrnadb.py"
    if not script.exists():
        raise FileNotFoundError(f"maketrnadb.py not found at {script}")

    genome_for_build = _prepare_genomefile_for_maketrnadb(genomefile, prefix.parent)

    cmd = _build_maketrnadb_command(
        script=script,
        databasename=str(prefix),
        genomefile=genome_for_build,
        trnascanfile=trnascanfile,
        namemapfile=namemapfile,
        gtrnafafile=gtrnafafile,
        orgmode=orgmode,
        forcecca=forcecca,
        addtrnas=addtrnas,
        addseqs=addseqs,
    )
    run_cli_cmd(cmd, cwd=str(prefix.parent))

    manifest = _collect_trnadb_manifest(prefix)
    manifest["status"] = "built"
    manifest["command"] = cmd
    manifest["inputs"] = {
        "genomefile": str(Path(genomefile).resolve()),
        "genomefile_used": str(Path(genome_for_build).resolve()),
        "trnascanfile": str(Path(trnascanfile).resolve()),
        "namemapfile": str(Path(namemapfile).resolve()),
    }
    if gtrnafafile:
        manifest["inputs"]["gtrnafafile"] = str(Path(gtrnafafile).resolve())
    if addtrnas:
        manifest["inputs"]["addtrnas"] = str(Path(addtrnas).resolve())
    if addseqs:
        manifest["inputs"]["addseqs"] = str(Path(addseqs).resolve())
    return manifest
