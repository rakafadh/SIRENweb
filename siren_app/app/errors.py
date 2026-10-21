"""Every error the API returns is one readable Indonesian sentence.

Validation errors also come back per field ({"errors": {"systolic": "..."}})
so the page can mark the exact input that needs fixing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("siren.errors")

FIELDS: dict[str, tuple[str, str]] = {
    # field: (label, unit)
    "name": ("Nama", ""),
    "age": ("Usia", " tahun"),
    "sex": ("Jenis kelamin", ""),
    "complaint": ("Keluhan", ""),
    "destination": ("RS tujuan", ""),
    "crew": ("Petugas", ""),
    "acvpu": ("Kesadaran", ""),
    "pain": ("Skala nyeri", ""),
    "systolic": ("Sistolik", " mmHg"),
    "diastolic": ("Diastolik", " mmHg"),
    "hr": ("Nadi", " /menit"),
    "spo2": ("SpO2", "%"),
    "rr": ("Laju napas", " /menit"),
    "temp": ("Suhu", " °C"),
    "text": ("Catatan", ""),
    "param": ("Parameter", ""),
    "reason": ("Alasan", ""),
    "mode": ("Mode sensor", ""),
    "scenario": ("Skenario simulasi", ""),
}


def _number(value: Any) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def describe(error: dict[str, Any]) -> tuple[str, str]:
    """(field or "_form", Indonesian message) for one pydantic error."""
    loc = [part for part in error.get("loc", []) if part != "body"]
    field = str(loc[-1]) if loc else "_form"
    label, unit = FIELDS.get(field, (field, ""))
    kind = error.get("type", "")
    ctx = error.get("ctx") or {}

    if kind == "greater_than_equal":
        return field, f"{label} minimal {_number(ctx.get('ge'))}{unit}."
    if kind == "less_than_equal":
        return field, f"{label} maksimal {_number(ctx.get('le'))}{unit}."
    if kind in {"int_parsing", "float_parsing", "int_from_float", "int_type", "float_type"}:
        return field, f"{label} harus berupa angka{' bulat' if kind.startswith('int') else ''}."
    if kind == "string_too_long":
        return field, f"{label} terlalu panjang (maksimal {ctx.get('max_length')} karakter)."
    if kind == "string_too_short":
        minimum = ctx.get("min_length", 1)
        return field, f"{label} wajib diisi." if minimum <= 1 else f"{label} minimal {minimum} karakter."
    if kind == "missing":
        return field, f"{label} wajib diisi."
    if kind == "literal_error":
        return field, f"Pilihan {label.lower()} tidak dikenal."
    if kind == "bool_parsing":
        return field, f"{label} harus ya atau tidak."
    if kind == "value_error":
        # Our own validators already speak Indonesian
        return field, str(error.get("msg", "")).removeprefix("Value error, ")
    if kind in {"json_invalid", "model_attributes_type", "dict_type"}:
        return "_form", "Data yang dikirim tidak terbaca. Muat ulang halaman lalu coba lagi."
    return field, f"{label}: nilai tidak valid."


async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    errors: dict[str, str] = {}
    for item in exc.errors():
        field, message = describe(item)
        errors.setdefault(field, message)
    summary = next(iter(errors.values()), "Data tidak valid.")
    if len(errors) > 1:
        summary = f"{len(errors)} isian perlu diperbaiki. {summary}"
    return JSONResponse({"detail": summary, "errors": errors}, status_code=422)


async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404 and not request.url.path.startswith("/api/"):
        from app.main import sensor_manager, settings, templates

        return templates.TemplateResponse(
            request, "404.html",
            {"ambulance_id": settings.AMBULANCE_ID, "mock_mode": sensor_manager.mode == "MOCK", "nav": None,
             "page_title": "Tidak ditemukan", "back_href": "/", "back_label": "Beranda", "flow_step": None},
            status_code=404,
        )
    detail = exc.detail
    if not isinstance(detail, str) or detail in {"Not Found", "Method Not Allowed"}:
        detail = {
            404: "Data atau halaman tidak ditemukan.",
            405: "Aksi ini tidak didukung.",
        }.get(exc.status_code, "Permintaan tidak dapat diproses.")
    return JSONResponse({"detail": detail}, status_code=exc.status_code)


async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error", exc_info=exc)
    return JSONResponse(
        {"detail": "Terjadi kesalahan di server SIREN. Coba lagi; jika berulang, mulai ulang perangkat."},
        status_code=500,
    )
