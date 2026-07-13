"""sRNAgent — tool-loop agent wired to function + skill registries."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .agent_config import ExecutionConfig, SandboxFallbackPolicy
from .bootstrap import initialize_agent_runtime, initialize_registries
from .execution import ExecutionBackend, initialize_execution_backend
from .llm_client import ChatClient, LLMConfig
from .plan_orchestrator import PlanOrchestrator
from .tools import (
    AGENT_TOOL_SCHEMAS,
    execute_code,
    list_available_skills,
    search_functions,
    search_skills,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[Dict[str, Any]], None]
CodeApprovalCallback = Callable[[str, str, str], bool]
StreamCallback = Callable[[str, str], None]

_CODE_PROGRESS_INTERVALS = (10, 15, 20, 30, 30, 30, 30, 30, 30, 30, 30)
_SSE_PROGRESS_HEARTBEAT_SEC = 12
_PROGRESS_MARKER = "__SRNAGENT_DL__"
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_BROKEN_ESCAPE_RE = re.compile(r"\[(?:\[[0-9;]*[A-Za-z]|[0-9;]*[A-Za-z])")
_OSC_HYPERLINK_RE = re.compile(r"\]8;[^;\n]*;[^\n\\]*\\?")
_ACCESSION_RE = re.compile(r"\b(SRR|ERR|DRR|SRS|SRP|ERP|DRP|GSE|GSM)\d+\b")
_RUN_ID_RE = re.compile(r"\b(SRR|ERR|DRR)\d+\b")
_SIZE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)\b|\b(\d+)\s*/\s*(\d+)\b|(\d+(?:\.\d+)?%)",
    re.IGNORECASE,
)
_DOWNLOAD_CMD_RE = re.compile(r"\b(curl|wget|urllib|requests\.get|fastq[\-_]?dl)\b", re.IGNORECASE)
_CURL_OUT_RE = re.compile(r"""[-]o\s+['"]?([^\s'"]+)['"]?""", re.IGNORECASE)
_OUT_PATH_RE = re.compile(r"\bOut:\s*(\S+)", re.IGNORECASE)
_FILE_PATH_RE = re.compile(
    r"[\w./-]+\.(?:fastq(?:\.gz)?|fa(?:\.gz)?|fasta(?:\.gz)?|gtf(?:\.gz)?|sra|bam|fq(?:\.gz)?)",
    re.IGNORECASE,
)
_SIZE_BYTES_RE = re.compile(r"size:\s*(\d+)\s*bytes", re.IGNORECASE)
_DOWNLOAD_LOG_RE = re.compile(r"\[download\]\s+(\S+)(?:\s+\(([^)]+)\))?", re.IGNORECASE)
_REFERENCE_DL_RE = re.compile(
    r"download_(?:genome|gtf|ncrna)|reference\.download|resumable_download",
    re.IGNORECASE,
)
_OUTDIR_RE = re.compile(r"""output_dir\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


class AgentCancelledError(Exception):
    """Raised when the agent run is cancelled by the user."""


def _summarize_tool_call(name: str, arguments: Dict[str, Any]) -> str:
    if name == "search_functions":
        return f"search_functions({arguments.get('query', '')!r})"
    if name == "search_skills":
        return f"search_skills({arguments.get('query', '')!r})"
    if name == "execute_code":
        desc = str(arguments.get("description") or "").strip()
        code = str(arguments.get("code") or "").strip()
        if desc:
            return f"execute_code — {desc}"
        preview = code.split("\n", 1)[0][:80]
        return f"execute_code — {preview}{'…' if len(code) > 80 else ''}"
    if name == "finish":
        msg = str(arguments.get("message") or "")
        preview = msg[:120] + ("…" if len(msg) > 120 else "")
        return f"finish — {preview}"
    return name


def _truncate_result(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} 秒"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {secs} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def _strip_terminal_noise(text: str) -> str:
    cleaned = (text or "").replace("\r", "\n")
    cleaned = _OSC_HYPERLINK_RE.sub("", cleaned)
    cleaned = _ANSI_ESCAPE_RE.sub("", cleaned)
    cleaned = _BROKEN_ESCAPE_RE.sub("", cleaned)
    cleaned = cleaned.replace("\x07", "").replace("\x1b", "")
    cleaned = re.sub(r"\\+/", " ", cleaned)
    cleaned = re.sub(r"\b(INFO|WARNING|ERROR|DEBUG)\b", r"\n\1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _shorten_paths(text: str) -> str:
    return re.sub(
        r"(?<![\w./-])/(?:[^/\s]+/){2,}([^/\s]+(?:\.[A-Za-z0-9]+)?)",
        r".../\1",
        text,
    )


def _clean_log_line(line: str) -> str:
    line = _shorten_paths(line.strip())
    line = re.sub(r"^(INFO|WARNING|ERROR|DEBUG)\s+", "", line, flags=re.IGNORECASE)
    line = re.sub(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[,.]?\d*\s*", "", line)
    line = re.sub(r"[./\\]+$", "", line)
    line = re.sub(r"\.{3,}$", "", line)
    return line.strip(" -•|")


def _line_quality(line: str) -> float:
    if not line:
        return 0.0
    printable = sum(1 for ch in line if ch.isprintable() or ch.isspace())
    return printable / max(len(line), 1)


_TQDM_BAR_RE = re.compile(r"\d+%\|[\s█▏▎▍▌▋▊▉]+\|")


def _is_download_progress_line(line: str) -> bool:
    if _PROGRESS_MARKER in line:
        return True
    if _TQDM_BAR_RE.search(line):
        return True
    lowered = line.lower()
    if "overallpct" in lowered and "bytestotal" in lowered:
        return True
    if re.search(r"\[\d{2}:\d{2}<\d{2}:\d{2},\s*\d+[kMG]?B/s\]", line):
        return True
    return False


def _is_noise_line(line: str) -> bool:
    if _is_download_progress_line(line):
        return True
    if not line or len(line) < 2:
        return True
    if _line_quality(line) < 0.85:
        return True
    lowered = line.lower()
    if "site-packages" in lowered and ".py" in lowered:
        return True
    if lowered.startswith("file ") and lowered.endswith(".py"):
        return True
    if len(line) > 180 and not _ACCESSION_RE.search(line) and not _SIZE_RE.search(line):
        return True
    return False


def _infer_progress_stage(lines: List[str]) -> str:
    for line in reversed(lines):
        match = _ACCESSION_RE.search(line)
        if match and re.search(r"work|download|fetch|process|run|sample", line, re.IGNORECASE):
            return f"正在处理 {match.group(0)}"
        if match and re.search(r"complete|done|finish|success", line, re.IGNORECASE):
            return f"已完成 {match.group(0)}"
    for line in reversed(lines):
        match = _ACCESSION_RE.search(line)
        if match:
            return f"正在处理 {match.group(0)}"
    if lines:
        return "任务进行中"
    return "等待输出"


def _parse_download_progress_marker(text: str) -> Dict[str, Any]:
    if _PROGRESS_MARKER not in text:
        return {}
    for raw in reversed(text.splitlines()):
        line = raw.strip()
        if _PROGRESS_MARKER not in line:
            continue
        payload_raw = line.split(_PROGRESS_MARKER, 1)[-1].strip()
        if not payload_raw:
            continue
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        result: Dict[str, Any] = {}
        if payload.get("overallPct") is not None:
            result["progressOverallPct"] = float(payload["overallPct"])
        if payload.get("filePct") is not None:
            result["progressFilePct"] = float(payload["filePct"])
        if payload.get("run"):
            result["progressRun"] = str(payload["run"])
        if payload.get("fileIndex") is not None:
            result["progressFileIndex"] = int(payload["fileIndex"])
        if payload.get("fileTotal") is not None:
            result["progressFileTotal"] = int(payload["fileTotal"])
        bytes_total = int(payload.get("bytesTotal") or 0)
        bytes_done = int(payload.get("bytes") or 0)
        if bytes_total > 0:
            result["progressBytesTotal"] = bytes_total
            result["progressBytes"] = bytes_done
        if result:
            run = result.get("progressRun") or "FASTQ"
            file_i = result.get("progressFileIndex")
            file_n = result.get("progressFileTotal")
            overall = result.get("progressOverallPct")
            if file_i and file_n and overall is not None:
                result["progressLabel"] = f"{run} · 文件 {file_i}/{file_n} · 总进度 {overall:.1f}%"
            elif overall is not None:
                result["progressLabel"] = f"{run} · 总进度 {overall:.1f}%"
        return result
    return {}


def _format_bytes(num: int) -> str:
    value = float(max(0, num))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(num)} B"


def _looks_like_download(text: str) -> bool:
    combined = text or ""
    return bool(_DOWNLOAD_CMD_RE.search(combined))


def _parse_human_size(text: str) -> int:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)", text, re.IGNORECASE)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).upper()
    mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(value * mult.get(unit, 1))


def _output_dirs_from_code(code: str) -> List[str]:
    dirs = [""]
    for match in _OUTDIR_RE.finditer(code or ""):
        dirs.append(match.group(1).strip())
    return dirs


def _download_bytes_on_disk(path: Path) -> int:
    total = 0
    if path.exists():
        try:
            total += path.stat().st_size
        except OSError:
            pass
    for part in path.parent.glob(f"{path.name}.part.*"):
        try:
            total += part.stat().st_size
        except OSError:
            pass
    return total


def _code_is_download_task(code: str) -> bool:
    if not code:
        return False
    if _DOWNLOAD_CMD_RE.search(code):
        return True
    if _REFERENCE_DL_RE.search(code):
        return True
    return bool(re.search(r"fastq_dl|download_ena|urllib\.request\.urlretrieve", code, re.I))


def _lookup_fastq_bytes(workspace: Path, run_id: str) -> int:
    if not run_id or not workspace.is_dir():
        return 0
    import csv

    for tsv_path in sorted(workspace.rglob("*run-info*.tsv")):
        try:
            with tsv_path.open(encoding="utf-8") as handle:
                reader = csv.DictReader(handle, delimiter="\t")
                for row in reader:
                    if str(row.get("run_accession") or "").strip() != run_id:
                        continue
                    raw = str(row.get("fastq_bytes") or row.get("sra_bytes") or "0").strip()
                    if raw.isdigit():
                        return int(raw)
        except OSError:
            continue
    return 0


def _expected_download_total(
    workspace: Path,
    run_id: str,
    text: str,
    code: str,
    *,
    filename: str = "",
) -> int:
    if filename:
        escaped = re.escape(filename)
        for match in re.finditer(rf"\[download\]\s+{escaped}\s+\(([^)]+)\)", text, re.I):
            size = _parse_human_size(match.group(1))
            if size > 0:
                return size
    expected = _lookup_fastq_bytes(workspace, run_id)
    if expected > 0:
        return expected
    for match in _SIZE_BYTES_RE.finditer(text):
        expected = max(expected, int(match.group(1)))
    combined = f"{text}\n{code}"
    url_match = re.search(
        r"https?://[^\s'\"]+\.(?:fastq|fq|fa|fasta|gtf)(?:\.gz)?",
        combined,
        re.I,
    )
    if url_match:
        try:
            import urllib.request

            request = urllib.request.Request(url_match.group(0), method="HEAD")
            with urllib.request.urlopen(request, timeout=8) as response:
                length = int(response.headers.get("Content-Length") or 0)
                if length > 0:
                    return length
        except Exception:
            pass
    return expected


def _collect_download_paths(text: str, code: str = "") -> List[str]:
    combined = f"{text}\n{code or ''}"
    paths: set[str] = set()
    for match in _CURL_OUT_RE.finditer(combined):
        paths.add(match.group(1).strip().strip("'").strip('"'))
    for match in _OUT_PATH_RE.finditer(combined):
        paths.add(match.group(1).strip())
    for match in _FILE_PATH_RE.finditer(combined):
        paths.add(match.group(0).strip())
    for match in _DOWNLOAD_LOG_RE.finditer(combined):
        fname = match.group(1).strip()
        paths.add(fname)
        for outdir in _output_dirs_from_code(code):
            if outdir:
                paths.add(str(Path(outdir) / fname))
    for match in _ACCESSION_RE.finditer(combined):
        accession = match.group(0)
        paths.add(f"srna_fastq/{accession}.fastq.gz")
        paths.add(f"{accession}.fastq.gz")
    return sorted(paths)


def _resolve_workspace_path(workspace: Path, rel_path: str) -> Optional[Path]:
    raw = rel_path.strip().strip("'").strip('"')
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate if candidate.exists() else None
    direct = workspace / candidate
    if direct.exists():
        return direct
    by_name = workspace / candidate.name
    if by_name.exists():
        return by_name
    search_dirs = [workspace]
    if workspace.is_dir():
        search_dirs.extend(item for item in workspace.iterdir() if item.is_dir())
    for parent in search_dirs:
        nested = parent / candidate.name
        if nested.exists():
            return nested
    pending = workspace / candidate
    return pending


def _active_run_from_stream(text: str) -> Optional[str]:
    for raw in reversed((text or "").splitlines()):
        line = raw.strip()
        if not re.search(r"正在处理|processing|download|fetch", line, re.IGNORECASE):
            continue
        match = _RUN_ID_RE.search(line)
        if match:
            return match.group(0)
    return None


def _ordered_download_targets(
    workspace: Path,
    text: str,
    code: str,
) -> List[tuple[str, Path]]:
    seen: set[str] = set()
    ordered: List[tuple[str, Path]] = []

    def add_run(acc: str) -> None:
        if acc in seen:
            return
        for rel in (f"srna_fastq/{acc}.fastq.gz", f"{acc}.fastq.gz"):
            resolved = _resolve_workspace_path(workspace, rel)
            if resolved is not None:
                seen.add(acc)
                ordered.append((acc, resolved))
                return

    for match in _RUN_ID_RE.finditer(code or ""):
        add_run(match.group(0))

    if not ordered:
        for rel in _collect_download_paths(text, code):
            match = _RUN_ID_RE.search(Path(rel).name)
            if match:
                add_run(match.group(0))

    return ordered


def _progress_for_single_target(
    workspace: Path,
    path: Path,
    run: str,
    text: str,
    code: str,
) -> Dict[str, Any]:
    size = _download_bytes_on_disk(path)
    expected_total = _expected_download_total(
        workspace,
        run,
        text,
        code,
        filename=path.name,
    )
    result: Dict[str, Any] = {
        "isDownloadTask": True,
        "progressRun": run,
        "progressBytes": size,
    }
    if expected_total > 0:
        pct = min(100.0, size / expected_total * 100.0)
        result["progressOverallPct"] = pct
        result["progressFilePct"] = pct
        result["progressBytesTotal"] = expected_total
        result["progressLabel"] = (
            f"{run} · {pct:.1f}% · {_format_bytes(size)} / {_format_bytes(expected_total)}"
        )
    elif size > 0:
        result["progressIndeterminate"] = True
        result["progressLabel"] = f"{run} · 已下载 {_format_bytes(size)}"
    else:
        result["progressOverallPct"] = 0.0
        result["progressLabel"] = f"{run} · 等待下载数据…"
    return result


def _infer_file_download_progress(
    workspace: Optional[Path],
    text: str,
    code: str = "",
) -> Dict[str, Any]:
    if workspace is None or not _code_is_download_task(code):
        return {}

    targets = _ordered_download_targets(workspace, text, code)
    if len(targets) == 1:
        acc, path = targets[0]
        return _progress_for_single_target(workspace, path, acc, text, code)

    if len(targets) > 1:
        stage_run = _active_run_from_stream(text)
        active_acc: Optional[str] = None
        active_path: Optional[Path] = None

        if stage_run:
            for acc, path in targets:
                if acc == stage_run:
                    active_acc, active_path = acc, path
                    break

        if active_path is None:
            for acc, path in targets:
                expected = _expected_download_total(
                    workspace, acc, text, code, filename=path.name,
                )
                got = _download_bytes_on_disk(path)
                if expected <= 0 or got < expected:
                    active_acc, active_path = acc, path
                    break

        if active_path is None:
            active_acc, active_path = targets[-1]

        total_expected = 0
        total_bytes = 0
        for acc, path in targets:
            expected = _expected_download_total(
                workspace, acc, text, code, filename=path.name,
            )
            got = _download_bytes_on_disk(path)
            if expected > 0:
                total_expected += expected
                total_bytes += min(got, expected)
            else:
                total_bytes += got

        active_expected = _expected_download_total(
            workspace,
            active_acc,
            text,
            code,
            filename=active_path.name,
        )
        active_got = _download_bytes_on_disk(active_path)
        file_index = next(i for i, (acc, _) in enumerate(targets, start=1) if acc == active_acc)
        file_total = len(targets)

        result: Dict[str, Any] = {
            "isDownloadTask": True,
            "progressRun": active_acc,
            "progressFileIndex": file_index,
            "progressFileTotal": file_total,
            "progressBytes": active_got,
        }

        file_pct: Optional[float] = None
        if active_expected > 0:
            file_pct = min(100.0, active_got / active_expected * 100.0)
            result["progressFilePct"] = file_pct
            result["progressBytesTotal"] = active_expected

        if total_expected > 0:
            overall_pct = min(100.0, total_bytes / total_expected * 100.0)
            result["progressOverallPct"] = overall_pct
            if file_pct is not None:
                result["progressLabel"] = (
                    f"{active_acc} · {file_pct:.1f}% · 整体 {overall_pct:.1f}% · "
                    f"{_format_bytes(total_bytes)} / {_format_bytes(total_expected)} "
                    f"({file_index}/{file_total})"
                )
            else:
                result["progressLabel"] = (
                    f"{active_acc} · 整体 {overall_pct:.1f}% ({file_index}/{file_total})"
                )
        elif file_pct is not None:
            result["progressOverallPct"] = file_pct
            result["progressLabel"] = (
                f"{active_acc} · {file_pct:.1f}% · "
                f"{_format_bytes(active_got)} / {_format_bytes(active_expected)} "
                f"({file_index}/{file_total})"
            )
        elif active_got > 0:
            result["progressIndeterminate"] = True
            result["progressLabel"] = (
                f"{active_acc} · 已下载 {_format_bytes(active_got)} ({file_index}/{file_total})"
            )
        else:
            result["progressOverallPct"] = 0.0
            result["progressLabel"] = f"{active_acc} · 等待下载数据… ({file_index}/{file_total})"
        return result

    best_path: Optional[Path] = None
    best_size = -1
    pending_path: Optional[Path] = None
    pending_run: Optional[str] = None

    for rel in _collect_download_paths(text, code):
        resolved = _resolve_workspace_path(workspace, rel)
        if resolved is None:
            continue
        if not resolved.exists() and _download_bytes_on_disk(resolved) <= 0:
            if pending_path is None:
                pending_path = resolved
                match = _ACCESSION_RE.search(resolved.name)
                pending_run = match.group(0) if match else resolved.stem
            continue
        size = _download_bytes_on_disk(resolved)
        if size > best_size:
            best_size = size
            best_path = resolved

    if best_path is None:
        if pending_path is None:
            return {}
        run = pending_run or pending_path.stem
        return {
            "isDownloadTask": True,
            "progressRun": run,
            "progressBytes": 0,
            "progressOverallPct": 0.0,
            "progressLabel": f"{run} · 等待下载数据…",
        }

    run_match = _ACCESSION_RE.search(best_path.name)
    run = run_match.group(0) if run_match else best_path.stem
    return _progress_for_single_target(workspace, best_path, run, text, code)


def _has_download_progress_fields(parsed: Dict[str, Any]) -> bool:
    if not parsed.get("isDownloadTask"):
        return False
    if parsed.get("progressOverallPct") is not None:
        return True
    if int(parsed.get("progressBytes") or 0) > 0:
        return True
    return bool(parsed.get("progressRun"))


def _merge_execution_progress(
    parsed: Dict[str, Any],
    workspace: Optional[Path],
    text: str,
    code: str = "",
) -> Dict[str, Any]:
    merged = dict(parsed)
    if _parse_download_progress_marker(text):
        merged["isDownloadTask"] = True
    elif _code_is_download_task(code):
        merged["isDownloadTask"] = True
    elif _DOWNLOAD_LOG_RE.search(text):
        merged["isDownloadTask"] = True
    else:
        merged.pop("isDownloadTask", None)
        for key in (
            "progressOverallPct",
            "progressFilePct",
            "progressRun",
            "progressFileIndex",
            "progressFileTotal",
            "progressBytes",
            "progressBytesTotal",
            "progressLabel",
            "progressIndeterminate",
        ):
            merged.pop(key, None)
        return merged

    file_progress = _infer_file_download_progress(workspace, text, code)
    if not file_progress:
        return merged
    marker = _parse_download_progress_marker(text)
    if marker.get("progressOverallPct") is not None and not file_progress.get("progressFileTotal"):
        merged["isDownloadTask"] = True
        merged.update({k: v for k, v in marker.items() if v is not None})
    elif merged.get("progressOverallPct") is None:
        merged.update({k: v for k, v in file_progress.items() if v is not None})
    else:
        merged.update({k: v for k, v in file_progress.items() if v is not None})
    if merged.get("isDownloadTask"):
        merged["highlights"] = []
        if merged.get("progressLabel"):
            merged["detail"] = str(merged["progressLabel"])
    return merged


def _parse_progress_output(text: str, *, workspace: Optional[Path] = None, code: str = "") -> Dict[str, Any]:
    cleaned = _strip_terminal_noise(text)
    if not cleaned:
        return {"stage": "等待输出", "highlights": [], "detail": ""}

    candidates: List[str] = []
    seen_keys: set[str] = set()
    for raw in cleaned.splitlines():
        line = _clean_log_line(raw)
        if _is_noise_line(line):
            continue
        key = re.sub(r"[^\w]+", "", line.lower())
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        candidates.append(line)

    highlights: List[str] = []
    for line in candidates:
        score = 0
        if _ACCESSION_RE.search(line):
            score += 3
        if _SIZE_RE.search(line):
            score += 2
        if re.search(r"download|working|fetch|complete|success|error|warning|retry", line, re.IGNORECASE):
            score += 2
        if score > 0 or len(highlights) < 3:
            highlights.append(line)

    highlights = highlights[-4:]
    stage = _infer_progress_stage(highlights or candidates)
    detail = _truncate_result("\n".join(highlights or candidates[-2:]), 180)
    base = {
        "stage": stage,
        "highlights": highlights,
        "detail": detail,
        **_parse_download_progress_marker(text),
    }
    return _merge_execution_progress(base, workspace, text, code)


def _progress_snippet(text: str, limit: int = 240) -> str:
    parsed = _parse_progress_output(text)
    if parsed["highlights"]:
        return _truncate_result("\n".join(parsed["highlights"]), limit)
    return parsed["detail"]


def _next_progress_wait(index: int) -> int:
    if index < len(_CODE_PROGRESS_INTERVALS):
        return _CODE_PROGRESS_INTERVALS[index]
    return _CODE_PROGRESS_INTERVALS[-1]


def _resolve_answer_text(completion: Any) -> str:
    """Prefer visible content; fall back when the model only returns thinking blocks."""
    content = str(getattr(completion, "content", "") or "").strip()
    thinking = str(getattr(completion, "thinking", "") or "").strip()
    if content:
        return content
    return thinking


def _package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _project_root() -> Path:
    return _package_root().parent


def _build_system_prompt(skill_overview: str, extra_system: str = "") -> str:
    skills_block = skill_overview or "(no skills loaded)"
    base = (
        "You are sRNAgent, an assistant for small RNA-seq (sRNA-seq) analysis.\n\n"
        "## Workflow\n"
        "1. For multi-step tasks, call `search_skills` first to load workflow guidance.\n"
        "2. Before writing code, call `search_functions` to discover exact API signatures.\n"
        "3. Use `execute_code` to run Python. The namespace already includes "
        "`import sRNAgent as sa` as variable `sa`.\n"
        "4. Prefer `sa.fastq.fastq_dl(...)` and other registered sRNAgent APIs.\n"
        "5. When listing skills, ONLY mention skills from the Registered skills section below.\n"
        "6. Call `finish` with a concise summary when done.\n\n"
        "## Registered skills\n"
        f"{skills_block}\n"
    )
    extra = (extra_system or "").strip()
    if extra:
        return f"{base}\n## Additional instructions\n{extra}\n"
    return base


class SRNAgent:
    """Minimal agent runtime with search_functions / search_skills / execute_code."""

    def __init__(
        self,
        llm_config: Optional[LLMConfig] = None,
        cwd: Optional[Path] = None,
        max_turns: int = 100,
        extra_system_prompt: str = "",
        execution_config: Optional[ExecutionConfig] = None,
        execution_backend: Optional[ExecutionBackend] = None,
    ):
        self.project_root = _project_root()
        self.cwd = cwd or Path.cwd()
        self.max_turns = max_turns
        self.llm = ChatClient(llm_config or LLMConfig.from_env())

        exec_cfg = execution_config or ExecutionConfig(
            use_notebook=True,
            strict_kernel_validation=False,
            strict_env_validation=False,
            sandbox_fallback_policy=SandboxFallbackPolicy.WARN_AND_FALLBACK,
        )

        if execution_backend is not None:
            self.function_registry, self.skill_registry, skill_overview = initialize_registries(
                cwd=self.cwd,
            )
            self.execution = execution_backend
        else:
            self.function_registry, self.skill_registry, skill_overview, self.execution = (
                initialize_agent_runtime(
                    project_root=self.project_root,
                    cwd=self.cwd,
                    execution_config=exec_cfg,
                )
            )
        self.system_prompt = _build_system_prompt(skill_overview, extra_system_prompt)

        env_name = self.execution.runtime.conda_env or "unknown"
        mode = "notebook" if self.execution.use_notebook else "in-process"
        logger.info(
            "SRNAgent ready: %d skills, execution=%s, conda=%s, kernel=%s",
            len(self.skill_registry.skill_metadata),
            mode,
            env_name,
            self.execution.runtime.kernel_name,
        )

    def dispatch_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        *,
        on_stream: Optional[StreamCallback] = None,
    ) -> str:
        if name == "search_functions":
            return search_functions(self.function_registry, arguments.get("query", ""))
        if name == "search_skills":
            return search_skills(self.skill_registry, arguments.get("query", ""))
        if name == "execute_code":
            return execute_code(
                arguments.get("code", ""),
                self.project_root,
                execution_backend=self.execution,
                on_stream=on_stream,
            )
        if name == "finish":
            return arguments.get("message", "Done.")
        return f"Unknown tool: {name}"

    def _check_cancelled(self, cancel_event: Optional[Any]) -> None:
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            raise AgentCancelledError("Agent run cancelled.")

    def _llm_complete_cancellable(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        cancel_event: Optional[Any] = None,
        on_progress: Optional[ProgressCallback] = None,
        enable_thinking: Optional[bool] = None,
    ):
        if cancel_event is None:
            return self.llm.complete(messages, tools=tools, enable_thinking=enable_thinking)

        result_box: Dict[str, Any] = {"value": None, "error": None}
        started_at = time.time()
        last_heartbeat = started_at

        def worker() -> None:
            try:
                result_box["value"] = self.llm.complete(
                    messages,
                    tools=tools,
                    enable_thinking=enable_thinking,
                )
            except Exception as exc:  # noqa: BLE001
                result_box["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        while thread.is_alive():
            self._check_cancelled(cancel_event)
            now = time.time()
            if on_progress is not None and now - last_heartbeat >= 15:
                elapsed = int(now - started_at)
                self._emit_progress(
                    on_progress,
                    "status",
                    message=f"正在请求 LLM…（已等待 {elapsed}s）",
                )
                last_heartbeat = now
            thread.join(timeout=0.3)
        if result_box["error"] is not None:
            raise result_box["error"]
        return result_box["value"]

    def _interrupt_running_code(self) -> None:
        try:
            self.execution.interrupt()
        except Exception:
            pass

    def _emit_progress(
        self,
        on_progress: Optional[ProgressCallback],
        event_type: str,
        **payload: Any,
    ) -> None:
        if on_progress:
            on_progress({"type": event_type, **payload})

    def _execution_workspace(self) -> Optional[Path]:
        executor = getattr(self.execution, "notebook_executor", None)
        if executor is not None:
            workspace = getattr(executor, "workspace_dir", None)
            if workspace:
                return Path(workspace)
        try:
            return Path(self.cwd)
        except Exception:
            return None

    @staticmethod
    def _progress_payload_from_parsed(parsed: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "isDownloadTask": bool(parsed.get("isDownloadTask")),
            "progressOverallPct": parsed.get("progressOverallPct"),
            "progressFilePct": parsed.get("progressFilePct"),
            "progressRun": parsed.get("progressRun"),
            "progressFileIndex": parsed.get("progressFileIndex"),
            "progressFileTotal": parsed.get("progressFileTotal"),
            "progressBytes": parsed.get("progressBytes"),
            "progressBytesTotal": parsed.get("progressBytesTotal"),
            "progressLabel": parsed.get("progressLabel"),
            "progressIndeterminate": parsed.get("progressIndeterminate"),
        }
        if not payload["isDownloadTask"]:
            for key in list(payload.keys()):
                if key != "isDownloadTask":
                    payload[key] = None
        return payload

    def _run_execute_code_with_progress(
        self,
        arguments: Dict[str, Any],
        *,
        on_progress: Optional[ProgressCallback],
        cancel_event: Optional[Any],
        turn: int,
        summary: str,
        description: str,
    ) -> str:
        stream_state = {"stdout": "", "stderr": ""}
        result_box: Dict[str, Any] = {"value": None, "error": None}
        last_stream_progress = [0.0]
        start_box: List[Optional[float]] = [None]
        code_text = str(arguments.get("code") or "")
        workspace = self._execution_workspace()

        def on_stream(kind: str, text: str) -> None:
            key = "stderr" if kind == "stderr" else "stdout"
            stream_state[key] += text or ""
            if not on_progress:
                return
            now = time.monotonic()
            if start_box[0] is None:
                start_box[0] = now
            if now - last_stream_progress[0] < 0.4:
                return
            combined = stream_state["stdout"] or stream_state["stderr"]
            parsed = _parse_progress_output(combined, workspace=workspace, code=code_text)
            if not _has_download_progress_fields(parsed):
                return
            last_stream_progress[0] = now
            elapsed = now - start_box[0]
            self._emit_progress(
                on_progress,
                "code_execution_progress",
                turn=turn,
                summary=summary,
                description=description,
                elapsedSec=int(elapsed),
                elapsedLabel=_format_elapsed(elapsed),
                stage=parsed.get("stage") or "下载中",
                highlights=parsed.get("highlights") or [],
                snippet=parsed.get("detail") or "",
                nextUpdateSec=1,
                **self._progress_payload_from_parsed(parsed),
            )

        def worker() -> None:
            try:
                result_box["value"] = self.dispatch_tool(
                    "execute_code",
                    arguments,
                    on_stream=on_stream,
                )
            except Exception as exc:  # noqa: BLE001
                result_box["error"] = exc

        self._emit_progress(
            on_progress,
            "code_execution_started",
            turn=turn,
            summary=summary,
            description=description,
            code=str(arguments.get("code") or ""),
            stage="已开始",
            nextUpdateSec=_next_progress_wait(0),
        )

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        start = time.monotonic()
        start_box[0] = start
        next_fire = start + _next_progress_wait(0)
        progress_index = 0
        last_sse_heartbeat = start

        while thread.is_alive():
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                self._interrupt_running_code()
                raise AgentCancelledError("Agent run cancelled.")
            thread.join(timeout=1.0)
            if not thread.is_alive():
                break

            now = time.monotonic()
            if on_progress and now - last_sse_heartbeat >= _SSE_PROGRESS_HEARTBEAT_SEC:
                elapsed = now - start
                raw_stream = stream_state["stdout"] or stream_state["stderr"]
                parsed = _parse_progress_output(raw_stream, workspace=workspace, code=code_text)
                self._emit_progress(
                    on_progress,
                    "code_execution_progress",
                    turn=turn,
                    summary=summary,
                    description=description,
                    elapsedSec=int(elapsed),
                    elapsedLabel=_format_elapsed(elapsed),
                    stage=parsed["stage"] or "运行中",
                    highlights=parsed["highlights"],
                    snippet=parsed["detail"],
                    nextUpdateSec=_SSE_PROGRESS_HEARTBEAT_SEC,
                    **self._progress_payload_from_parsed(parsed),
                )
                last_sse_heartbeat = now

            if now < next_fire:
                continue

            elapsed = now - start
            raw_stream = stream_state["stdout"] or stream_state["stderr"]
            parsed = _parse_progress_output(raw_stream, workspace=workspace, code=code_text)
            self._emit_progress(
                on_progress,
                "code_execution_progress",
                turn=turn,
                summary=summary,
                description=description,
                elapsedSec=int(elapsed),
                elapsedLabel=_format_elapsed(elapsed),
                stage=parsed["stage"],
                highlights=parsed["highlights"],
                snippet=parsed["detail"],
                nextUpdateSec=_next_progress_wait(progress_index + 1),
                **self._progress_payload_from_parsed(parsed),
            )
            progress_index += 1
            next_fire = now + _next_progress_wait(progress_index)

        if result_box["error"] is not None:
            raise result_box["error"]
        return str(result_box["value"] or "")

    def _tool_loop(
        self,
        messages: List[Dict[str, Any]],
        *,
        on_progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[Any] = None,
        code_approval_callback: Optional[CodeApprovalCallback] = None,
    ) -> str:
        for turn in range(self.max_turns):
            self._check_cancelled(cancel_event)

            self._emit_progress(on_progress, "status", message="正在请求 LLM…")
            completion = self._llm_complete_cancellable(
                messages,
                tools=AGENT_TOOL_SCHEMAS,
                cancel_event=cancel_event,
                on_progress=on_progress,
                enable_thinking=False,
            )
            self._check_cancelled(cancel_event)

            if completion.tool_calls:
                thinking = str(completion.thinking or "").strip()
                visible = str(completion.content or "").strip()
                if thinking and thinking != visible:
                    self._emit_progress(
                        on_progress,
                        "thinking",
                        turn=turn + 1,
                        content=thinking,
                    )

                assistant_message: Dict[str, Any] = {
                    "role": "assistant",
                    "content": completion.content or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in completion.tool_calls
                    ],
                }
                messages.append(assistant_message)

                for call in completion.tool_calls:
                    self._check_cancelled(cancel_event)
                    summary = _summarize_tool_call(call.name, call.arguments)
                    self._emit_progress(
                        on_progress,
                        "tool_call",
                        turn=turn + 1,
                        name=call.name,
                        summary=summary,
                        arguments=call.arguments,
                    )

                    if call.name == "finish":
                        message = str(call.arguments.get("message", "Task completed."))
                        self._emit_progress(
                            on_progress,
                            "final",
                            turn=turn + 1,
                            content=message,
                        )
                        return message

                    if call.name == "execute_code":
                        code = str(call.arguments.get("code") or "")
                        description = str(call.arguments.get("description") or "")
                        approved = True
                        if code_approval_callback is not None:
                            request_id = str(uuid.uuid4())
                            approved = code_approval_callback(request_id, code, description)
                        if not approved:
                            result = (
                                "User denied code execution. Explain what the code would do "
                                "and ask whether to try again."
                            )
                        elif on_progress is not None:
                            result = self._run_execute_code_with_progress(
                                call.arguments,
                                on_progress=on_progress,
                                cancel_event=cancel_event,
                                turn=turn + 1,
                                summary=summary,
                                description=description,
                            )
                        else:
                            result = self.dispatch_tool(call.name, call.arguments)
                    else:
                        result = self.dispatch_tool(call.name, call.arguments)

                    if call.name == "execute_code":
                        self._emit_progress(
                            on_progress,
                            "tool_result",
                            turn=turn + 1,
                            name=call.name,
                            summary=summary,
                            content=_truncate_result(result),
                        )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": result,
                        }
                    )
                continue

            answer = _resolve_answer_text(completion)
            if answer:
                self._emit_progress(
                    on_progress,
                    "final",
                    turn=turn + 1,
                    content=answer,
                )
                return answer
            return "Agent stopped without a final response."

        message = "Agent reached max turns without calling finish."
        self._emit_progress(on_progress, "final", content=message)
        return message

    def run(self, user_query: str) -> str:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_query},
        ]
        return self._tool_loop(messages)

    def run_with_history(
        self,
        history: List[Dict[str, str]],
        *,
        on_progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[Any] = None,
        code_approval_callback: Optional[CodeApprovalCallback] = None,
    ) -> str:
        messages: List[Dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        for item in history:
            role = item.get("role")
            content = str(item.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        if not any(m.get("role") == "user" for m in messages):
            raise ValueError("No user message in history")
        return self._tool_loop(
            messages,
            on_progress=on_progress,
            cancel_event=cancel_event,
            code_approval_callback=code_approval_callback,
        )

    def run_planned(
        self,
        history: List[Dict[str, str]],
        *,
        extra_context: str = "",
        chat_id: str = "",
        save_plan: Optional[Any] = None,
        load_plan: Optional[Any] = None,
        on_progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[Any] = None,
        code_approval_callback: Optional[CodeApprovalCallback] = None,
    ) -> str:
        """Plan-and-Execute: planner splits work; each step gets its own tool-loop budget."""
        orchestrator = PlanOrchestrator(
            self,
            chat_id=chat_id,
            save_plan=save_plan,
            load_plan=load_plan,
        )
        return orchestrator.run(
            history,
            extra_context=extra_context,
            on_progress=on_progress,
            cancel_event=cancel_event,
            code_approval_callback=code_approval_callback,
        )

    def status(self) -> Dict[str, Any]:
        return {
            "skills": list(self.skill_registry.skill_metadata.keys()),
            "skill_overview": list_available_skills(self.skill_registry),
            "functions_sample": [
                entry.get("full_name")
                for entry in self.function_registry.find("fastq")[:5]
            ],
            "execution": self.execution.to_dict(),
        }
