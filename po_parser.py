"""Parse natural-language PO requests using Claude."""

from __future__ import annotations

import json

import anthropic

import config
from models import Supplier, Product, ParsedRequest


def parse_po_request(
    email_body: str,
    suppliers: list[Supplier],
    products: list[Product],
) -> ParsedRequest:
    """Parse a natural-language email into a structured PO request."""
    supplier_list = "\n".join(
        f"- {s.supplier_id}: {s.name}" for s in suppliers
    )
    product_list = "\n".join(
        f"- {p.product_id}: {p.product_name} (supplier: {p.supplier_id}, "
        f"unit: {p.unit}, default_price: {p.default_price})"
        for p in products
    )

    system_prompt = f"""You are a purchase order parsing assistant for {config.COMPANY_NAME}.
Your job is to extract structured PO information from natural-language requests.

Known suppliers:
{supplier_list}

Known products:
{product_list}

Return a JSON object with this exact structure:
{{
  "supplier_name": "matched supplier display name",
  "supplier_id": "matched supplier_id from the list above",
  "items": [
    {{
      "product_name": "matched or described product name",
      "quantity": 16.0,
      "unit": "kg",
      "unit_price": null
    }}
  ],
  "notes": "any additional notes from the request",
  "ambiguities": ["list of anything unclear that needs confirmation"],
  "confidence": 0.95
}}

Rules:
- Match supplier names fuzzily (e.g., "Giveran" matches "Giveran Fragrances").
- Match product names fuzzily against the known products list.
- If a product isn't in the known list, still include it but add a note in ambiguities.
- If quantity or unit is missing, set to null and note it in ambiguities.
- If unit_price is not specified, set to null (the system will use the default price).
- Set confidence between 0 and 1 based on how clear the request is.
- Return ONLY valid JSON, no markdown or explanation."""

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": email_body}],
    )

    text = response.content[0].text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[: -len("```")]
        text = text.strip()

    data = json.loads(text)
    return ParsedRequest(**data)
