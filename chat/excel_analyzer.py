from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import pandas as pd


EXCEL_EXTENSIONS = {".xls", ".xlsx", ".csv"}


def is_excel_file(filename: str) -> bool:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in EXCEL_EXTENSIONS


def _safe_float(v):
    try:
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def _safe_value(v):
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    return str(v)


def _detect_possible_id_columns(columns: List[str]) -> List[str]:
    keys = ["id", "code", "no", "number", "branch", "member", "customer", "loan", "account"]
    out = []
    for col in columns:
        c = col.lower()
        if any(k in c for k in keys):
            out.append(col)
    return out


def _detect_possible_amount_columns(columns: List[str]) -> List[str]:
    keys = ["amount", "balance", "paid", "due", "loan", "savings", "cash", "total", "fee", "installment"]
    out = []
    for col in columns:
        c = col.lower()
        if any(k in c for k in keys):
            out.append(col)
    return out


def _detect_possible_date_columns(columns: List[str]) -> List[str]:
    keys = ["date", "day", "month", "year", "created", "updated", "due"]
    out = []
    for col in columns:
        c = col.lower()
        if any(k in c for k in keys):
            out.append(col)
    return out


def _read_file(file_path: str) -> Dict[str, pd.DataFrame]:
    ext = os.path.splitext(file_path or "")[1].lower()

    if ext == ".csv":
        return {"CSV": pd.read_csv(file_path)}

    if ext == ".xlsx":
        return pd.read_excel(file_path, sheet_name=None, engine="openpyxl")

    if ext == ".xls":
        return pd.read_excel(file_path, sheet_name=None, engine="xlrd")

    raise ValueError(f"Unsupported Excel/CSV file type: {ext}")


def analyze_excel_file(file_path: str, max_sample_rows: int = 20) -> Dict[str, Any]:
    sheets = _read_file(file_path)
    workbook_summary = {
        "file_path": file_path,
        "sheet_count": len(sheets),
        "sheets": {},
    }

    for sheet_name, df in sheets.items():
        df = df.copy()

        # Remove fully empty rows/columns
        df = df.dropna(how="all")
        df = df.dropna(axis=1, how="all")

        # Clean column names
        df.columns = [str(c).strip() for c in df.columns]
        columns = df.columns.tolist()

        # Try convert possible numeric columns
        for col in columns:
            if df[col].dtype == "object":
                cleaned = (
                    df[col]
                    .astype(str)
                    .str.replace(",", "", regex=False)
                    .str.replace("৳", "", regex=False)
                    .str.strip()
                )
                converted = pd.to_numeric(cleaned, errors="coerce")
                if converted.notna().sum() >= max(3, int(len(df) * 0.5)):
                    df[col] = converted

        numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
        text_cols = df.select_dtypes(include=["object"]).columns.tolist()
        date_cols = df.select_dtypes(include=["datetime64[ns]", "datetime64[ns, UTC]"]).columns.tolist()

        missing_values = {}
        for col in columns:
            missing_count = int(df[col].isna().sum())
            missing_values[col] = {
                "missing_count": missing_count,
                "missing_percent": round((missing_count / len(df) * 100), 2) if len(df) else 0,
            }

        numeric_summary = {}
        for col in numeric_cols:
            numeric_summary[col] = {
                "count": int(df[col].count()),
                "sum": _safe_float(df[col].sum()),
                "average": _safe_float(df[col].mean()),
                "minimum": _safe_float(df[col].min()),
                "maximum": _safe_float(df[col].max()),
            }

        top_values = {}
        for col in text_cols[:20]:
            vc = df[col].astype(str).replace("nan", "").value_counts().head(10)
            top_values[col] = {str(k): int(v) for k, v in vc.items() if str(k).strip()}

        correlations = {}
        if len(numeric_cols) >= 2:
            corr = df[numeric_cols].corr(numeric_only=True).round(3)
            correlations = corr.fillna("").to_dict()

        possible_relationships = {
            "possible_id_columns": _detect_possible_id_columns(columns),
            "possible_amount_columns": _detect_possible_amount_columns(columns),
            "possible_date_columns": _detect_possible_date_columns(columns),
            "numeric_correlation_available": len(numeric_cols) >= 2,
        }

        data_quality_flags = []
        for col, info in missing_values.items():
            if info["missing_percent"] >= 30:
                data_quality_flags.append(
                    f"Column '{col}' has high missing value rate: {info['missing_percent']}%."
                )

        duplicate_rows = int(df.duplicated().sum())
        if duplicate_rows > 0:
            data_quality_flags.append(f"Found {duplicate_rows} duplicate rows.")

        workbook_summary["sheets"][sheet_name] = {
            "row_count": int(df.shape[0]),
            "column_count": int(df.shape[1]),
            "columns": columns,
            "numeric_columns": numeric_cols,
            "text_columns": text_cols,
            "date_columns": date_cols,
            "missing_values": missing_values,
            "numeric_summary": numeric_summary,
            "top_values": top_values,
            "correlations": correlations,
            "possible_relationships": possible_relationships,
            "data_quality_flags": data_quality_flags,
            "sample_rows": df.head(max_sample_rows).fillna("").astype(str).to_dict(orient="records"),
        }

    return workbook_summary


def build_excel_context_for_llm(file_path: str) -> str:
    analysis = analyze_excel_file(file_path)

    payload = {
        "role": "structured_excel_profile",
        "instruction": (
            "This is a structured profile of an uploaded Excel/CSV file. "
            "Use it to answer management decision questions. "
            "Explain column meaning, numeric summaries, missing values, relationships, "
            "data quality risks, and decision recommendations."
        ),
        "analysis": analysis,
    }

    return json.dumps(payload, ensure_ascii=False, indent=2)
