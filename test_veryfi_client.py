# -*- coding: utf-8 -*-
"""
Tests veryfi_client.py against MOCKED HTTP responses shaped like Veryfi's
documented v8 schema (https://docs.veryfi.com/api/receipts-invoices/process-a-document/).
We have no real Veryfi account/credentials to test against from this sandbox
(and api.veryfi.com isn't network-reachable here anyway), so this validates the
parsing/error-handling logic in isolation, which is what's actually ours to get
right -- the wire format itself is Veryfi's documented contract.
"""
import asyncio
import os
from unittest.mock import AsyncMock, patch

os.environ["VERYFI_CLIENT_ID"] = "test_client_id"
os.environ["VERYFI_USERNAME"] = "test_user"
os.environ["VERYFI_API_KEY"] = "test_key"

import importlib
import config
importlib.reload(config)  # pick up the env vars set above
import veryfi_client
importlib.reload(veryfi_client)

failures = []


def check(name, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


class FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        return self._json_data


async def main():
    check("VERYFI_ENABLED true when all 3 keys set", config.VERYFI_ENABLED is True)

    # ---- Happy path: realistic mocked receipt response ----
    mocked_response = FakeResponse(201, json_data={
        "id": 12345,
        "vendor": {"name": "رستوران سنتی"},
        "total": 850000,
        "line_items": [
            {"id": 1, "description": "چلو کباب کوبیده", "total": 250000, "price": 250000},
            {"id": 2, "description": "زرشک پلو با مرغ", "total": 220000, "price": 220000},
            {"id": 3, "description": "نوشابه", "total": 0, "price": 0},  # zero-amount line, should be skipped
            {"id": 4, "description": "مالیات بر ارزش افزوده", "total": 45000},
            {"id": 5, "description": None, "total": 30000},  # missing description, should be skipped
        ],
    })
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mocked_response)):
        result = await veryfi_client.extract_receipt(b"fake image bytes", "receipt.jpg")

    check("extracted 3 valid line items (skipped zero-amount and no-description ones)", len(result.line_items) == 3)
    check("first item description correct", result.line_items[0].description == "چلو کباب کوبیده")
    check("first item amount correct (int)", result.line_items[0].amount == 250000)
    check("document_total parsed correctly", result.document_total == 850000)
    check("vendor_name parsed correctly", result.vendor_name == "رستوران سنتی")

    # ---- Falls back to 'price' when 'total' is missing ----
    mocked_response2 = FakeResponse(201, json_data={
        "line_items": [{"description": "آیتم بدون total", "price": 99000}],
    })
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mocked_response2)):
        result2 = await veryfi_client.extract_receipt(b"fake", "r.jpg")
    check("falls back to 'price' field when 'total' missing", result2.line_items[0].amount == 99000)

    # ---- Auth error (401) surfaces a clear Persian error, not a crash ----
    mocked_401 = FakeResponse(401, text="Unauthorized")
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mocked_401)):
        try:
            await veryfi_client.extract_receipt(b"fake", "r.jpg")
            check("401 raises VeryfiError", False)
        except veryfi_client.VeryfiError as e:
            check("401 raises VeryfiError with helpful message", "کلید" in str(e))

    # ---- Server error (500) ----
    mocked_500 = FakeResponse(500, text="Internal Server Error")
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mocked_500)):
        try:
            await veryfi_client.extract_receipt(b"fake", "r.jpg")
            check("500 raises VeryfiError", False)
        except veryfi_client.VeryfiError:
            check("500 raises VeryfiError", True)

    # ---- Empty line_items (e.g. blurry photo) raises a clear, actionable error ----
    mocked_empty = FakeResponse(201, json_data={"line_items": [], "total": None})
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mocked_empty)):
        try:
            await veryfi_client.extract_receipt(b"fake", "r.jpg")
            check("empty line_items raises VeryfiError", False)
        except veryfi_client.VeryfiError as e:
            check("empty line_items raises VeryfiError with actionable message", "پیدا نشد" in str(e))

    # ---- Not configured at all ----
    config.VERYFI_ENABLED = False
    try:
        await veryfi_client.extract_receipt(b"fake", "r.jpg")
        check("unconfigured Veryfi raises VeryfiError", False)
    except veryfi_client.VeryfiError:
        check("unconfigured Veryfi raises VeryfiError", True)
    config.VERYFI_ENABLED = True

    print()
    if failures:
        print(f"*** {len(failures)} TEST(S) FAILED: {failures}")
        raise SystemExit(1)
    else:
        print("ALL VERYFI CLIENT TESTS PASSED")


asyncio.run(main())
