#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import requests

RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "primary_topic": {"type": "string", "description": "Kort nøgleord for hovedemnet"},
        "political_alignment": {"type": "string", "description": "F.eks. venstrefløjen, højrefløjen, liberal, konservativ"},
        "actors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Navne på personer, partier eller organisationer nævnt i indlægget",
        },
        "geography": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Geografiske steder eller niveauer (Danmark, EU, Aarhus, globalt)",
        },
        "tone": {"type": "string", "description": "Stemning/tonalitet, fx kritisk, optimistisk, vred"},
        "social_themes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Sociale temaer, fx klima, migration, velfærd",
        },
        "hashtags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Hashtags i indlægget (uden # hvis muligt).",
        },
        "engagement_level": {
            "type": "string",
            "enum": ["unknown", "low", "medium", "high"],
            "description": "Bedømt ud fra likes/kommentarer/delinger hvis nævnt",
        },
        "meta_tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Liste med korte tags - brugs til word clouds",
        },
    },
    "required": [
        "primary_topic",
        "political_alignment",
        "actors",
        "geography",
        "tone",
        "social_themes",
        "hashtags",
        "engagement_level",
        "meta_tags",
    ],
}

PROMPT_TEMPLATE = """Du er ekspert i politisk analyse ogskal analysere følgende Facebook-opslag og returnere metadata.

Følg disse trin:
1. Identificer det primære emne (økonomi, sundhed, klima, transport, osv.).
2. Angiv geografisk fokus (fx Danmark, EU, Aarhus).
3. Beskriv tonen (kritisk, optimistisk, vred, satirisk, alvorlig, neutral).
4. Find sociale emner eller tendenser (fx klimaændringer, migration, velfærd).
5. Medtag hashtags hvis de optræder.
6. Angiv engagement-niveauet (low/medium/high) hvis likes/kommentarer/delinger er nævnt, ellers "unknown".
7. Opret et sæt meta-tags (3-10 korte korte tags) der sammenfatter indholdet, fx ["økonomi", "venstrefløjen", "kritisk"].

Tildel minimum 3 meta-tags og max 6 til indholdet, hvis du har flere tags end 6 vælg de mest relevante.

Tekst: <<<POST_START>>>
{post_text}
<<<POST_END>>>

Supplerende information:
- Parti: {party_code}
- Forfatter: {author_name}
- Link: {post_link}

Returnér kun gyldigt JSON med denne struktur (brug dobbelte anførselstegn):
{{
  "primary_topic": "...",
  "political_alignment": "...",
  "actors": ["..."],
  "geography": ["..."],
  "tone": "...",
  "social_themes": ["..."],
  "hashtags": ["..."],
  "engagement_level": "low|medium|high|unknown",
  "meta_tags": ["..."]
}}
"""


def _get_openai_config() -> Dict[str, Optional[str]]:
    model_env = os.getenv("OPENAI_METADATA_MODEL") or "gpt-4o-mini"
    model = model_env.strip()
    if model:
        model = model.lower()
    return {
        "api_key": os.getenv("OPENAI_API_KEY"),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "model": model or "gpt-4o-mini",
        "system_prompt": (
            os.getenv("OPENAI_METADATA_SYSTEM_PROMPT")
            or "Du er en hjælpsom politisk analytiker, der skriver på dansk og giver neutrale, kortfattede observationer."
        ),
    }


def generate_post_metadata(
    post_text: str,
    *,
    party_code: Optional[str] = None,
    author_name: Optional[str] = None,
    post_link: Optional[str] = None,
) -> Dict[str, Any]:
    config = _get_openai_config()
    api_key = config["api_key"]
    if not post_text or not api_key:
        return {}

    payload = {
        "model": config["model"],
        "input": [
            {"role": "system", "content": config["system_prompt"]},
            {
                "role": "user",
                "content": PROMPT_TEMPLATE.format(
                    post_text=post_text.strip(),
                    party_code=party_code or "ukendt",
                    author_name=author_name or "ukendt",
                    post_link=post_link or "ukendt",
                ),
            },
        ],
    }

    try:
        response = requests.post(
            f"{config['base_url']}/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        return _extract_json_payload(response.json())
    except requests.HTTPError as exc:
        detail = ""
        if exc.response is not None:
            try:
                detail = exc.response.text
            except Exception:
                detail = ""
        print(f"[META] Failed to generate metadata: {exc} {detail}", flush=True)
        return {}
    except Exception as exc:
        print(f"[META] Failed to generate metadata: {exc}", flush=True)
        return {}


def _extract_json_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    output = data.get("output") or data.get("choices") or []
    text_payload = ""
    if isinstance(output, list) and output:
        first = output[0]
        content = first.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    text_payload += str(block["text"])
        elif isinstance(content, str):
            text_payload = content
    elif "output_text" in data:
        text_payload = data.get("output_text", "")

    if not text_payload:
        return {}

    def _try_parse(raw: str) -> Dict[str, Any] | None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    parsed = _try_parse(text_payload)
    if parsed is None:
        cleaned = text_payload.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`").strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        parsed = _try_parse(cleaned)

    if parsed is None:
        print(f"[META] Unable to parse metadata JSON payload: {text_payload}", flush=True)
        return {}

    # Ensure meta_tags exists and is list
    meta_tags = parsed.get("meta_tags")
    if not isinstance(meta_tags, list):
        parsed["meta_tags"] = [tag.strip() for tag in str(meta_tags or "").split(",") if tag.strip()]
    return parsed


__all__ = ["generate_post_metadata"]

