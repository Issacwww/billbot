#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pypdf import PdfReader

LOGGER = logging.getLogger("billbot")

AMOUNT_RE = re.compile(
    r"(?:\$\s*)?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})|[0-9]+\.[0-9]{2})(?:\s*\$)?"
)
DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
DATE_RANGE_RE = re.compile(r"(\d{2}/\d{2}/\d{4})\s*[-–—]\s*(\d{2}/\d{2}/\d{4})")
# City bills use MM/DD/YY format with space separator
DATE_RANGE_SHORT_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{2})\s+(\d{1,2}/\d{1,2}/\d{2})")

LABELS = [
    "total amount due",
    "amount due",
    "total due",
    "balance due",
    "total current charges",
    "current charges",
    "new charges",
    "please pay",
]

LINE_ITEM_PATTERNS = [
    (
        "electric_delivery",
        re.compile(
            r"electric\s+delivery\s+charges?.{0,80}?(\d{2}/\d{2}/\d{4})\s*[-–]\s*(\d{2}/\d{2}/\d{4}).{0,30}?\$?\s*([0-9]+\.[0-9]{2})",
            re.IGNORECASE,
        ),
    ),
    (
        "electric_generation",
        re.compile(
            r"electric\s+generation\s+charges?.{0,80}?(\d{2}/\d{2}/\d{4})\s*[-–]\s*(\d{2}/\d{2}/\d{4}).{0,30}?\$?\s*([0-9]+\.[0-9]{2})",
            re.IGNORECASE,
        ),
    ),
    (
        "gas",
        re.compile(
            r"gas\s+charges?.{0,80}?(\d{2}/\d{2}/\d{4})\s*[-–]\s*(\d{2}/\d{2}/\d{4}).{0,30}?\$?\s*([0-9]+\.[0-9]{2})",
            re.IGNORECASE,
        ),
    ),
    (
        "water",
        re.compile(
            r"water.{0,60}?\$?\s*([0-9]+\.[0-9]{2})",
            re.IGNORECASE,
        ),
    ),
    (
        "sewer",
        re.compile(
            r"sewer.{0,60}?\$?\s*([0-9]+\.[0-9]{2})",
            re.IGNORECASE,
        ),
    ),
    (
        "garbage",
        re.compile(
            r"(?:garbage|trash|refuse).{0,60}?\$?\s*([0-9]+\.[0-9]{2})",
            re.IGNORECASE,
        ),
    ),
]


@dataclass
class TenantConfig:
    name: str
    share_percent: float
    is_active: bool = True
    lease_start: Optional[str] = None
    lease_end: Optional[str] = None
    email: Optional[str] = None


@dataclass
class TenantShare:
    name: str
    email: Optional[str]
    share_percent: float
    included: bool
    reason: str
    amount: float
    prorate_factor: Optional[float] = None  # e.g. 0.5 if tenant covers half the bill period
    prorate_detail: Optional[str] = None  # human-readable explanation


@dataclass
class ExtractionDetails:
    method: str
    confidence: str
    matched_label: str
    matched_snippet: str
    due_date: Optional[str]


@dataclass
class BillLineItem:
    category: str
    amount: float
    source_text: str
    confidence: str
    period_start: Optional[str] = None
    period_end: Optional[str] = None


@dataclass
class ValidationResult:
    passed: bool
    issues: list[str] = field(default_factory=list)
    sum_line_items: Optional[float] = None
    difference_from_total_due: Optional[float] = None


@dataclass
class BillbotResult:
    pdf_path: str
    detected_amount_due: float
    extraction: ExtractionDetails
    generated_at: str
    provider: Optional[str] = None
    bill_period_start: Optional[str] = None
    bill_period_end: Optional[str] = None
    tenant_shares: list[TenantShare] = field(default_factory=list)
    total_assigned_amount: float = 0.0
    line_items: list[BillLineItem] = field(default_factory=list)
    validation: Optional[ValidationResult] = None
    text_sources_used: list[str] = field(default_factory=list)


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logging.getLogger("pdfminer").setLevel(logging.WARNING)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_money(raw: str) -> float:
    return float(raw.replace(",", "").replace("$", "").strip())


def to_float(value: object) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return parse_money(value)
        except ValueError:
            return None
    return None


def read_pdf_text_pypdf(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    chunks: list[str] = []
    for idx, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        LOGGER.debug("pypdf page %d length=%d", idx + 1, len(text))
        if text.strip():
            chunks.append(text)
    return "\n".join(chunks).strip()


def read_pdf_text_pdfplumber(pdf_path: Path) -> str:
    try:
        import pdfplumber
    except Exception:
        return ""
    chunks: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            LOGGER.debug("pdfplumber page %d length=%d", idx + 1, len(text))
            if text.strip():
                chunks.append(text)
    return "\n".join(chunks).strip()


def looks_low_quality(text: str) -> bool:
    if len(text) < 300:
        return True
    lowered = text.lower()
    useful_keywords = [
        "amount due",
        "charges",
        "electric",
        "gas",
        "water",
        "sewer",
        "garbage",
        "total",
    ]
    hits = sum(1 for word in useful_keywords if word in lowered)
    amount_hits = len(AMOUNT_RE.findall(text))
    return hits < 2 or amount_hits < 2


def read_pdf_text_ocr(pdf_path: Path, max_pages: int) -> str:
    if shutil.which("tesseract") is None:
        LOGGER.warning("tesseract not found, skip OCR")
        return ""
    try:
        import pypdfium2 as pdfium
    except Exception:
        LOGGER.warning("pypdfium2 not installed, skip OCR")
        return ""

    doc = pdfium.PdfDocument(str(pdf_path))
    page_count = min(len(doc), max_pages)
    chunks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="billbot_ocr_") as tmpdir:
        for i in range(page_count):
            page = doc[i]
            bitmap = page.render(scale=2.2)
            pil_img = bitmap.to_pil()
            img_path = Path(tmpdir) / f"page_{i + 1}.png"
            pil_img.save(str(img_path))
            cmd = ["tesseract", str(img_path), "stdout", "--psm", "6"]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                LOGGER.warning("tesseract failed on page %d: %s", i + 1, proc.stderr.strip())
                continue
            page_text = proc.stdout.strip()
            LOGGER.debug("ocr page %d length=%d", i + 1, len(page_text))
            if page_text:
                chunks.append(page_text)
    return "\n".join(chunks).strip()


def extract_json_object(text: str) -> Optional[dict]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(cleaned[start : end + 1])
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        return None


def extract_response_text(payload: dict) -> str:
    direct = str(payload.get("output_text", "")).strip()
    if direct:
        return direct
    chunks: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text_value = part.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    chunks.append(text_value.strip())
    return "\n".join(chunks).strip()


def extract_due_date(text: str) -> Optional[str]:
    patterns = [
        r"total\s+amount\s+due\s+by\s+(\d{2}/\d{2}/\d{4})",
        r"(?:due\s+date|payment\s+due)\s*[:\-]?\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def parse_iso_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def load_tenants(path: Path) -> list[TenantConfig]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("tenants JSON must be an object")
    raw_tenants = payload.get("tenants")
    if not isinstance(raw_tenants, list) or not raw_tenants:
        raise ValueError("tenants JSON must include non-empty 'tenants' array")

    tenants: list[TenantConfig] = []
    for idx, item in enumerate(raw_tenants):
        if not isinstance(item, dict):
            raise ValueError(f"tenants[{idx}] must be an object")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError(f"tenants[{idx}].name is required")
        share_percent = to_float(item.get("share_percent"))
        if share_percent is None or not (0 <= share_percent <= 100):
            raise ValueError(f"tenants[{idx}].share_percent must be in [0,100]")
        tenants.append(
            TenantConfig(
                name=name,
                email=str(item["email"]).strip() if item.get("email") else None,
                share_percent=round(share_percent, 2),
                # Project rule: having lease_end means tenant is no longer active.
                is_active=False if item.get("lease_end") else bool(item.get("is_active", True)),
                lease_start=str(item["lease_start"]).strip() if item.get("lease_start") else None,
                lease_end=str(item["lease_end"]).strip() if item.get("lease_end") else None,
            )
        )
    return tenants


def dates_overlap(
    lease_start: Optional[str],
    lease_end: Optional[str],
    bill_start: Optional[str],
    bill_end: Optional[str],
) -> bool:
    ls = parse_iso_date(lease_start)
    le = parse_iso_date(lease_end)
    bs = parse_iso_date(bill_start)
    be = parse_iso_date(bill_end)
    if not bs and not be:
        return True
    # If we know bill_end, check if it's entirely before lease starts
    if ls and be and be < ls:
        return False
    # If we know bill_start, check if it's entirely after lease ends
    if le and bs and bs > le:
        return False
    return True


def compute_overlap_days(
    lease_start: Optional[str],
    lease_end: Optional[str],
    bill_start: Optional[str],
    bill_end: Optional[str],
) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """Compute overlap days between lease and bill period.

    Returns:
        (overlap_days, total_bill_days, detail_string) or (None, None, None) if periods unknown.
    """
    bs = parse_iso_date(bill_start)
    be = parse_iso_date(bill_end)
    if not bs or not be:
        return None, None, None

    # Bill period dates are inclusive on both ends, so add 1
    total_days = (be - bs).days + 1
    if total_days <= 0:
        return None, None, None

    ls = parse_iso_date(lease_start)
    le = parse_iso_date(lease_end)

    # Clamp to bill period
    overlap_start = max(bs, ls) if ls else bs
    overlap_end = min(be, le) if le else be

    overlap_days = max(0, (overlap_end - overlap_start).days + 1)

    detail = f"{overlap_days}/{total_days} days"
    if ls and ls > bs:
        detail += f" (lease starts {lease_start}, bill starts {bill_start})"
    if le and le < be:
        detail += f" (lease ends {lease_end}, bill ends {bill_end})"

    return overlap_days, total_days, detail


def compute_tenant_shares(
    amount_due: float,
    tenants: list[TenantConfig],
    bill_period_start: Optional[str],
    bill_period_end: Optional[str],
) -> list[TenantShare]:
    draft: list[tuple[TenantConfig, bool, str, Optional[float], Optional[str]]] = []
    for tenant in tenants:
        if not tenant.is_active:
            draft.append((tenant, False, "inactive", None, None))
            continue

        if not dates_overlap(
            tenant.lease_start, tenant.lease_end,
            bill_period_start, bill_period_end,
        ):
            draft.append((tenant, False, "outside_bill_period", None, None))
            continue

        # Check if we need pro-rating (partial overlap)
        overlap_days, total_days, detail = compute_overlap_days(
            tenant.lease_start, tenant.lease_end,
            bill_period_start, bill_period_end,
        )

        if overlap_days is not None and total_days is not None and overlap_days < total_days:
            prorate = overlap_days / total_days
            draft.append((tenant, True, "prorated", prorate, detail))
        else:
            draft.append((tenant, True, "included", None, None))

    raw_amounts: list[float] = []
    for tenant, included, reason, prorate, detail in draft:
        if not included:
            raw_amounts.append(0.0)
        elif prorate is not None:
            raw_amounts.append(round(amount_due * tenant.share_percent / 100.0 * prorate, 2))
        else:
            raw_amounts.append(round(amount_due * tenant.share_percent / 100.0, 2))

    shares: list[TenantShare] = []
    for idx, (tenant, included, reason, prorate, detail) in enumerate(draft):
        shares.append(
            TenantShare(
                name=tenant.name,
                email=tenant.email,
                share_percent=tenant.share_percent,
                included=included,
                reason=reason,
                amount=raw_amounts[idx],
                prorate_factor=prorate,
                prorate_detail=detail,
            )
        )
    return shares


def detect_provider(text: str, pdf_name: str) -> Optional[str]:
    low = text.lower()
    low_name = pdf_name.lower()
    if "pacific gas and electric" in low or "pge" in low or "pge" in low_name:
        return "pge"
    if "city services" in low or "city service" in low or "statement" in low_name:
        return "city-service"
    return None


def detect_amount_rule_based(text: str) -> Optional[tuple[float, ExtractionDetails]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    best: Optional[tuple[float, ExtractionDetails, float]] = None

    for idx, line in enumerate(lines):
        low = line.lower()
        for label_rank, label in enumerate(LABELS):
            if label not in low:
                continue
            amounts = [parse_money(raw) for raw in AMOUNT_RE.findall(line)]
            amounts = [x for x in amounts if x > 0]
            if not amounts:
                continue
            amount = amounts[-1]
            score = 100.0 - (label_rank * 8.0) + (idx / max(1, len(lines)))
            details = ExtractionDetails(
                method="rule",
                confidence="high" if label_rank <= 2 else "medium",
                matched_label=label,
                matched_snippet=line,
                due_date=extract_due_date(line),
            )
            if best is None or score > best[2]:
                best = (amount, details, score)

    if best is not None:
        return best[0], best[1]

    for idx, line in enumerate(lines):
        low = line.lower()
        match_label = next((label for label in LABELS if label in low), None)
        if not match_label:
            continue
        lookahead = lines[idx : min(len(lines), idx + 6)]
        amounts = [parse_money(raw) for seg in lookahead for raw in AMOUNT_RE.findall(seg)]
        amounts = [x for x in amounts if x > 0]
        if not amounts:
            continue
        amount = amounts[-1]
        details = ExtractionDetails(
            method="rule-lookahead",
            confidence="medium",
            matched_label=match_label,
            matched_snippet="\n".join(lookahead),
            due_date=extract_due_date("\n".join(lookahead)),
        )
        return amount, details

    all_amounts = [parse_money(raw) for raw in AMOUNT_RE.findall(text)]
    positive = [x for x in all_amounts if x > 0]
    if not positive:
        return None
    amount = max(x for x in positive if x < 10000) if any(x < 10000 for x in positive) else max(positive)
    details = ExtractionDetails(
        method="rule-fallback",
        confidence="low",
        matched_label="fallback:max-amount",
        matched_snippet="Used fallback strategy: maximum reasonable monetary value.",
        due_date=extract_due_date(text),
    )
    return amount, details


def extract_first_date_range_near(text: str, anchor: str, window: int = 1200) -> tuple[Optional[str], Optional[str]]:
    low = text.lower()
    anchor_low = anchor.lower()
    start_at = 0
    while True:
        idx = low.find(anchor_low, start_at)
        if idx < 0:
            return None, None
        snippet = text[idx : idx + window]
        match = DATE_RANGE_RE.search(snippet)
        if match:
            return match.group(1), match.group(2)
        start_at = idx + max(1, len(anchor_low))


def extract_pge_line_items(text: str) -> list[BillLineItem]:
    amount_patterns = {
        "electric_delivery": re.compile(
            r"(?:current\s+)?p(?:g&e|ge)\s+electric\s+delivery\s+charges\s*\$?\s*([0-9]+\.[0-9]{2})",
            re.IGNORECASE,
        ),
        "electric_generation": re.compile(
            r"electric\s+generation\s+charges\s*\$?\s*([0-9]+\.[0-9]{2})",
            re.IGNORECASE,
        ),
        "gas": re.compile(
            r"current\s+gas\s+charges\s*\$?\s*([0-9]+\.[0-9]{2})",
            re.IGNORECASE,
        ),
    }
    date_anchors = {
        "electric_delivery": "Details of PG&E Electric Delivery Charges",
        "electric_generation": "Details of Silicon Valley Clean Energy Electric",
        "gas": "Details of Gas Charges Service Information",
    }
    items: list[BillLineItem] = []
    for category, pattern in amount_patterns.items():
        match = pattern.search(text)
        if not match:
            continue
        amount = round(parse_money(match.group(1)), 2)
        period_start, period_end = extract_first_date_range_near(text, date_anchors[category])
        items.append(
            BillLineItem(
                category=category,
                amount=amount,
                source_text=match.group(0),
                confidence="high",
                period_start=period_start,
                period_end=period_end,
            )
        )
    return items


def extract_line_items_rule_based(text: str, provider: Optional[str]) -> list[BillLineItem]:
    if provider == "pge":
        pge_items = extract_pge_line_items(text)
        if pge_items:
            return pge_items

    items: list[BillLineItem] = []
    for category, pattern in LINE_ITEM_PATTERNS:
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        chosen = matches[-1]
        groups = chosen.groups()
        period_start: Optional[str] = None
        period_end: Optional[str] = None
        amount_str: Optional[str] = None
        if len(groups) == 3:
            period_start, period_end, amount_str = groups
        elif len(groups) == 1:
            amount_str = groups[0]
        if amount_str is None:
            continue
        try:
            amount = round(parse_money(amount_str), 2)
        except ValueError:
            continue
        items.append(
            BillLineItem(
                category=category,
                amount=amount,
                source_text=chosen.group(0),
                confidence="medium",
                period_start=period_start,
                period_end=period_end,
            )
        )
    return items


def _normalize_short_date(d: str) -> str:
    """Convert MM/DD/YY to MM/DD/YYYY."""
    parts = d.split("/")
    if len(parts) == 3 and len(parts[2]) == 2:
        year = int(parts[2])
        full_year = 2000 + year if year < 50 else 1900 + year
        return f"{parts[0]}/{parts[1]}/{full_year}"
    return d


def infer_bill_period(line_items: list[BillLineItem], text: str) -> tuple[Optional[str], Optional[str]]:
    starts = [item.period_start for item in line_items if item.period_start]
    ends = [item.period_end for item in line_items if item.period_end]
    if starts and ends:
        return min(starts), max(ends)
    # Try MM/DD/YYYY date ranges
    ranges = DATE_RANGE_RE.findall(text)
    if ranges:
        starts2 = [s for s, _ in ranges]
        ends2 = [e for _, e in ranges]
        return min(starts2), max(ends2)
    # Try MM/DD/YY date ranges (City bills)
    short_ranges = DATE_RANGE_SHORT_RE.findall(text)
    if short_ranges:
        starts3 = [_normalize_short_date(s) for s, _ in short_ranges]
        ends3 = [_normalize_short_date(e) for _, e in short_ranges]
        return min(starts3), max(ends3)
    return None, None


def detect_structured_with_ai(
    text: str,
    model: str,
    debug_ai_out: Optional[Path] = None,
) -> Optional[tuple[float, ExtractionDetails, Optional[str], Optional[str], Optional[str], list[BillLineItem]]]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        LOGGER.info("OPENAI_API_KEY not set; skipping AI extraction")
        return None

    request_payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "Extract utility bill data from text and return strict JSON only with keys: "
                    "provider (string|null), bill_period_start (YYYY-MM-DD|null), bill_period_end (YYYY-MM-DD|null), "
                    "total_due (number), due_date (YYYY-MM-DD|null), matched_label (string), matched_snippet (string), "
                    "confidence (high|medium|low), line_items (array of objects with category, amount, source_text, confidence)."
                ),
            },
            {"role": "user", "content": text[:22000]},
        ],
    }
    req = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        LOGGER.warning("AI extraction failed: %s", exc)
        return None

    if debug_ai_out:
        debug_ai_out.parent.mkdir(parents=True, exist_ok=True)
        debug_ai_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    output_text = extract_response_text(payload)
    if not output_text:
        LOGGER.warning("AI extraction returned empty output_text")
        return None
    parsed = extract_json_object(output_text)
    if not parsed:
        LOGGER.warning("AI extraction did not return valid JSON")
        return None

    amount = to_float(parsed.get("total_due"))
    if amount is None or amount <= 0:
        LOGGER.warning("AI extraction did not provide valid total_due")
        return None

    items: list[BillLineItem] = []
    if isinstance(parsed.get("line_items"), list):
        for item in parsed["line_items"]:
            if not isinstance(item, dict):
                continue
            item_amount = to_float(item.get("amount"))
            if item_amount is None or item_amount < 0:
                continue
            items.append(
                BillLineItem(
                    category=str(item.get("category", "other")),
                    amount=round(item_amount, 2),
                    source_text=str(item.get("source_text", "")),
                    confidence=str(item.get("confidence", "medium")),
                )
            )

    details = ExtractionDetails(
        method="ai-structured",
        confidence=str(parsed.get("confidence", "medium")),
        matched_label=str(parsed.get("matched_label", "ai-detected")),
        matched_snippet=str(parsed.get("matched_snippet", "")),
        due_date=parsed.get("due_date"),
    )
    return (
        round(amount, 2),
        details,
        parsed.get("provider"),
        parsed.get("bill_period_start"),
        parsed.get("bill_period_end"),
        items,
    )


def validate_result(
    amount_due: float,
    line_items: list[BillLineItem],
    bill_period_start: Optional[str],
    bill_period_end: Optional[str],
    provider: Optional[str],
) -> ValidationResult:
    issues: list[str] = []
    if amount_due <= 0:
        issues.append("total_due must be > 0")
    if amount_due > 10000:
        issues.append("total_due is unreasonably large (>10000), likely parsing error")

    start = parse_iso_date(bill_period_start)
    end = parse_iso_date(bill_period_end)
    if bill_period_start and not start:
        issues.append("bill_period_start is not a valid date")
    if bill_period_end and not end:
        issues.append("bill_period_end is not a valid date")
    if start and end and end < start:
        issues.append("bill_period_end is before bill_period_start")

    sum_items = None
    diff = None
    if line_items:
        sum_items = round(sum(item.amount for item in line_items), 2)
        diff = round(abs(sum_items - amount_due), 2)
        if diff > 5.0:
            issues.append(f"sum(line_items) differs from total_due by {diff:.2f}")
    if provider == "pge":
        cats = {item.category for item in line_items}
        expected = {"electric_delivery", "electric_generation", "gas"}
        missing = expected - cats
        if missing:
            issues.append("pge line items missing expected categories: " + ", ".join(sorted(missing)))
    return ValidationResult(
        passed=len(issues) == 0,
        issues=issues,
        sum_line_items=sum_items,
        difference_from_total_due=diff,
    )


def default_output_path(pdf_path: Path) -> Path:
    if pdf_path.suffix.lower() == ".pdf":
        return pdf_path.with_suffix(".billbot.json")
    return pdf_path.with_name(pdf_path.name + ".billbot.json")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BillBot CLI (Python)")
    parser.add_argument("--pdf", required=True, help="Path to utility bill PDF")
    parser.add_argument("--tenants-file", required=True, help="Path to tenants JSON file")
    parser.add_argument("--out", required=False, help="Output JSON path")
    parser.add_argument(
        "--use-ai-fallback",
        action="store_true",
        help="Try AI first, then fallback to local parser if AI output fails validation",
    )
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"), help="OpenAI model")
    parser.add_argument("--debug-ai-out", required=False, help="Optional path for raw AI response JSON")
    parser.add_argument(
        "--env-file",
        default="projects/billbot/.env",
        help="Path to .env file (default: projects/billbot/.env)",
    )
    parser.add_argument("--disable-ocr", action="store_true", help="Disable OCR fallback")
    parser.add_argument("--ocr-max-pages", type=int, default=8, help="Max pages for OCR fallback")
    parser.add_argument("--debug", action="store_true", help="Enable debug logs")
    return parser.parse_args(argv)


def parse_pdf(
    pdf_path: Path,
    tenants: list[TenantConfig],
    *,
    use_ai_fallback: bool = False,
    model: str = "gpt-4.1-mini",
    disable_ocr: bool = False,
    ocr_max_pages: int = 8,
) -> BillbotResult:
    """Parse a PDF bill and return structured result. Raises ValueError on failure."""
    text_sources_used: list[str] = []
    text_chunks: list[str] = []

    pypdf_text = read_pdf_text_pypdf(pdf_path)
    if pypdf_text:
        text_chunks.append(pypdf_text)
        text_sources_used.append("pypdf")

    plumber_text = read_pdf_text_pdfplumber(pdf_path)
    if plumber_text:
        text_chunks.append(plumber_text)
        text_sources_used.append("pdfplumber")

    merged_text = "\n\n".join(text_chunks).strip()
    need_ocr = (not merged_text or looks_low_quality(merged_text)) and not disable_ocr
    if need_ocr:
        LOGGER.info("Text quality is low, trying OCR fallback")
        ocr_text = read_pdf_text_ocr(pdf_path, max_pages=max(1, ocr_max_pages))
        if ocr_text:
            text_sources_used.append("ocr")
            merged_text = (merged_text + "\n\n" + ocr_text).strip() if merged_text else ocr_text

    if not merged_text:
        raise ValueError("No text could be extracted from PDF (including OCR)")

    provider = detect_provider(merged_text, pdf_path.name)
    line_items = extract_line_items_rule_based(merged_text, provider)
    period_start, period_end = infer_bill_period(line_items, merged_text)

    detected: Optional[tuple[float, ExtractionDetails]] = None
    validation: Optional[ValidationResult] = None

    if use_ai_fallback:
        LOGGER.info("Trying AI structured extraction with model %s", model)
        ai = detect_structured_with_ai(merged_text, model)
        if ai is not None:
            ai_amount, ai_details, ai_provider, ai_start, ai_end, ai_items = ai
            detected = (ai_amount, ai_details)
            provider = ai_provider or provider
            period_start = ai_start or period_start
            period_end = ai_end or period_end
            if ai_items:
                line_items = ai_items
            validation = validate_result(ai_amount, line_items, period_start, period_end, provider)
            if not validation.passed:
                LOGGER.warning("AI extraction failed validation: %s", "; ".join(validation.issues))
                detected = None

    if detected is None:
        detected = detect_amount_rule_based(merged_text)
        if detected is not None:
            validation = validate_result(detected[0], line_items, period_start, period_end, provider)

    if detected is None:
        raise ValueError("Could not detect amount due. Try OCR-enabled run or AI fallback.")

    amount_due, extraction = detected
    tenant_shares = compute_tenant_shares(
        amount_due=round(amount_due, 2),
        tenants=tenants,
        bill_period_start=period_start,
        bill_period_end=period_end,
    )
    total_assigned = round(sum(t.amount for t in tenant_shares), 2)

    return BillbotResult(
        pdf_path=str(pdf_path),
        detected_amount_due=round(amount_due, 2),
        extraction=extraction,
        generated_at=datetime.now(timezone.utc).isoformat(),
        provider=provider,
        bill_period_start=period_start,
        bill_period_end=period_end,
        tenant_shares=tenant_shares,
        total_assigned_amount=total_assigned,
        line_items=line_items,
        validation=validation,
        text_sources_used=text_sources_used,
    )


def run(argv: list[str]) -> int:
    args = parse_args(argv)
    setup_logging(args.debug)
    load_env_file(Path(args.env_file).expanduser().resolve())

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    tenants_path = Path(args.tenants_file).expanduser().resolve()
    if not tenants_path.exists():
        raise FileNotFoundError(f"Tenants file not found: {tenants_path}")

    output_path = Path(args.out).expanduser().resolve() if args.out else default_output_path(pdf_path)
    tenants = load_tenants(tenants_path)

    try:
        result = parse_pdf(
            pdf_path,
            tenants,
            use_ai_fallback=args.use_ai_fallback,
            model=args.model,
            disable_ocr=args.disable_ocr,
            ocr_max_pages=args.ocr_max_pages,
        )
    except ValueError as exc:
        payload = {
            "status": "failed",
            "pdf_path": str(pdf_path),
            "error": str(exc),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tenants_file": str(tenants_path),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "failed", "output_path": str(output_path)}, indent=2))
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "ok",
                "output_path": str(output_path),
                "detected_amount_due": result.detected_amount_due,
                "total_assigned_amount": result.total_assigned_amount,
                "included_tenants": len([t for t in result.tenant_shares if t.included]),
                "method": result.extraction.method,
                "sources": result.text_sources_used,
            },
            indent=2,
        )
    )
    return 0


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1)
