from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple


FRONTMATTER_PATTERN = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", re.DOTALL)
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
TAG_PATTERN = re.compile(r"(?<!\w)#([\w\u3400-\u9fff/-]+)", re.UNICODE)
WORD_PATTERN = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)


@dataclass
class MarkdownDocument:
    path: str
    title: str
    text: str
    body: str
    frontmatter: Dict[str, object]
    tags: List[str]
    aliases: List[str]
    updated: str
    document_type: str
    document_role: str
    status: str
    source: str
    updated_source: str


@dataclass
class MarkdownChunk:
    chunk_id: str
    path: str
    title: str
    heading: str
    text: str
    tags: List[str]
    aliases: List[str]
    ordinal: int
    document_type: str = "note"
    document_role: str = "note"
    status: str = "active"
    source: str = "unspecified"
    updated: str = ""


def _parse_scalar(raw: str):
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("\"'") for item in inner.split(",") if item.strip()]
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value in ("null", "Null", "~"):
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> Tuple[Dict[str, object], str]:
    match = FRONTMATTER_PATTERN.match(text.lstrip("\ufeff"))
    if not match:
        return {}, text.lstrip("\ufeff")
    result: Dict[str, object] = {}
    active_list = ""
    for raw_line in match.group(1).splitlines():
        if re.match(r"^\s+-\s+", raw_line) and active_list:
            result.setdefault(active_list, [])
            value = re.sub(r"^\s+-\s+", "", raw_line).strip().strip("\"'")
            if value:
                result[active_list].append(value)
            continue
        found = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", raw_line)
        if not found:
            active_list = ""
            continue
        key, value = found.groups()
        parsed = [] if value.strip() == "" else _parse_scalar(value)
        result[key] = parsed
        active_list = key if value.strip() == "" else ""
    return result, match.string[match.end():]


def list_value(value) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in re.split(r"[,，]", value) if item.strip()]
    return []


def first_heading(body: str, fallback: str) -> str:
    match = HEADING_PATTERN.search(body)
    return match.group(2).strip() if match else fallback


def infer_document_type(relative: str, frontmatter: Dict[str, object]) -> str:
    explicit = str(frontmatter.get("type") or frontmatter.get("memory_type") or "").strip().casefold()
    if explicit:
        return explicit
    root = relative.replace("\\", "/").split("/", 1)[0]
    return {
        "00-System": "system",
        "01-Inbox": "inbox",
        "02-Projects": "project",
        "03-Knowledge": "knowledge",
        "04-Content": "content",
        "05-Prompts": "prompt",
        "06-Business": "business",
        "07-Quick-Notes": "quick-note",
        "90-Archive": "archive",
        "98-Skills": "skill",
    }.get(root, "note")


def section_value(body: str, *titles: str) -> str:
    if not titles:
        return ""
    alternatives = "|".join(re.escape(title) for title in titles)
    match = re.search(
        rf"(?ms)^##{{1,5}}\s+(?:{alternatives})\s*$\n(.*?)(?=^##{{1,5}}\s+|\Z)",
        body,
    )
    return match.group(1).strip() if match else ""


def infer_document_role(relative: str, frontmatter: Dict[str, object], document_type: str) -> str:
    explicit = str(frontmatter.get("role") or "").strip().casefold()
    if explicit:
        return explicit
    if frontmatter.get("memory_type"):
        return "knowledge-card"
    if document_type == "content" and any(marker in relative.casefold() for marker in ("style", "guide", "习惯", "风格")):
        return "style-guide"
    if relative == "00-System/Asset-Index.md":
        return "asset-index"
    if relative in {
        "DASHBOARD.md",
        "00-System/Hot-Index.md",
        "00-System/Memory-Index.md",
        "00-System/Source-Map.md",
        "00-System/Active-Context.md",
        "00-System/Index-Health.md",
    }:
        return "navigation-index"
    return {
        "knowledge": "knowledge-card",
        "project": "project-brief",
        "content": "content-asset",
        "prompt": "workflow",
        "business": "business-card",
        "system": "system-rule",
        "quick-note": "quick-note",
        "archive": "archive",
    }.get(document_type, "note")


def derived_updated(path: Path, frontmatter: Dict[str, object], body: str = "") -> Tuple[str, str]:
    declared = str(frontmatter.get("updated") or frontmatter.get("date") or "").strip()
    if declared:
        return declared, "frontmatter"
    legacy = section_value(body, "更新时间")
    match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", legacy)
    if match:
        return match.group(0), "section"
    try:
        value = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return "", "unavailable"
    return value.isoformat(timespec="seconds").replace("+00:00", "Z"), "filesystem"


def parse_document(path: Path, relative: str, text: str) -> MarkdownDocument:
    frontmatter, body = parse_frontmatter(text)
    title = str(frontmatter.get("title") or first_heading(body, path.stem)).strip()
    tags = list_value(frontmatter.get("tags"))
    tags.extend(TAG_PATTERN.findall(body))
    legacy_tags = re.sub(r"[`#*]", "", section_value(body, "相关标签", "标签", "Tags"))
    tags.extend(re.sub(r"^\s*[-+]\s*", "", item).strip() for item in re.split(r"[,，、\n]", legacy_tags) if item.strip())
    aliases = list_value(frontmatter.get("aliases"))
    tags = list(dict.fromkeys(item.casefold() for item in tags if item))
    aliases = list(dict.fromkeys(item for item in aliases if item))
    document_type = infer_document_type(relative, frontmatter)
    document_role = infer_document_role(relative, frontmatter, document_type)
    status = str(frontmatter.get("status") or "active").strip().casefold()
    source = str(frontmatter.get("source") or frontmatter.get("source_type") or "").strip()
    if not source:
        legacy_source = section_value(body, "来源类型")
        source = legacy_source.splitlines()[0].strip() if legacy_source else "unspecified"
    updated, updated_source = derived_updated(path, frontmatter, body)
    return MarkdownDocument(
        relative, title, text, body, frontmatter, tags, aliases, updated,
        document_type, document_role, status, source, updated_source,
    )


def chunk_document(document: MarkdownDocument, max_chars: int = 2400) -> List[MarkdownChunk]:
    matches = list(HEADING_PATTERN.finditer(document.body))
    sections = []
    if not matches:
        sections.append((document.title, document.body))
    else:
        if document.body[:matches[0].start()].strip():
            sections.append((document.title, document.body[:matches[0].start()]))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(document.body)
            sections.append((match.group(2).strip(), document.body[match.end():end]))

    chunks: List[MarkdownChunk] = []
    ordinal = 0
    for heading, raw in sections:
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n", raw) if item.strip()]
        if not paragraphs:
            paragraphs = [heading]
        buffer = ""
        for paragraph in paragraphs:
            candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
            if buffer and len(candidate) > max_chars:
                chunks.append(MarkdownChunk(
                    f"{document.path}#{ordinal}", document.path, document.title, heading, buffer,
                    document.tags, document.aliases, ordinal, document.document_type,
                    document.document_role, document.status, document.source, document.updated,
                ))
                ordinal += 1
                buffer = paragraph
            else:
                buffer = candidate
        if buffer:
            chunks.append(MarkdownChunk(
                f"{document.path}#{ordinal}", document.path, document.title, heading, buffer,
                document.tags, document.aliases, ordinal, document.document_type,
                document.document_role, document.status, document.source, document.updated,
            ))
            ordinal += 1
    return chunks


def terms(text: str) -> List[str]:
    return [item.casefold() for item in WORD_PATTERN.findall(text)]


def yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if re.fullmatch(r"[A-Za-z0-9_./:+-]+", text):
        return text
    return json.dumps(text, ensure_ascii=False)


def render_frontmatter(values: Dict[str, object]) -> str:
    lines = ["---"]
    for key, value in values.items():
        if isinstance(value, (list, tuple)):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {yaml_scalar(item)}" for item in value)
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.extend(["---", ""])
    return "\n".join(lines)
