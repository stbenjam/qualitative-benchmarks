from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import re
import shutil
import statistics
import subprocess
import tarfile
import tomllib
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import quote

import httpx
import pygments
from pygments import lex
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.lexers.python import PythonLexer
from pygments.token import Comment, String
from pygments.util import ClassNotFound


Category = Literal["greenfield", "repair"]
SUPABASE_URL = "https://ofhuhcpkvzjlejydnvyd.supabase.co"
SUPABASE_KEY = "sb_publishable_Z-vuQbpvpG-PStjbh4yE0Q_e-d3MTIH"
HUB_URL = "https://hub.harborframework.com"
DATASET = "terminal-bench/terminal-bench"
DATASET_VERSION = "latest"
LEADERBOARD = "4-0-0"
PAGE_SIZE = 1000
COMMENT_WORD = re.compile(r"\b[\w'-]+\b")
PROSE_WORD = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")
PROSE_SENTENCE_END = re.compile(r"[.!?]+(?:[\"')\]]+)?(?=\s|$)")
COMMENT_OPEN = re.compile(
    r"^\s*(?://+|\#+|/\*+|\*+|--+|;+|%+|<!--|\(\*+|\"\"\"|''')\s*"
)
COMMENT_CLOSE = re.compile(r"\s*(?:\*/|-->|\*\)|\"\"\"|''')\s*$")
MIN_READABILITY_WORDS = 100
MIN_READABILITY_SENTENCES = 5


@dataclass(frozen=True)
class PathRule:
    final: str
    baseline: str | None = None
    canonical_prefix: str = ""
    exact: bool = False
    complexity_suffix: str | None = None


@dataclass(frozen=True)
class TaskSpec:
    slug: str
    title: str
    category: Category
    language: str
    description: str
    scope: str
    suffixes: frozenset[str]
    rules: tuple[PathRule, ...]
    excluded_parts: frozenset[str] = frozenset()
    excluded_paths: frozenset[str] = frozenset()
    lexer_alias: str | None = None

    @property
    def task_name(self) -> str:
        return f"terminal-bench/{self.slug}"

    @property
    def metric_label(self) -> str:
        return "Solution SLOC" if self.category == "greenfield" else "Changed SLOC"


def exact(
    final: str,
    baseline: str | None = None,
    *,
    canonical: str = "",
    complexity_suffix: str | None = None,
) -> PathRule:
    return PathRule(
        final=final,
        baseline=baseline,
        canonical_prefix=canonical,
        exact=True,
        complexity_suffix=complexity_suffix,
    )


def prefix(
    final: str,
    baseline: str | None = None,
    *,
    canonical: str = "",
) -> PathRule:
    return PathRule(
        final=final,
        baseline=baseline,
        canonical_prefix=canonical,
    )


PY = frozenset({".py"})
PY_CACHE = frozenset({"__pycache__", ".pytest_cache"})
C_CPP = frozenset({".c", ".cc", ".cpp", ".h", ".hpp"})
TS_JS = frozenset({".ts", ".tsx", ".js", ".jsx"})


# Every included task has an explicit, artifact-backed source deliverable. The
# remaining 34 Terminal-Bench 4.0 tasks either deliver data/binaries/live state,
# or need patch reconstruction/an external baseline before churn is comparable.
SOURCE_TASKS = (
    TaskSpec(
        "retro-console-soc",
        "Retro Console SoC",
        "greenfield",
        "Verilog",
        "Synthesizable 8-bit console SoC.",
        "/app/src/**/*.{v,vh,sv,svh}",
        frozenset({".v", ".vh", ".sv", ".svh"}),
        (prefix("artifacts/app/src/"),),
        lexer_alias="verilog",
    ),
    TaskSpec(
        "interleaved-vigenere",
        "Interleaved Vigenère",
        "greenfield",
        "Python",
        "Standalone cipher-cracking CLI.",
        "/app/cracker.py",
        PY,
        (exact("artifacts/app/cracker.py"),),
    ),
    TaskSpec(
        "rs-archive-clone",
        "RS Archive Clone",
        "greenfield",
        "Python",
        "Black-box-compatible archive utility.",
        "/app/archive-clone",
        frozenset({""}),
        (
            exact(
                "artifacts/app/archive-clone",
                canonical="archive-clone",
                complexity_suffix=".py",
            ),
        ),
        lexer_alias="python",
    ),
    TaskSpec(
        "distributed-dedup",
        "Distributed Dedup",
        "repair",
        "Scala",
        "Spark near-duplicate clustering pipeline.",
        "Changes to SubmissionDedup.scala",
        frozenset({".scala"}),
        (
            exact(
                "artifacts/app/submission/src/main/scala/tb/dedup/submission/SubmissionDedup.scala",
                "environment/app/submission/src/main/scala/tb/dedup/submission/SubmissionDedup.scala",
            ),
        ),
    ),
    TaskSpec(
        "vf2-speedup-networkx",
        "VF2++ Speedup",
        "greenfield",
        "Python + C++",
        "Faster NetworkX-compatible graph isomorphism subset.",
        "/app/fast_networkx/**/* + /app/setup.py",
        PY | C_CPP,
        (
            exact("artifacts/app/setup.py"),
            prefix("artifacts/app/fast_networkx/", canonical="fast_networkx/"),
        ),
        frozenset({"igraph", "node_modules", "vendor"}),
    ),
    TaskSpec(
        "biped-contact-dynamics",
        "Biped Contact Dynamics",
        "repair",
        "Python",
        "Contact-dynamics trajectory generator.",
        "Changes to /app/submission/solve.py",
        PY,
        (
            exact(
                "artifacts/app/submission/solve.py",
                "environment/data/submission/solve.py",
            ),
        ),
    ),
    TaskSpec(
        "data-anonymization",
        "Data Anonymization",
        "greenfield",
        "Python",
        "Policy-driven anonymization program.",
        "/app/anon.py",
        PY,
        (exact("artifacts/app/anon.py"),),
    ),
    TaskSpec(
        "formal-crypto",
        "Formal Crypto",
        "greenfield",
        "SageMath",
        "SageMath cryptanalysis program.",
        "/app/solve.sage",
        frozenset({".sage"}),
        (
            exact(
                "artifacts/app/solve.sage",
                canonical="solve.sage",
                complexity_suffix=".py",
            ),
        ),
        lexer_alias="python",
    ),
    TaskSpec(
        "fp8-rmsnorm-gemm",
        "FP8 RMSNorm GEMM",
        "repair",
        "CUDA C++",
        "Fused GPU kernel submission.",
        "Changes to /app/fp8_rmsnorm_gemm.cu",
        frozenset({".cu"}),
        (
            exact(
                "artifacts/app/fp8_rmsnorm_gemm.cu",
                "environment/data/fp8_rmsnorm_gemm.cu",
                canonical="fp8_rmsnorm_gemm.cu",
                complexity_suffix=".cpp",
            ),
        ),
        lexer_alias="cuda",
    ),
    TaskSpec(
        "freecad-impeller",
        "FreeCAD Impeller",
        "greenfield",
        "Python",
        "Parametric impeller construction program.",
        "/app/answer.py",
        PY,
        (exact("artifacts/app/answer.py"),),
    ),
    TaskSpec(
        "freecad-platform-drawing",
        "FreeCAD Platform Drawing",
        "greenfield",
        "Python",
        "Parametric platform construction program.",
        "/app/build_part.py",
        PY,
        (exact("artifacts/app/build_part.py"),),
    ),
    TaskSpec(
        "freecad-spring-clip",
        "FreeCAD Spring Clip",
        "greenfield",
        "Python",
        "Parametric spring-clip construction program.",
        "/app/answer.py",
        PY,
        (exact("artifacts/app/answer.py"),),
    ),
    TaskSpec(
        "freight-dispatch-shift",
        "Freight Dispatch Shift",
        "greenfield",
        "Python",
        "Stateful freight-dispatch CLI.",
        "/workspace/dispatch",
        frozenset({""}),
        (
            exact(
                "artifacts/workspace/dispatch",
                canonical="dispatch",
                complexity_suffix=".py",
            ),
        ),
        lexer_alias="python",
    ),
    TaskSpec(
        "html-js-filter",
        "HTML / JS Filter",
        "greenfield",
        "Python",
        "HTML and JavaScript filtering program.",
        "/app/filter.py",
        PY,
        (exact("artifacts/app/filter.py"),),
    ),
    TaskSpec(
        "jax-speedrun-gpu",
        "JAX Speedrun GPU",
        "repair",
        "Python / JAX",
        "GPU training recipe and model implementation.",
        "Python changes under /app",
        PY,
        (prefix("artifacts/app/", "environment/"),),
        PY_CACHE,
    ),
    TaskSpec(
        "ks-solver-cpp",
        "KS Solver C++",
        "greenfield",
        "C++",
        "Kuramoto–Sivashinsky solver.",
        "/app/**/*.{c,cc,cpp,h,hpp}",
        C_CPP,
        (prefix("artifacts/app/"),),
        frozenset({"build", "cmake-build-debug", "cmake-build-release"}),
        frozenset({"artifacts/app/oracle.hpp"}),
    ),
    TaskSpec(
        "math-eval-grader",
        "Math Eval Grader",
        "greenfield",
        "Python",
        "Free-form mathematical equivalence grader.",
        "/app/grader.py",
        PY,
        (exact("artifacts/app/grader.py"),),
    ),
    TaskSpec(
        "ontology-kg-querying",
        "Ontology KG Querying",
        "greenfield",
        "Python + SPARQL",
        "Knowledge-graph integration and query pipeline.",
        "/app/pipeline.py + /app/*.rq",
        frozenset({".py", ".rq"}),
        (
            exact("artifacts/app/pipeline.py"),
            exact("artifacts/app/cross_border_operational_points.rq"),
            exact("artifacts/app/qualified_cross_border_points.rq"),
        ),
    ),
    TaskSpec(
        "vba-userform-port",
        "VBA UserForm Port",
        "greenfield",
        "Python + JavaScript",
        "Delivered web application replacing a VBA user form.",
        "/workspace/generated_app source",
        frozenset({".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".sh"}),
        (prefix("artifacts/workspace/generated_app/"),),
        frozenset(
            {
                "node_modules",
                ".venv",
                ".vite",
                ".git",
                "__pycache__",
                ".pytest_cache",
                ".npm",
                ".cache",
            }
        ),
    ),
    TaskSpec(
        "react-lead-form",
        "React Lead Form",
        "repair",
        "TypeScript",
        "Lead validation, reconciliation, and CRM export repair.",
        "Source changes under /app/src",
        TS_JS,
        (prefix("artifacts/app/src/", "environment/app/src/"),),
        frozenset({"node_modules", "dist", "tests"}),
    ),
    TaskSpec(
        "batched-eval-parity",
        "Batched Eval Parity",
        "repair",
        "Python",
        "Batched evaluation parity repair.",
        "Source changes under /app/evalbench",
        PY,
        (prefix("artifacts/app/evalbench/", "environment/evalbench/"),),
        PY_CACHE,
    ),
    TaskSpec(
        "payments-pipeline-fix",
        "Payments Pipeline Fix",
        "repair",
        "Python",
        "Kafka payments worker startup repair.",
        "Source changes under /app/src",
        PY,
        (prefix("artifacts/app/src/", "environment/src/"),),
        PY_CACHE,
    ),
    TaskSpec(
        "wal-recovery-ordering",
        "WAL Recovery Ordering",
        "repair",
        "Python",
        "Snapshot and write-ahead-log recovery repair.",
        "Python source changes under /app",
        PY,
        (prefix("artifacts/app/", "environment/app/"),),
        frozenset({"tests", "__pycache__"}),
    ),
    TaskSpec(
        "mvcc-lsm-compaction",
        "MVCC LSM Compaction",
        "repair",
        "C++",
        "MVCC snapshots, flushes, and compaction repair.",
        "C++ source changes under /app, including regression_test.cc",
        C_CPP,
        (prefix("artifacts/app/", "environment/app/"),),
        frozenset({"build"}),
    ),
    TaskSpec(
        "bun-sourcemap-leak",
        "Bun Sourcemap Leak",
        "repair",
        "TypeScript",
        "Release-bundle sourcemap leak repair.",
        "/app/scripts and /app/src source changes",
        TS_JS,
        (
            prefix(
                "artifacts/app/scripts/",
                "environment/scripts/",
                canonical="scripts/",
            ),
            prefix("artifacts/app/src/", "environment/src/", canonical="src/"),
        ),
        frozenset({"dist", "node_modules"}),
    ),
    TaskSpec(
        "cargo-flight-dispatch",
        "Cargo Flight Dispatch",
        "repair",
        "Python",
        "Aircraft and route dispatch repair.",
        "navigation.py, aircraft.py, and dispatch.py",
        PY,
        (
            exact("artifacts/app/navigation.py", "environment/navigation.py"),
            exact("artifacts/app/aircraft.py", "environment/aircraft.py"),
            exact("artifacts/app/dispatch.py", "environment/dispatch.py"),
        ),
    ),
    TaskSpec(
        "coq-block-bound",
        "Coq Block Bound",
        "repair",
        "Coq",
        "Gallina proof repair and helper definitions.",
        "/app/**/*.v",
        frozenset({".v"}),
        (prefix("artifacts/app/", "environment/"),),
        frozenset({"_build", ".git"}),
        lexer_alias="coq",
    ),
    TaskSpec(
        "embedding-drift-monitor",
        "Embedding Drift Monitor",
        "repair",
        "Python",
        "Embedding drift-monitor implementation repair.",
        "/app/drift_monitor/**/*.py",
        PY,
        (
            prefix(
                "artifacts/app/drift_monitor/",
                "environment/drift_monitor/",
                canonical="drift_monitor/",
            ),
        ),
        PY_CACHE,
    ),
    TaskSpec(
        "nextjs-performance",
        "Next.js Performance",
        "repair",
        "TypeScript + CSS",
        "Next.js rendering and layout performance repair.",
        "/app/app, /app/components, /app/lib, and source config",
        TS_JS | frozenset({".css"}),
        (
            prefix("artifacts/app/app/", "environment/app/app/", canonical="app/"),
            prefix(
                "artifacts/app/components/",
                "environment/app/components/",
                canonical="components/",
            ),
            prefix("artifacts/app/lib/", "environment/app/lib/", canonical="lib/"),
            exact("artifacts/app/next-env.d.ts", "environment/app/next-env.d.ts"),
            exact("artifacts/app/next.config.ts", "environment/app/next.config.ts"),
        ),
        frozenset({"node_modules", ".next", "dist", "tests"}),
    ),
    TaskSpec(
        "risk-scorer-replay",
        "Risk Scorer Replay",
        "repair",
        "Python",
        "Deterministic risk-scoring replay repair.",
        "/app/parityctl/**/*.py",
        PY,
        (
            prefix(
                "artifacts/app/parityctl/",
                "environment/app/parityctl/",
                canonical="parityctl/",
            ),
        ),
        PY_CACHE,
    ),
    TaskSpec(
        "session-window-debug",
        "Session Window Debug",
        "repair",
        "Python",
        "Session-window merging and garbage-collection repair.",
        "/app/app/**/*.py",
        PY,
        (prefix("artifacts/app/app/", "environment/app/"),),
        PY_CACHE,
    ),
    TaskSpec(
        "takens-embedding-lean",
        "Takens Embedding Lean",
        "repair",
        "Lean 4",
        "Lean proof repair and helper lemmas.",
        "/task/GSLean/Takens/**/*.lean",
        frozenset({".lean"}),
        (
            prefix(
                "artifacts/task/GSLean/Takens/",
                "environment/GSLean/Takens/",
                canonical="GSLean/Takens/",
            ),
        ),
        frozenset({".lake", "build"}),
        frozenset(
            {
                "artifacts/task/GSLean/Takens/Core.lean",
                "environment/GSLean/Takens/Core.lean",
            }
        ),
    ),
)

# These repair tasks produce source, but comparable churn needs either applying
# a patch artifact or obtaining a baseline that is not exported with the task.
DEFERRED_REPAIR_TASKS = frozenset(
    {
        "cumulative-layout-shift",
        "live-database-cutover",
        "sglang-qwen-burst",
        "vllm-deepseek-streaming",
        "vpp-loss-divergence",
    }
)

# These tasks deliver data, models, binaries, documents, or live environment
# state rather than a source artifact that can be compared with this report.
NON_SOURCE_TASKS = frozenset(
    {
        "atrx-vep-crispr",
        "cad-model",
        "ctr-optimization",
        "fin-saccr-rwa",
        "foodstuff-beta-activity",
        "glycan-ms2-elucidation",
        "gsea-proteomics",
        "heat-pump-warranty",
        "hof-topology-interpenetration",
        "intrastat-meldung",
        "kv-live-surgery",
        "lake-temp-glm",
        "layout-config-recreation",
        "layout-config-recreation2",
        "legacy-utility-triage",
        "medical-claims-processing",
        "mp-checkpoint-consolidation",
        "music-harmony",
        "photonic-waveguide-routing",
        "pretrain-shard-corruption",
        "production-planning",
        "protein-autointerp-disulfide",
        "roy-polymorph-cn",
        "satb-audio-transcription",
        "shadow-relay",
        "sound-change-cascade",
        "telecom-entity-resolution",
        "uefi-bootkit",
        "wdm-design",
    }
)

TASKS = SOURCE_TASKS

SOURCE_SLUGS = frozenset(spec.slug for spec in SOURCE_TASKS)
if len(SOURCE_SLUGS) != len(SOURCE_TASKS):
    raise RuntimeError("The source task manifest contains duplicate slugs")
if SOURCE_SLUGS & (DEFERRED_REPAIR_TASKS | NON_SOURCE_TASKS):
    raise RuntimeError("Terminal-Bench task partitions overlap")
if len(SOURCE_SLUGS | DEFERRED_REPAIR_TASKS | NON_SOURCE_TASKS) != 66:
    raise RuntimeError("Terminal-Bench task partitions must cover all 66 tasks")


@dataclass
class LineMetrics:
    code: list[bool]
    code_bytes: list[int]
    code_tokens: list[int]
    comment_words: list[int]
    comment_text: list[str]


def _round(value: float) -> float:
    return round(value, 1)


def _lexer_for_path(path: str, lexer_alias: str | None):
    if lexer_alias:
        if lexer_alias == "verilog" and PurePosixPath(path).suffix.lower() in {
            ".sv",
            ".svh",
        }:
            lexer_alias = "systemverilog"
        return get_lexer_by_name(lexer_alias, stripnl=False, ensurenl=False)
    if not PurePosixPath(path).suffix:
        return PythonLexer(stripnl=False, ensurenl=False)
    try:
        return get_lexer_for_filename(path, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return PythonLexer(stripnl=False, ensurenl=False)


def _is_prose_comment(token_type: Any) -> bool:
    regular_comment = (
        token_type in Comment
        and token_type not in Comment.Preproc
        and token_type not in Comment.Hashbang
    )
    return regular_comment or token_type in String.Doc


def _line_metrics(path: str, source: str, lexer_alias: str | None) -> LineMetrics:
    lines = source.splitlines()
    result = LineMetrics(
        code=[False] * len(lines),
        code_bytes=[0] * len(lines),
        code_tokens=[0] * len(lines),
        comment_words=[0] * len(lines),
        comment_text=[""] * len(lines),
    )
    line_index = 0

    for token_type, value in lex(source, _lexer_for_path(path, lexer_alias)):
        is_comment = _is_prose_comment(token_type)
        is_code = bool(value) and not is_comment and not value.isspace()
        token_recorded = False
        for piece in value.splitlines(keepends=True) or [value]:
            content = piece.rstrip("\r\n")
            if line_index < len(lines) and content:
                if is_comment:
                    result.comment_words[line_index] += len(
                        COMMENT_WORD.findall(content)
                    )
                    result.comment_text[line_index] += f" {content}"
                elif is_code:
                    result.code[line_index] = True
                    result.code_bytes[line_index] += len(content.encode("utf-8"))
                    if not token_recorded:
                        result.code_tokens[line_index] += 1
                        token_recorded = True
            line_index += piece.count("\n")
    return result


def _clean_comment_prose(lines: Sequence[str]) -> str:
    cleaned: list[str] = []
    for line in lines:
        text = COMMENT_OPEN.sub("", line, count=1)
        text = COMMENT_CLOSE.sub("", text, count=1).strip()
        if text:
            cleaned.append(text)
    return "\n".join(cleaned)


def _estimate_syllables(word: str) -> int:
    letters = re.sub(r"[^a-z]", "", word.lower())
    if not letters:
        return 0
    if len(letters) <= 3:
        return 1
    groups = len(re.findall(r"[aeiouy]+", letters))
    if letters.endswith("e") and not letters.endswith(("le", "ye")) and groups > 1:
        groups -= 1
    if (
        letters.endswith("ed")
        and len(letters) > 4
        and letters[-3] not in "td"
        and groups > 1
    ):
        groups -= 1
    return max(1, groups)


def _readability_metrics(comment_lines: Sequence[str]) -> dict[str, Any]:
    prose = _clean_comment_prose(comment_lines)
    words = PROSE_WORD.findall(prose)
    sentences = len(PROSE_SENTENCE_END.findall(prose))
    result: dict[str, Any] = {
        "comment_prose_words": len(words),
        "comment_prose_sentences": sentences,
        "ari_grade": None,
        "flesch_reading_ease": None,
    }
    if len(words) < MIN_READABILITY_WORDS or sentences < MIN_READABILITY_SENTENCES:
        return result
    characters = sum(sum(character.isalnum() for character in word) for word in words)
    syllables = sum(_estimate_syllables(word) for word in words)
    words_per_sentence = len(words) / sentences
    result["ari_grade"] = _round(
        4.71 * characters / len(words) + 0.5 * words_per_sentence - 21.43
    )
    result["flesch_reading_ease"] = _round(
        206.835 - 1.015 * words_per_sentence - 84.6 * syllables / len(words)
    )
    return result


def _rule_member(
    spec: TaskSpec, member_name: str, *, baseline: bool
) -> tuple[str, str | None] | None:
    member = PurePosixPath(member_name)
    if member_name in spec.excluded_paths:
        return None
    if any(part in spec.excluded_parts for part in member.parts):
        return None
    if member.suffix.lower() not in spec.suffixes:
        return None

    for rule in spec.rules:
        target = rule.baseline if baseline else rule.final
        if target is None:
            continue
        if rule.exact:
            if member_name != target:
                continue
            canonical = rule.canonical_prefix or member.name
        else:
            if not member_name.startswith(target):
                continue
            relative = member_name.removeprefix(target)
            canonical = f"{rule.canonical_prefix}{relative}"
        return canonical, rule.complexity_suffix
    return None


def _read_archive_sources(
    archive_path: Path, spec: TaskSpec
) -> tuple[dict[str, str], dict[str, str | None]]:
    sources: dict[str, str] = {}
    complexity_suffixes: dict[str, str | None] = {}
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            matched = _rule_member(spec, member.name, baseline=False)
            if matched is None:
                continue
            canonical, complexity_suffix = matched
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError(f"Could not read {member.name} from {archive_path}")
            sources[canonical] = extracted.read().decode("utf-8", errors="replace")
            complexity_suffixes[canonical] = complexity_suffix
    return sources, complexity_suffixes


def _archive_exports_artifacts(archive_path: Path) -> bool:
    with tarfile.open(archive_path, "r:gz") as archive:
        return any(
            member.isfile()
            and PurePosixPath(member.name).parts
            and PurePosixPath(member.name).parts[0] == "artifacts"
            for member in archive.getmembers()
        )


def _read_baseline_sources(
    dataset_dir: Path, spec: TaskSpec
) -> tuple[dict[str, str], dict[str, str | None]]:
    if spec.category == "greenfield":
        return {}, {}
    task_dir = dataset_dir / "terminal-bench" / spec.slug
    if not task_dir.is_dir():
        raise RuntimeError(f"Missing task package: {task_dir}")

    sources: dict[str, str] = {}
    complexity_suffixes: dict[str, str | None] = {}
    for path in task_dir.rglob("*"):
        if not path.is_file():
            continue
        member_name = path.relative_to(task_dir).as_posix()
        matched = _rule_member(spec, member_name, baseline=True)
        if matched is None:
            continue
        canonical, complexity_suffix = matched
        sources[canonical] = path.read_text(errors="replace")
        complexity_suffixes[canonical] = complexity_suffix
    return sources, complexity_suffixes


def _task_taxonomy(dataset_dir: Path, spec: TaskSpec) -> dict[str, Any]:
    task_path = dataset_dir / "terminal-bench" / spec.slug / "task.toml"
    document = tomllib.loads(task_path.read_text())
    metadata = document.get("metadata") or {}
    category = metadata.get("category")
    subcategory = metadata.get("subcategory")
    tags = metadata.get("tags") or []
    if not isinstance(category, str) or not category:
        raise RuntimeError(f"Missing Terminal-Bench category in {task_path}")
    if not isinstance(subcategory, str) or not subcategory:
        raise RuntimeError(f"Missing Terminal-Bench subcategory in {task_path}")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise RuntimeError(f"Invalid Terminal-Bench tags in {task_path}")
    return {
        "tb_category": category,
        "tb_subcategory": subcategory,
        "tb_tags": tags,
    }


def _write_snapshot(
    root: Path,
    sources: dict[str, str],
    complexity_suffixes: dict[str, str | None] | None = None,
) -> dict[str, str]:
    written: dict[str, str] = {}
    for canonical, source in sources.items():
        suffix = (complexity_suffixes or {}).get(canonical)
        snapshot_name = f"{canonical}{suffix}" if suffix else canonical
        target = root / snapshot_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
        written[canonical] = snapshot_name
    return written


def _measure_solution(sources: dict[str, str], spec: TaskSpec) -> dict[str, Any]:
    loc = code_bytes = code_tokens = comment_words = 0
    comment_lines: list[str] = []
    for path, source in sources.items():
        metrics = _line_metrics(path, source, spec.lexer_alias)
        loc += sum(metrics.code)
        code_bytes += sum(metrics.code_bytes)
        code_tokens += sum(metrics.code_tokens)
        comment_words += sum(metrics.comment_words)
        comment_lines.extend(metrics.comment_text)
    return {
        "loc": loc,
        "added_loc": loc,
        "deleted_loc": 0,
        "code_bytes": code_bytes,
        "code_tokens": code_tokens,
        "comment_words": comment_words,
        "comment_code_tokens": code_tokens,
        "file_count": len(sources),
        "touched_paths": sorted(sources),
        **_readability_metrics(comment_lines),
    }


def _measure_change(
    baseline: dict[str, str], final: dict[str, str], spec: TaskSpec
) -> dict[str, Any]:
    added = deleted = code_bytes = code_tokens = 0
    comment_words = comment_code_tokens = 0
    comment_lines: list[str] = []
    touched_paths: list[str] = []

    for path in sorted(baseline.keys() | final.keys()):
        old_source = baseline.get(path, "")
        new_source = final.get(path, "")
        if old_source == new_source:
            continue
        touched_paths.append(path)
        old_lines = old_source.splitlines()
        new_lines = new_source.splitlines()
        old_metrics = _line_metrics(path, old_source, spec.lexer_alias)
        new_metrics = _line_metrics(path, new_source, spec.lexer_alias)
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if tag == "equal":
                continue
            deleted += sum(old_metrics.code[old_start:old_end])
            added += sum(new_metrics.code[new_start:new_end])
            code_bytes += sum(old_metrics.code_bytes[old_start:old_end])
            code_bytes += sum(new_metrics.code_bytes[new_start:new_end])
            code_tokens += sum(old_metrics.code_tokens[old_start:old_end])
            code_tokens += sum(new_metrics.code_tokens[new_start:new_end])
            comment_words += sum(new_metrics.comment_words[new_start:new_end])
            comment_code_tokens += sum(new_metrics.code_tokens[new_start:new_end])
            comment_lines.extend(new_metrics.comment_text[new_start:new_end])

    return {
        "loc": added + deleted,
        "added_loc": added,
        "deleted_loc": deleted,
        "code_bytes": code_bytes,
        "code_tokens": code_tokens,
        "comment_words": comment_words,
        "comment_code_tokens": comment_code_tokens,
        "file_count": len(touched_paths),
        "touched_paths": touched_paths,
        **_readability_metrics(comment_lines),
    }


def _is_success(association: dict[str, Any]) -> bool:
    reward = ((association.get("trial") or {}).get("rewards") or {}).get("reward")
    return isinstance(reward, int | float) and float(reward) == 1


def _hub_trial_url(row_id: str, job_id: str, trial_id: str) -> str:
    return_path = (
        f"/datasets/{DATASET}/{DATASET_VERSION}/leaderboards/{LEADERBOARD}/"
        f"rows/{row_id}?tab=results"
    )
    return (
        f"{HUB_URL}/jobs/{job_id}/trials/{trial_id}"
        f"?return={quote(return_path, safe='')}"
    )


async def _request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            if attempt == 3:
                break
            await asyncio.sleep(0.5 * (2**attempt))
    raise RuntimeError(f"Request failed after retries: {url}") from last_error


async def _fetch_leaderboard(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await _request_with_retries(
        client,
        "POST",
        f"{SUPABASE_URL}/functions/v1/leaderboard-read",
        headers={"apikey": SUPABASE_KEY},
        json={
            "package": DATASET,
            "name": LEADERBOARD,
            "page": 1,
            "page_size": PAGE_SIZE,
        },
    )
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise RuntimeError("Unexpected leaderboard-read response")
    return payload


async def _fetch_associations(
    client: httpx.AsyncClient, row_ids: list[str]
) -> list[dict[str, Any]]:
    select = (
        "row_id,trial_id,trial:trial_id("
        "id,job_id,trial_name,task_name,archive_path,rewards,status,config)"
    )
    associations: list[dict[str, Any]] = []
    start = 0
    while True:
        response = await _request_with_retries(
            client,
            "GET",
            f"{SUPABASE_URL}/rest/v1/leaderboard_row_trial",
            headers={
                "apikey": SUPABASE_KEY,
                "Range": f"{start}-{start + PAGE_SIZE - 1}",
                "Prefer": "count=exact",
            },
            params={
                "select": select,
                "row_id": f"in.({','.join(row_ids)})",
                "order": "row_id.asc,trial_id.asc",
            },
        )
        page = response.json()
        if not isinstance(page, list):
            raise RuntimeError("Unexpected leaderboard trial response")
        associations.extend(item for item in page if isinstance(item, dict))
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return associations


async def _download_archives(
    client: httpx.AsyncClient,
    associations: list[dict[str, Any]],
    archive_dir: Path,
    *,
    refresh: bool,
    concurrency: int,
) -> None:
    semaphore = asyncio.Semaphore(concurrency)
    successes = [item for item in associations if _is_success(item)]
    archive_dir.mkdir(parents=True, exist_ok=True)

    async def download(item: dict[str, Any]) -> None:
        trial = item.get("trial") or {}
        trial_id = str(item["trial_id"])
        target = archive_dir / f"{trial_id}.tar.gz"
        if target.exists() and not refresh and tarfile.is_tarfile(target):
            return
        remote_path = trial.get("archive_path")
        if not isinstance(remote_path, str) or not remote_path:
            raise RuntimeError(f"Successful trial {trial_id} has no archive_path")
        url = f"{SUPABASE_URL}/storage/v1/object/authenticated/results/{remote_path}"
        async with semaphore:
            response = await _request_with_retries(
                client, "GET", url, headers={"apikey": SUPABASE_KEY}
            )
        target.write_bytes(response.content)
        if not tarfile.is_tarfile(target):
            target.unlink(missing_ok=True)
            raise RuntimeError(f"Downloaded archive is not a tar file: {trial_id}")

    async with asyncio.TaskGroup() as group:
        for item in successes:
            group.create_task(download(item))


def _ensure_dataset(work_dir: Path, *, refresh: bool) -> Path:
    dataset_dir = work_dir / "dataset"
    missing = [
        spec.slug
        for spec in TASKS
        if not (dataset_dir / "terminal-bench" / spec.slug / "task.toml").is_file()
    ]
    if not missing and not refresh:
        return dataset_dir
    harbor = shutil.which("harbor")
    if harbor is None:
        raise RuntimeError("harbor CLI is required to download the task packages")
    command = [
        harbor,
        "datasets",
        "download",
        f"{DATASET}@{DATASET_VERSION}",
        "--output-dir",
        str(dataset_dir),
        "--export",
    ]
    if refresh:
        command.append("--overwrite")
    subprocess.run(command, check=True)
    return dataset_dir


async def extract(work_dir: Path, *, refresh: bool, concurrency: int) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    _ensure_dataset(work_dir, refresh=refresh)
    timeout = httpx.Timeout(120.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        leaderboard = await _fetch_leaderboard(client)
        rows = leaderboard["rows"]
        row_ids = [str(row["id"]) for row in rows]
        associations = await _fetch_associations(client, row_ids)
        selected_names = {spec.task_name for spec in TASKS}
        selected = [
            item
            for item in associations
            if (item.get("trial") or {}).get("task_name") in selected_names
        ]
        expected = len(TASKS) * len(rows) * 5
        if len(selected) != expected:
            raise RuntimeError(
                f"Expected {expected} selected associations, found {len(selected)}"
            )
        snapshot = {
            "extracted_at": datetime.now(UTC).isoformat(),
            "leaderboard": leaderboard["leaderboard"],
            "rows": rows,
            "associations": selected,
        }
        metadata_path = work_dir / "trials.json"
        metadata_path.write_text(json.dumps(snapshot, indent=2) + "\n")
        await _download_archives(
            client,
            selected,
            work_dir / "archives",
            refresh=refresh,
            concurrency=concurrency,
        )
    return metadata_path


def _resolve_complexity_binary(work_dir: Path, configured: Path | None) -> Path | None:
    if configured is not None:
        return configured
    installed = shutil.which("rust-code-analysis-cli")
    if installed:
        return Path(installed)
    local = work_dir / "tools" / "bin" / "rust-code-analysis-cli"
    return local if local.is_file() else None


def install_metrics_tool(work_dir: Path) -> Path:
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("cargo is required for --install-metrics")
    root = work_dir / "tools"
    subprocess.run(
        [
            cargo,
            "install",
            "rust-code-analysis-cli",
            "--version",
            "0.0.25",
            "--root",
            str(root),
            "--locked",
        ],
        check=True,
    )
    return root / "bin" / "rust-code-analysis-cli"


def _run_complexity_tool(binary: Path, work_dir: Path) -> tuple[Path, str]:
    output_dir = work_dir / "complexity"
    output_dir.mkdir(parents=True, exist_ok=True)
    version = subprocess.run(
        [str(binary), "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            str(binary),
            "--paths",
            "snapshots",
            "--metrics",
            "--output-format",
            "json",
            "--output",
            "complexity",
        ],
        check=True,
        cwd=work_dir,
    )
    return output_dir, version


def _complexity_file(output_dir: Path, snapshot_name: str) -> dict[str, Any] | None:
    path = output_dir / "snapshots" / f"{snapshot_name}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    return data if isinstance(data, dict) else None


def _complexity_totals(
    output_dir: Path,
    snapshot_paths: dict[str, str],
    selected_paths: list[str],
) -> dict[str, Any]:
    cognitive = cyclomatic = 0.0
    supported = 0
    for canonical in selected_paths:
        snapshot_name = snapshot_paths.get(canonical)
        if snapshot_name is None:
            continue
        data = _complexity_file(output_dir, snapshot_name)
        if data is None:
            continue
        metrics = data.get("metrics") or {}
        cognitive += float((metrics.get("cognitive") or {}).get("sum", 0))
        cyclomatic += float((metrics.get("cyclomatic") or {}).get("sum", 0))
        supported += 1
    total = len(selected_paths)
    return {
        "cognitive_complexity": _round(cognitive) if supported else None,
        "cyclomatic_complexity": _round(cyclomatic) if supported else None,
        "complexity_files_supported": supported,
        "complexity_files_total": total,
        "complexity_coverage": _round(100 * supported / total) if total else None,
    }


def _model_identity(
    row: dict[str, Any], associations: list[dict[str, Any]]
) -> dict[str, str]:
    first = next(item for item in associations if item["row_id"] == row["id"])
    agent = ((first.get("trial") or {}).get("config") or {}).get("agent") or {}
    metadata = row.get("metadata") or {}
    model_display = (metadata.get("model_display") or {}).get("label")
    agent_display = (metadata.get("agent_display") or {}).get("label")
    reasoning_effort = str(metadata.get("reasoning_effort") or "")
    return {
        "model": str(agent.get("model_name") or model_display or row["id"]),
        "model_display": str(model_display or agent.get("model_name") or row["id"]),
        "agent": str(agent.get("name") or agent_display or ""),
        "agent_display": str(agent_display or agent.get("name") or ""),
        "reasoning_effort": reasoning_effort,
    }


def _mean(values: Sequence[int | float]) -> float | None:
    return _round(statistics.fmean(values)) if values else None


def _median(values: Sequence[int | float]) -> float | None:
    return _round(statistics.median(values)) if values else None


def _metric_index(value: float | None, best: float | None) -> float | None:
    if value is None or best is None:
        return None
    if best == 0:
        return 100.0 if value == 0 else None
    return _round(100 * value / best)


def _task_relative_indices(rows: list[dict[str, Any]], field: str) -> None:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    best = min(values) if values else None
    target = f"relative_{field.removeprefix('mean_')}_index"
    for row in rows:
        value = row.get(field)
        row[target] = _metric_index(float(value), best) if value is not None else None


def _attach_complexity(
    metrics: dict[str, Any],
    spec: TaskSpec,
    trial_id: str,
    complexity_output: Path,
    baseline_snapshot_paths: dict[str, str],
) -> None:
    selected_paths = metrics["touched_paths"]
    final_paths = {
        canonical: f"trials/{trial_id}/{snapshot_name}"
        for canonical, snapshot_name in metrics.pop("snapshot_paths").items()
    }
    final_complexity = _complexity_totals(
        complexity_output, final_paths, selected_paths
    )
    baseline_paths = {
        canonical: f"baselines/{spec.slug}/{snapshot_name}"
        for canonical, snapshot_name in baseline_snapshot_paths.items()
    }
    baseline_complexity = _complexity_totals(
        complexity_output, baseline_paths, selected_paths
    )
    all_deleted = bool(selected_paths) and not any(
        canonical in final_paths for canonical in selected_paths
    )
    if all_deleted:
        final_complexity.update(
            {
                "cognitive_complexity": 0.0,
                "cyclomatic_complexity": 0.0,
                "complexity_files_supported": 0,
                "complexity_files_total": 0,
                "complexity_coverage": None,
            }
        )
    metrics.update(final_complexity)
    all_new = bool(selected_paths) and not any(
        canonical in baseline_paths for canonical in selected_paths
    )
    for metric_name in ("cognitive_complexity", "cyclomatic_complexity"):
        final_value = metrics[metric_name]
        baseline_value = baseline_complexity[metric_name]
        if spec.category == "greenfield":
            delta = final_value
        elif final_value is None:
            delta = None
        elif baseline_value is None and all_new:
            delta = final_value
        elif baseline_value is None:
            delta = None
        else:
            delta = _round(float(final_value) - float(baseline_value))
        metrics[f"{metric_name}_delta"] = delta
    denominator = int(metrics.pop("comment_code_tokens"))
    metrics["comment_density"] = (
        _round(100 * int(metrics["comment_words"]) / denominator)
        if denominator
        else None
    )
    metrics.pop("touched_paths")


def build_report(work_dir: Path, complexity_binary: Path) -> dict[str, Any]:
    snapshot = json.loads((work_dir / "trials.json").read_text())
    associations = snapshot["associations"]
    all_rows = snapshot["rows"]
    dataset_dir = work_dir / "dataset"
    identities = {
        str(row["id"]): _model_identity(row, associations) for row in all_rows
    }
    display_counts = Counter(
        identity["model_display"] for identity in identities.values()
    )
    for identity in identities.values():
        if display_counts[identity["model_display"]] <= 1:
            continue
        effort = identity["reasoning_effort"] or "default effort"
        identity["model_display"] = f"{identity['model_display']} · {effort}"
    attempts: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for association in associations:
        trial = association["trial"]
        attempts[(str(trial["task_name"]), str(association["row_id"]))].append(
            association
        )

    task_names = {spec.task_name for spec in TASKS}
    artifact_unavailable_row_ids: set[str] = set()
    for row in all_rows:
        row_id = str(row["id"])
        successful = next(
            (
                association
                for association in associations
                if str(association["row_id"]) == row_id
                and str(association["trial"]["task_name"]) in task_names
                and _is_success(association)
            ),
            None,
        )
        if successful is None:
            continue
        trial_id = str(successful["trial_id"])
        if not _archive_exports_artifacts(work_dir / "archives" / f"{trial_id}.tar.gz"):
            artifact_unavailable_row_ids.add(row_id)
    rows = [
        row for row in all_rows if str(row["id"]) not in artifact_unavailable_row_ids
    ]

    snapshot_root = work_dir / "snapshots"
    baseline_snapshot_paths: dict[str, dict[str, str]] = {}
    measured: dict[str, dict[str, Any]] = {}

    for spec in TASKS:
        baseline, baseline_suffixes = _read_baseline_sources(dataset_dir, spec)
        baseline_snapshot_paths[spec.slug] = _write_snapshot(
            snapshot_root / "baselines" / spec.slug, baseline, baseline_suffixes
        )
        for row in rows:
            row_id = str(row["id"])
            for association in attempts[(spec.task_name, row_id)]:
                if not _is_success(association):
                    continue
                trial = association["trial"]
                trial_id = str(association["trial_id"])
                sources, suffixes = _read_archive_sources(
                    work_dir / "archives" / f"{trial_id}.tar.gz", spec
                )
                if not sources:
                    raise RuntimeError(
                        f"No scoped source files found for {spec.slug}/{trial_id}"
                    )
                metrics = (
                    _measure_solution(sources, spec)
                    if spec.category == "greenfield"
                    else _measure_change(baseline, sources, spec)
                )
                if spec.category == "repair" and not metrics["touched_paths"]:
                    raise RuntimeError(
                        f"Successful repair has no scoped source changes: "
                        f"{spec.slug}/{trial_id}"
                    )
                metrics.update(
                    {
                        "trial_id": trial_id,
                        "job_id": str(trial["job_id"]),
                        "row_id": row_id,
                        "hub_url": _hub_trial_url(
                            row_id, str(trial["job_id"]), trial_id
                        ),
                    }
                )
                metrics["snapshot_paths"] = _write_snapshot(
                    snapshot_root / "trials" / trial_id, sources, suffixes
                )
                measured[trial_id] = metrics

    complexity_output, complexity_version = _run_complexity_tool(
        complexity_binary, work_dir
    )
    for spec in TASKS:
        for row in rows:
            row_id = str(row["id"])
            for association in attempts[(spec.task_name, row_id)]:
                trial_id = str(association["trial_id"])
                metrics = measured.get(trial_id)
                if metrics is None:
                    continue
                _attach_complexity(
                    metrics,
                    spec,
                    trial_id,
                    complexity_output,
                    baseline_snapshot_paths[spec.slug],
                )

    report_tasks: list[dict[str, Any]] = []
    for spec in TASKS:
        task_rows: list[dict[str, Any]] = []
        for leaderboard_row in rows:
            row_id = str(leaderboard_row["id"])
            identity = identities[row_id]
            model_attempts = attempts[(spec.task_name, row_id)]
            successful = [
                measured[str(item["trial_id"])]
                for item in model_attempts
                if str(item["trial_id"]) in measured
            ]
            successful.sort(key=lambda item: (int(item["loc"]), item["trial_id"]))
            values = [int(item["loc"]) for item in successful]
            metric_values = {
                "code_bytes": [int(item["code_bytes"]) for item in successful],
                "code_tokens": [int(item["code_tokens"]) for item in successful],
                "comment_density": [
                    float(item["comment_density"])
                    for item in successful
                    if item["comment_density"] is not None
                ],
                "ari_grade": [
                    float(item["ari_grade"])
                    for item in successful
                    if item["ari_grade"] is not None
                ],
                "flesch_reading_ease": [
                    float(item["flesch_reading_ease"])
                    for item in successful
                    if item["flesch_reading_ease"] is not None
                ],
                "cognitive_complexity": [
                    float(item["cognitive_complexity"])
                    for item in successful
                    if item["cognitive_complexity"] is not None
                ],
                "cyclomatic_complexity": [
                    float(item["cyclomatic_complexity"])
                    for item in successful
                    if item["cyclomatic_complexity"] is not None
                ],
                "cognitive_delta": [
                    float(item["cognitive_complexity_delta"])
                    for item in successful
                    if item["cognitive_complexity_delta"] is not None
                ],
                "cyclomatic_delta": [
                    float(item["cyclomatic_complexity_delta"])
                    for item in successful
                    if item["cyclomatic_complexity_delta"] is not None
                ],
            }
            task_row: dict[str, Any] = {
                **identity,
                "row_id": row_id,
                "attempts": len(model_attempts),
                "successes": len(successful),
                "failures": len(model_attempts) - len(successful),
                "solve_rate": _round(100 * len(successful) / len(model_attempts)),
                "trials": successful,
                "mean_loc": _mean(values),
                "median_loc": _median(values),
                "min_loc": min(values) if values else None,
                "max_loc": max(values) if values else None,
                "mean_added_loc": _mean(
                    [int(item["added_loc"]) for item in successful]
                ),
                "mean_deleted_loc": _mean(
                    [int(item["deleted_loc"]) for item in successful]
                ),
                "shortest_trial": successful[0] if successful else None,
                "readability_samples": sum(
                    item["ari_grade"] is not None for item in successful
                ),
            }
            for name, metric_list in metric_values.items():
                task_row[f"mean_{name}"] = _mean(metric_list)
            task_rows.append(task_row)

        for metric_field in (
            "mean_loc",
            "mean_code_bytes",
            "mean_code_tokens",
            "mean_cognitive_complexity",
            "mean_cyclomatic_complexity",
        ):
            _task_relative_indices(task_rows, metric_field)
        ranked = sorted(
            (row for row in task_rows if row["mean_loc"] is not None),
            key=lambda row: (float(row["mean_loc"]), -int(row["successes"])),
        )
        for rank, row in enumerate(ranked, start=1):
            row["rank"] = rank
        for row in task_rows:
            row.setdefault("rank", None)
        task_rows.sort(
            key=lambda row: (
                row["rank"] is None,
                int(row["rank"] or 10_000),
                str(row["model_display"]),
            )
        )
        task_successes = sum(int(row["successes"]) for row in task_rows)
        task_attempts = sum(int(row["attempts"]) for row in task_rows)
        if not task_successes:
            continue
        best_means = [
            float(row["mean_loc"]) for row in task_rows if row["mean_loc"] is not None
        ]
        report_tasks.append(
            {
                "slug": spec.slug,
                "title": spec.title,
                "task_name": spec.task_name,
                "category": spec.category,
                **_task_taxonomy(dataset_dir, spec),
                "language": spec.language,
                "description": spec.description,
                "scope": spec.scope,
                "metric_label": spec.metric_label,
                "attempts": task_attempts,
                "successes": task_successes,
                "solve_rate": _round(100 * task_successes / task_attempts),
                "models_solved": sum(bool(row["successes"]) for row in task_rows),
                "best_mean_loc": min(best_means) if best_means else None,
                "rows": task_rows,
            }
        )

    report_models: list[dict[str, Any]] = []
    for leaderboard_row in rows:
        row_id = str(leaderboard_row["id"])
        identity = identities[row_id]
        model_rows = [
            next(row for row in task["rows"] if row["row_id"] == row_id)
            for task in report_tasks
        ]
        solved = [row for row in model_rows if row["successes"]]
        readability_trials = [
            trial
            for row in solved
            for trial in row["trials"]
            if trial["ari_grade"] is not None
        ]

        def index_mean(field: str) -> float | None:
            values = [float(row[field]) for row in solved if row.get(field) is not None]
            return _mean(values)

        model_attempts = sum(int(row["attempts"]) for row in model_rows)
        model_successes = sum(int(row["successes"]) for row in model_rows)
        leaderboard_metrics = leaderboard_row.get("metrics") or {}
        report_models.append(
            {
                **identity,
                "row_id": row_id,
                "attempts": model_attempts,
                "successes": model_successes,
                "solve_rate": (
                    _round(100 * model_successes / model_attempts)
                    if model_attempts
                    else 0.0
                ),
                "tasks_solved": len(solved),
                "task_coverage": (
                    _round(100 * len(solved) / len(report_tasks))
                    if report_tasks
                    else 0.0
                ),
                "wins": sum(row["rank"] == 1 for row in solved),
                "mean_relative_loc_index": index_mean("relative_loc_index"),
                "mean_relative_code_bytes_index": index_mean(
                    "relative_code_bytes_index"
                ),
                "mean_relative_code_tokens_index": index_mean(
                    "relative_code_tokens_index"
                ),
                "mean_relative_cognitive_complexity_index": index_mean(
                    "relative_cognitive_complexity_index"
                ),
                "mean_relative_cyclomatic_complexity_index": index_mean(
                    "relative_cyclomatic_complexity_index"
                ),
                "mean_comment_density": _mean(
                    [
                        float(row["mean_comment_density"])
                        for row in solved
                        if row["mean_comment_density"] is not None
                    ]
                ),
                "mean_ari_grade": _mean(
                    [float(trial["ari_grade"]) for trial in readability_trials]
                ),
                "mean_flesch_reading_ease": _mean(
                    [
                        float(trial["flesch_reading_ease"])
                        for trial in readability_trials
                    ]
                ),
                "readability_samples": len(readability_trials),
                "leaderboard_accuracy": leaderboard_metrics.get("accuracy"),
                "leaderboard_pass_at_5": leaderboard_metrics.get("pass_at_5"),
                "leaderboard_successes": leaderboard_metrics.get("successes"),
                "leaderboard_attempts": leaderboard_metrics.get("n_trials"),
            }
        )
    report_models.sort(
        key=lambda row: (
            float(row["mean_relative_loc_index"] or 10_000),
            -int(row["successes"]),
        )
    )

    unavailable_models = []
    for leaderboard_row in all_rows:
        row_id = str(leaderboard_row["id"])
        if row_id not in artifact_unavailable_row_ids:
            continue
        leaderboard_metrics = leaderboard_row.get("metrics") or {}
        unavailable_models.append(
            {
                **identities[row_id],
                "row_id": row_id,
                "leaderboard_accuracy": leaderboard_metrics.get("accuracy"),
                "leaderboard_pass_at_5": leaderboard_metrics.get("pass_at_5"),
                "leaderboard_successes": leaderboard_metrics.get("successes"),
                "leaderboard_attempts": leaderboard_metrics.get("n_trials"),
                "unavailable_reason": "Trial archives do not export solution artifacts.",
            }
        )

    category_summaries = []
    for category in ("greenfield", "repair"):
        category_tasks = [task for task in report_tasks if task["category"] == category]
        category_summaries.append(
            {
                "category": category,
                "task_count": len(category_tasks),
                "attempts": sum(int(task["attempts"]) for task in category_tasks),
                "successes": sum(int(task["successes"]) for task in category_tasks),
            }
        )

    successes = sum(int(task["successes"]) for task in report_tasks)
    included_slugs = {str(task["slug"]) for task in report_tasks}
    no_success_slugs = SOURCE_SLUGS - included_slugs
    complexity_successes = sum(
        1
        for task in report_tasks
        for row in task["rows"]
        for trial in row["trials"]
        if trial["cognitive_complexity"] is not None
    )
    readability_successes = sum(
        1
        for task in report_tasks
        for row in task["rows"]
        for trial in row["trials"]
        if trial["ari_grade"] is not None
    )
    return {
        "schema_version": 3,
        "generated_at": datetime.now(UTC).isoformat(),
        "extracted_at": snapshot["extracted_at"],
        "source_url": (
            f"{HUB_URL}/datasets/{DATASET}/{DATASET_VERSION}"
            f"?leaderboard={LEADERBOARD}&tab=leaderboard"
        ),
        "dataset": "Terminal-Bench 4.0",
        "methodology": {
            "success": "Only associated trials whose live reward is exactly 1.",
            "greenfield": "Final scoped source artifact.",
            "repair": "Added plus deleted scoped source against the packaged baseline.",
            "loc": "Nonblank physical lines containing a non-comment Pygments token.",
            "code_bytes": "UTF-8 bytes in non-comment, non-whitespace lexer tokens.",
            "code_tokens": (
                "Non-comment, non-whitespace source-code lexical tokens counted "
                "by Pygments; these are not model or API tokens."
            ),
            "comment_density": (
                "Comment words per 100 final-side lexical tokens; docstrings count as comments."
            ),
            "ari_grade": (
                "Automated Readability Index grade of comment prose, using characters "
                "per word and words per sentence."
            ),
            "flesch_reading_ease": (
                "Flesch Reading Ease of comment prose; higher is easier. English "
                "syllables use a documented vowel-group heuristic."
            ),
            "readability_eligibility": (
                f"At least {MIN_READABILITY_WORDS} English prose words and "
                f"{MIN_READABILITY_SENTENCES} punctuated sentences per artifact. "
                "Repair tasks use final-side comments on changed lines."
            ),
            "complexity": (
                "Final scoped source for greenfield; final touched files for repair. "
                "Mozilla rust-code-analysis 0.0.25; unsupported languages are null."
            ),
            "failures": "Failures count toward solve rate and have no source metric.",
            "normalization": (
                "Each task-relative index sets the lowest successful model mean to 100."
            ),
        },
        "tools": {
            "pygments": pygments.__version__,
            "rust_code_analysis": complexity_version,
            "readability": "internal deterministic formulas; syllable heuristic v1",
        },
        "selection": {
            "included": len(report_tasks),
            "greenfield": sum(
                task["category"] == "greenfield" for task in report_tasks
            ),
            "repair": sum(task["category"] == "repair" for task in report_tasks),
            "source_tasks_without_success": len(no_success_slugs),
            "source_tasks_without_success_slugs": sorted(no_success_slugs),
            "deferred_repair": len(DEFERRED_REPAIR_TASKS),
            "deferred_repair_slugs": sorted(DEFERRED_REPAIR_TASKS),
            "excluded_non_source": len(NON_SOURCE_TASKS),
            "excluded_non_source_slugs": sorted(NON_SOURCE_TASKS),
            "terminal_bench_tasks": len(
                SOURCE_SLUGS | DEFERRED_REPAIR_TASKS | NON_SOURCE_TASKS
            ),
            "artifact_unavailable_models": len(unavailable_models),
            "artifact_unavailable_model_names": [
                model["model_display"] for model in unavailable_models
            ],
        },
        "summary": {
            "task_count": len(report_tasks),
            "model_count": len(rows),
            "attempts": sum(int(task["attempts"]) for task in report_tasks),
            "successes": successes,
            "complexity_successes": complexity_successes,
            "readability_successes": readability_successes,
            "category_summaries": category_summaries,
        },
        "models": report_models,
        "unavailable_models": unavailable_models,
        "tasks": report_tasks,
    }


def _write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, indent=2)
    (output_dir / "report.json").write_text(serialized + "\n")
    (output_dir / "data.js").write_text(f"window.REPORT_DATA = {serialized};\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract and analyze Terminal-Bench 4.0 source artifacts."
    )
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--skip-extract", action="store_true")
    parser.add_argument("--download-concurrency", type=int, default=8)
    parser.add_argument("--complexity-bin", type=Path)
    parser.add_argument("--install-metrics", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.download_concurrency < 1:
        raise ValueError("--download-concurrency must be at least 1")
    work_dir = args.work_dir.expanduser().resolve()
    if not args.skip_extract:
        asyncio.run(
            extract(
                work_dir,
                refresh=args.refresh,
                concurrency=args.download_concurrency,
            )
        )
    if args.extract_only:
        return
    if not (work_dir / "trials.json").is_file():
        raise RuntimeError("trials.json is missing; run without --skip-extract first")
    complexity_binary = _resolve_complexity_binary(work_dir, args.complexity_bin)
    if complexity_binary is None and args.install_metrics:
        complexity_binary = install_metrics_tool(work_dir)
    if complexity_binary is None:
        raise RuntimeError(
            "rust-code-analysis-cli is missing; pass --install-metrics or "
            "--complexity-bin"
        )
    report = build_report(work_dir, complexity_binary)
    _write_report(report, args.output_dir.resolve())


if __name__ == "__main__":
    main()
