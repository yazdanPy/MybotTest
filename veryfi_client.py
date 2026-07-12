# -*- coding: utf-8 -*-
"""
Thin client for Veryfi's Receipts OCR API (https://docs.veryfi.com/), used to
extract line items from a photo of a receipt so the bot can ask "who had this
item?" for each one instead of the user typing every amount by hand.

Only active when VERYFI_CLIENT_ID / VERYFI_USERNAME / VERYFI_API_KEY are all
set (see config.py) -- get these from https://hub.veryfi.com/ under
Settings -> Keys. Nothing else in the bot depends on this module; if it's not
configured, the OCR option is simply not offered.

NOTE: this integration was written against Veryfi's documented v8 API schema
(https://docs.veryfi.com/api/receipts-invoices/process-a-document/) but has
not been exercised against a real Veryfi account (that requires credentials
only you can obtain). If Veryfi ever changes a field name, the error message
surfaced to the user will include the raw response to help debug quickly.
"""
import base64
from dataclasses import dataclass

import httpx

import config

VERYFI_URL = "https://api.veryfi.com/api/v8/partner/documents"


class VeryfiError(Exception):
    """Raised for any Veryfi configuration, network, or API-level failure."""


@dataclass
class ReceiptLineItem:
    description: str
    amount: int  # Toman, rounded to nearest integer


@dataclass
class ReceiptExtraction:
    line_items: list[ReceiptLineItem]
    document_total: int | None
    vendor_name: str | None


async def extract_receipt(image_bytes: bytes, filename: str = "receipt.jpg") -> ReceiptExtraction:
    if not config.VERYFI_ENABLED:
        raise VeryfiError("Veryfi تنظیم نشده. کلیدهای VERYFI_CLIENT_ID / VERYFI_USERNAME / VERYFI_API_KEY رو در .env بذار.")

    headers = {
        "CLIENT-ID": config.VERYFI_CLIENT_ID,
        "AUTHORIZATION": f"apikey {config.VERYFI_USERNAME}:{config.VERYFI_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "file_data": base64.b64encode(image_bytes).decode("ascii"),
        "file_name": filename,
        "document_type": "receipt",
        "boost_mode": False,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(VERYFI_URL, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise VeryfiError(f"اتصال به سرور Veryfi برقرار نشد: {e}") from e

    if resp.status_code in (401, 403):
        raise VeryfiError(
            f"Veryfi کلیدها رو نپذیرفت (کد {resp.status_code}). "
            "VERYFI_CLIENT_ID / VERYFI_USERNAME / VERYFI_API_KEY رو در .env دوباره چک کن."
        )
    if resp.status_code >= 400:
        raise VeryfiError(f"Veryfi خطا برگردوند (کد {resp.status_code}): {resp.text[:300]}")

    try:
        data = resp.json()
    except ValueError as e:
        raise VeryfiError(f"پاسخ Veryfi قابل خواندن نبود: {e}") from e

    raw_items = data.get("line_items") or []
    items: list[ReceiptLineItem] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        desc = (it.get("description") or it.get("text") or "").strip()
        amount_raw = it.get("total")
        if amount_raw is None:
            amount_raw = it.get("price")
        if not desc or amount_raw is None:
            continue
        try:
            amount = round(float(amount_raw))
        except (TypeError, ValueError):
            continue
        if amount == 0:
            continue
        items.append(ReceiptLineItem(description=desc[:200], amount=amount))

    doc_total_raw = data.get("total")
    doc_total = None
    if isinstance(doc_total_raw, (int, float)):
        doc_total = round(float(doc_total_raw))

    vendor_name = None
    vendor_obj = data.get("vendor")
    if isinstance(vendor_obj, dict) and vendor_obj.get("name"):
        vendor_name = str(vendor_obj["name"])[:200]

    if not items:
        raise VeryfiError(
            "هیچ آیتمی توی این عکس پیدا نشد. مطمئن شو عکس واضح از رسیده، یا از تقسیم دستی استفاده کن."
        )

    return ReceiptExtraction(line_items=items, document_total=doc_total, vendor_name=vendor_name)
