#!/usr/bin/env python3
"""Fill versioned workbooks by semantic keys, without replacing their layout."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil

import yaml

from release_common import read_json, require


def lookup(row, dotted):
    current = row
    for key in dotted.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def display(value):
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "default" if value is None else value


def matches(row, selector):
    return all(str(display(lookup(row, key))) == str(display(value)) for key, value in selector.items())


def export(results_path, mapping_path, in_place=False):
    from openpyxl import load_workbook
    from openpyxl.comments import Comment
    from openpyxl.cell.cell import MergedCell

    results = read_json(results_path)["cases"]
    mapping_path = Path(mapping_path).resolve()
    config = yaml.safe_load(mapping_path.read_text())
    written = []
    for item in config["workbooks"]:
        source = (mapping_path.parent / item["input"]).resolve()
        target = (mapping_path.parent / item.get("output", item["input"])).resolve()
        require(source.suffix == ".xlsx" and target.suffix == ".xlsx", "Only xlsx workbooks are supported")
        require(source != target or in_place, "Replacing the input requires --in-place")
        require(source == target or not target.exists(), "Output workbook already exists")
        book = load_workbook(source)
        changes = []

        def fill(cell, row, field):
            require(not isinstance(cell, MergedCell), f"Cannot write a merged child cell: {cell.coordinate}")
            value = lookup(row, field)
            if field.startswith("result.metrics.") and row["result"]["status"] != "PASS" and not item.get("include_failed_metrics", False):
                value = "N/A"
            cell.value = display(value)
            provenance = f"case_id={row['case_id']}\nstatus={row['result']['status']}\nattempt={row['result'].get('attempt','pending')}\nfield={field}"
            existing = cell.comment.text + "\n\n" if cell.comment else ""
            cell.comment = Comment(existing + provenance, "xllm-release-validation")
            changes.append(cell.coordinate)

        for sheet in item["sheets"]:
            worksheet = book[sheet["name"]]
            candidates = [r for r in results if matches(r, sheet.get("case_selector", {}))]
            if sheet.get("layout", "rows") == "columns":
                key_column = sheet.get("key_column", "A")
                rows = {}
                for cells in worksheet.iter_rows():
                    cell = worksheet[f"{key_column}{cells[0].row}"]
                    if cell.value in sheet["fields"]:
                        require(cell.value not in rows, f"Duplicate feature label: {cell.value}")
                        rows[cell.value] = cell.row
                require(set(rows) == set(sheet["fields"]), "Some feature labels were not found")
                for column, selector in sheet["case_columns"].items():
                    selected = [r for r in candidates if matches(r, selector)]
                    require(len(selected) == 1, f"Feature column must select one case: {column}")
                    for label, field in sheet["fields"].items():
                        fill(worksheet[f"{column}{rows[label]}"], selected[0], field)
                continue
            header_row = sheet.get("header_row", 1)
            headers = {}
            wanted = set(sheet["match"]) | set(sheet["fill"])
            for cell in worksheet[header_row]:
                if cell.value in wanted:
                    require(cell.value not in headers, f"Duplicate mapped header: {cell.value}")
                    headers[cell.value] = cell.column
            require(wanted <= headers.keys(), f"Mapped headers missing: {wanted - headers.keys()}")
            for number in range(header_row + 1, worksheet.max_row + 1):
                selector = {}
                for label, field in sheet["match"].items():
                    raw = worksheet.cell(number, headers[label]).value
                    selector[field] = sheet.get("value_maps", {}).get(label, {}).get(str(raw), raw)
                selected = [r for r in candidates if matches(r, selector)]
                require(len(selected) <= 1, f"Workbook row matches multiple cases: {sheet['name']} row {number}")
                if selected:
                    for label, field in sheet["fill"].items():
                        fill(worksheet.cell(number, headers[label]), selected[0], field)
        require(changes, "No workbook cells matched the release cases")
        if source == target:
            backup = source.with_name(source.stem + ".backup-" + datetime.now().strftime("%Y%m%d%H%M%S%f") + ".xlsx")
            shutil.copy2(source, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.stem + ".writing.xlsx")
        book.save(temporary)
        # Verify saved values and readability before replacing the destination.
        verified = load_workbook(temporary)
        require(verified.sheetnames == book.sheetnames, "Workbook sheets changed during save")
        for name in book.sheetnames:
            for row in book[name].iter_rows():
                for cell in row:
                    expected = None if cell.value == "" else cell.value
                    require(verified[name][cell.coordinate].value == expected, f"Workbook verification failed: {cell.coordinate}")
        temporary.replace(target)
        written.append({"output": str(target), "cells": changes})
    return written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--in-place", action="store_true")
    args = parser.parse_args()
    print(json.dumps(export(args.results, args.mapping, args.in_place), ensure_ascii=False, indent=2))
