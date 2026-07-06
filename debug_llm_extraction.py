#!/usr/bin/env python3
"""Debug LLM entity extraction to see raw responses."""
import sys
import json
sys.path.insert(0, '.')
from query_engine import QueryEngine
from llm_fallback import generate_content_with_openrouter_fallback
from idms_config import EXTRACT_MODEL

engine = QueryEngine()

# Test query that should hit LLM
test_query = "The biggest shareholder is James Wilson"

print(f"Testing LLM extraction on: {test_query}\n")

# Manually test LLM extraction to see raw response
if engine.genai_client:
    prompt = (
        "Extract person or organization names that are EXPLICITLY MENTIONED in this question text. "
        "Return ONLY names that actually appear in the question—do NOT infer, guess, or hallucinate names. "
        "If no clear person or organization name is found, return null.\n"
        "Return ONLY valid JSON with no extra text:\n"
        '{"entity_name": <person or organization name found in question text, or null>}\n'
        f"Question: {json.dumps(test_query)}"
    )
    
    print("Sending to LLM:")
    print(f"Query: {test_query}\n")
    
    try:
        response = generate_content_with_openrouter_fallback(
            primary_call=lambda: engine.genai_client.models.generate_content(
                model=EXTRACT_MODEL,
                contents=prompt,
            ),
            model=EXTRACT_MODEL,
            contents=prompt,
            temperature=0.0,
            call_name="entity_extract_llm",
            complexity="simple",
        )
        raw_text = (response.text or "").strip()
        print(f"Raw LLM response:\n{raw_text}\n")
        
        # Clean and parse
        text = raw_text
        import re
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
        
        print(f"After cleaning code fences:\n{text}\n")
        
        try:
            data = json.loads(text)
            print(f"Parsed JSON: {data}")
            entity = (data.get("entity_name") or "").strip() or None
            print(f"Extracted entity: {entity}")
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}")
            
    except Exception as e:
        print(f"LLM call failed: {e}")
else:
    print("No LLM client available")
