"""Parse the renovation Bill of Quantities xlsx into structured chunks.

BoQ rows classify into six kinds via Bill Ref + Description + Unit:
  TRADE_HEADER | SUBSECTION_HEADER | GROUP_HEADER |
  PRICED_ITEM  | UNPRICED_ITEM     | NARRATIVE | TOTAL

Emits:
  1. trade_summary chunk per Summary sheet (aggregate totals)
  2. line_item chunk per priced row (direct rate lookup)
  3. subsection roll-up chunk per (trade, subsection, group) block (topical)

See scripts/corpus_spike/parse_boq.py for the original validation run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import openpyxl

from app.integrations.historical_corpus.parsers.types import ChunkRecord, DocMeta


NUMBERED_SUBSECTION_RE = re.compile(r"^(\d+(?:\.\d+)?)(\.|\s)")
NARRATIVE_UNITS = {"note", "noid", "noidn"}
UNPRICED_UNITS = {"item", "sum"}


@dataclass
class _Row:
    row_num: int
    bill_ref: str
    description: str
    quantity: float | None
    unit: str
    rate: float | None
    total: float | None

    @property
    def kind(self) -> str:
        desc = self.description.strip()
        unit = self.unit.lower().strip() if self.unit else ""

        if not self.bill_ref and desc and desc == desc.upper() and len(desc) > 3:
            if desc in {"SUBTOTAL", "ADJUSTMENT", "G.S.T", "TOTAL"}:
                return "TOTAL"
            return "TRADE_HEADER"

        if not self.bill_ref and NUMBERED_SUBSECTION_RE.match(desc):
            return "SUBSECTION_HEADER"

        if not self.bill_ref and not self.total:
            return "GROUP_HEADER"

        if unit in NARRATIVE_UNITS:
            return "NARRATIVE"

        if self.rate or (self.total and self.total > 0):
            return "PRICED_ITEM"

        if self.bill_ref and unit in UNPRICED_UNITS:
            return "UNPRICED_ITEM"

        return "UNKNOWN"


def _iter_rows(ws) -> Iterator[_Row]:
    for r in range(2, ws.max_row + 1):
        bill_ref = str(ws.cell(r, 1).value or "").strip()
        desc = str(ws.cell(r, 2).value or "").strip()
        qty = ws.cell(r, 3).value
        unit = str(ws.cell(r, 4).value or "").strip()
        rate = ws.cell(r, 5).value
        total = ws.cell(r, 6).value

        if not any([bill_ref, desc, qty, unit, rate, total]):
            continue

        yield _Row(
            row_num=r,
            bill_ref=bill_ref,
            description=desc,
            quantity=qty if isinstance(qty, (int, float)) else None,
            unit=unit,
            rate=rate if isinstance(rate, (int, float)) else None,
            total=total if isinstance(total, (int, float)) else None,
        )


def _parse_summary(ws) -> tuple[ChunkRecord, set[str]]:
    lines = ["# BoQ Trade Summary"]
    trades: list[dict] = []
    trade_names: set[str] = set()
    skip = {"Subtotal", "Adjustment", "G.S.T", "Total"}
    for r in range(2, ws.max_row + 1):
        desc = ws.cell(r, 1).value
        total = ws.cell(r, 4).value
        if not desc:
            continue
        if isinstance(total, (int, float)):
            desc_s = str(desc).strip()
            lines.append(f"- {desc_s}: €{total:,.2f}")
            trades.append({"trade": desc_s, "total": total})
            if desc_s not in skip:
                trade_names.add(desc_s)
    return ChunkRecord(
        chunk_type="trade_summary",
        chunk_text="\n".join(lines),
        breadcrumb="BoQ Trade Summary",
        metadata={"trades": trades},
    ), trade_names


def _parse_breakup(ws, known_trades: set[str]) -> list[ChunkRecord]:
    chunks: list[ChunkRecord] = []
    trade = ""
    subsection = ""
    group = ""
    sub_items: list[_Row] = []

    def flush_subsection() -> None:
        nonlocal sub_items
        if not sub_items:
            return
        breadcrumb = " › ".join(p for p in [trade, subsection, group] if p)
        priced = [r for r in sub_items if r.kind == "PRICED_ITEM"]
        narrative = [r for r in sub_items if r.kind == "NARRATIVE"]
        unpriced = [r for r in sub_items if r.kind == "UNPRICED_ITEM"]
        subtotal = sum(r.total for r in priced if r.total)

        lines = [f"# {breadcrumb}"]
        if priced:
            lines.append(f"\n## Priced items (subtotal €{subtotal:,.2f})")
            for r in priced:
                rate_s = f"€{r.rate:,.2f}" if r.rate else "—"
                total_s = f"€{r.total:,.2f}" if r.total else "€0.00"
                lines.append(
                    f"- {r.bill_ref} {r.description} — "
                    f"{r.quantity or ''} {r.unit} × {rate_s} = {total_s}"
                )
        if unpriced:
            lines.append("\n## Unpriced / allowance items")
            for r in unpriced:
                lines.append(f"- {r.bill_ref} {r.description}")
        if narrative:
            lines.append("\n## Contract notes")
            for r in narrative:
                lines.append(f"- {r.description}")

        chunks.append(ChunkRecord(
            chunk_type="subsection",
            chunk_text="\n".join(lines),
            breadcrumb=breadcrumb,
            metadata={
                "trade": trade,
                "subsection": subsection,
                "group": group,
                "item_count": len(sub_items),
                "subtotal": subtotal,
                "priced_count": len(priced),
            },
        ))
        sub_items = []

    for row in _iter_rows(ws):
        kind = row.kind
        if kind == "TRADE_HEADER":
            if known_trades and row.description not in known_trades:
                group = row.description
                continue
            flush_subsection()
            trade = row.description
            subsection = ""
            group = ""
        elif kind == "SUBSECTION_HEADER":
            flush_subsection()
            subsection = row.description
            group = ""
        elif kind == "GROUP_HEADER":
            if trade and row.description.isupper() and len(row.description) > 2:
                group = row.description
        elif kind == "PRICED_ITEM":
            sub_items.append(row)
            breadcrumb = " › ".join(p for p in [trade, subsection, group] if p)
            rate_s = f"€{row.rate:,.2f} per {row.unit}" if row.rate else "—"
            total_s = f"€{row.total:,.2f}" if row.total else "€0.00"
            qty_s = f"{row.quantity} {row.unit}" if row.quantity else row.unit
            body = (
                f"[{row.bill_ref}] {row.description}\n"
                f"Context: {breadcrumb}\n"
                f"Quantity: {qty_s}\n"
                f"Rate: {rate_s}\n"
                f"Total: {total_s}"
            )
            chunks.append(ChunkRecord(
                chunk_type="line_item",
                chunk_text=body,
                breadcrumb=breadcrumb,
                metadata={
                    "bill_ref": row.bill_ref,
                    "trade": trade,
                    "subsection": subsection,
                    "group": group,
                    "quantity": row.quantity,
                    "unit": row.unit,
                    "rate": row.rate,
                    "total": row.total,
                },
            ))
        elif kind in ("NARRATIVE", "UNPRICED_ITEM"):
            sub_items.append(row)
        # TOTAL and UNKNOWN are ignored at this level.

    flush_subsection()
    return chunks


def parse(path: Path) -> tuple[DocMeta, list[ChunkRecord]]:
    wb = openpyxl.load_workbook(path, data_only=True)

    known_trades: set[str] = set()
    chunks: list[ChunkRecord] = []

    summary_sheet = next((s for s in wb.sheetnames if "Summary" in s), None)
    if summary_sheet:
        summary_chunk, known_trades = _parse_summary(wb[summary_sheet])
        chunks.append(summary_chunk)

    breakup_sheet = next(
        (s for s in wb.sheetnames if "Breakup" in s and "Markup" not in s), None
    )
    if breakup_sheet:
        chunks.extend(_parse_breakup(wb[breakup_sheet], known_trades))

    meta = DocMeta(
        title=path.stem,
        source_type="boq_xlsx",
        metadata={"sheets": wb.sheetnames, "known_trades": sorted(known_trades)},
    )
    return meta, chunks
