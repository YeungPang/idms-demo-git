from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from idms_query_planner_prompt import build_query_planner_prompt, build_query_planner_request_payload

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

LLM_MODEL = "openai/gpt-4o-mini"

REGRESSION_LOG_QUERIES = [
    "Show me all the themes of documents containing Chung Yeung Pang.",
    "Show me the address of Chung Yeung Pang",
    "Show me the document concerning Chung Yeung Pang",
    "Show me the full information of Chung Yeung Pang",
    "Show me the full personal information of Chung Yeung Pang",
    "Show me the full profile of Chung Yeung Pang",
    "Show me the note for the profile of Chung Yeung Pang",
    "Show me the note concerning Chung Yeung Pang",
    "Show me the note titles and file names of all documents that have description of Chung Yeung Pang",
    "What companies does B. Pang hold shares?",
    "What companies does Chung Yeung Pang hold shares?",
    "What companies does Mr. Pang hold shares?",
    "Who are the shareholders of Aphotonix GmbH and how many shares each one holds?",
    "What day was Aphotonix GmbH registered?",
    "What is the Statute date in the Handelsregisterauszug of Aphotonix GmbH?",
    "What is the total capital of Aphotonix GmbH?",
    "When was Aphotonix GmbH found?",
    "When was Aphotonix GmbH registered?",
    "What are the name and email address of the contact person for the EORI registration from Aphotonix GmbH?",
    "Which document mentioned Benjamin Pang? Please return the file name with the full path.",
    "Was ist der EORI-Nr. von Aphotonix GmbH?",
    "Was ist der email für Kontakt für EORI-Ansprechpartner von Aphotonix GmbH?",
    "Werr ist der EORI-Ansprechpartner von Aphotonix GmbH?",
    "Wie lautet das Statutendatum im Handelsregisterauszug der Aphotonix GmbH?",
    "Wie lautet die Adresse der Aphotonix GmbH?",
    "How many shares of Aphotonix GmbH does B. Pang hold?",
    "Show me the note titles and file names of all documents that are related to Chung Yeung Pang",
    "Which document mentioned Benjamin Pang? Please return the file name with the full path.",
    "What is the invoice due date of the invoice from Kanton Zug to Aphotonix GmbH?",
    "Within how many days must the invoice from Kanton Zug to Aphotonix GmbH be paid?",
    "Show me the full content of the note of personal profile of Chung Yeung Pang",
    "Zeigen Sie mir die E-Mail-Adresse, die in den Kontaktdaten der EORI-Registrierung für die Aphotonix GmbH angegeben ist.",
    "Zeigen Sie mir bitte die Details der Buchführung, die Eori-Nummer für die Aphotonix GmbH beantragt hat.",
    "Show me the company responsible for the eori-number application for Aphotonix GmbH.",
    "Wie lautet die EORI-Nummer der Aphotonix GmbH?",
    "When and where was Chung Yeung Pang born?",
    "Show me the bookkeeping details of the registration for Aphotonix GmbH.",
]

SUPPLEMENTAL_DIRECT_IDMS_QUERIES = [
    "Fetch all the travelling expenses of Aphotonix GmbH in the last 6 months.",
    "How much did the last visit to Italy cost for Chung Yeung Pang?",
    "How many times did Chung Yeung Pang travel to Italy in the last year?",
    "How long did the train ride last in the last trip to Konstanz for Chung Yeung Pang?",
    "Who is the oldest grandchild of Chung Yeung Pang?",
    "When is the next birthday of the grandchild of Chung Yeung Pang and how old would he/she be?",
    "Which document contains the information about laser systems and engineering consultancy? Please return the file name with the full path.",
]

def unique_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


ALL_IDMS_QUERIES = unique_preserving_order(REGRESSION_LOG_QUERIES + SUPPLEMENTAL_DIRECT_IDMS_QUERIES)


# ---------------------------------------------------------------------------
# OpenRouter call
# ---------------------------------------------------------------------------

OPENROUTER_BASE_URL = os.getenv("IDMS_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
LOG_DIR = Path(__file__).parent / "log"


def call_openrouter(query: str, model: str = LLM_MODEL, max_retries: int = 2) -> dict:
    """Send query to OpenRouter and return the parsed JSON plan.

    Retries automatically on transient JSON parse errors (model occasionally
    returns leading text before the JSON object).
    """
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set.")

    url = f"{OPENROUTER_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = build_query_planner_request_payload(query, model)

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        # Strip any accidental leading prose before the JSON object
        json_start = content.find("{")
        if json_start > 0:
            content = content[json_start:]
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            last_exc = exc
            if attempt < max_retries:
                print(f"  [retry {attempt}/{max_retries}] JSON parse error, retrying...")
    raise last_exc


def resolve_query(selector: str) -> tuple[int, str]:
    """Return (1-based index, query text) for a numeric index or literal query string."""
    if selector.strip().isdigit():
        idx = int(selector.strip())
        if idx < 1 or idx > len(ALL_IDMS_QUERIES):
            raise ValueError(f"Index {idx} out of range (1–{len(ALL_IDMS_QUERIES)}).")
        return idx, ALL_IDMS_QUERIES[idx - 1]
    # Treat as literal query text
    text = selector.strip()
    if text in ALL_IDMS_QUERIES:
        return ALL_IDMS_QUERIES.index(text) + 1, text
    return -1, text  # -1 = not in known list


def run_and_log(query: str, idx: int | None, model: str, output_path: Path) -> None:
    """Call OpenRouter, pretty-print result, and append to output_path."""
    label = f"Query #{idx}" if idx and idx > 0 else "Ad-hoc query"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\nModel : {model}")
    print(f"Query : {query}")
    print("Sending to OpenRouter...")

    try:
        result = call_openrouter(query, model=model)
        result_str = json.dumps(result, indent=2, ensure_ascii=False)
        status = "OK"
    except Exception as exc:
        result_str = f"ERROR: {exc}"
        status = "ERROR"

    print(f"Status: {status}")
    print(result_str)

    # Build the log entry
    separator = "=" * 80
    entry = textwrap.dedent(f"""\
        {separator}
        {label}
        Timestamp : {timestamp}
        Model     : {model}
        Status    : {status}
        Query     : {query}
        {'-' * 80}
        {result_str}
        """)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as fh:
        fh.write(entry + "\n")

    print(f"\nAppended to: {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Send an IDMS query to an LLM and log the structured plan."
    )
    parser.add_argument(
        "-q", "--query",
        metavar="QUERY_OR_INDEX",
        help="Query index (1-based) or literal query text to analyse.",
    )
    parser.add_argument(
        "-o", "--output",
        metavar="FILE",
        help="Output log file path. Default: log/query_analysis_YYYYMMDD.log",
    )
    parser.add_argument(
        "-m", "--model",
        default=LLM_MODEL,
        metavar="MODEL",
        help=f"OpenRouter model slug. Default: {LLM_MODEL}",
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="List all available queries and exit.",
    )
    args = parser.parse_args()

    if args.list or not args.query:
        print(f"Regression log queries  : {len(REGRESSION_LOG_QUERIES)}")
        print(f"Supplemental queries    : {len(unique_preserving_order(SUPPLEMENTAL_DIRECT_IDMS_QUERIES))}")
        print(f"All unique IDMS queries : {len(ALL_IDMS_QUERIES)}")
        print()
        for idx, q in enumerate(ALL_IDMS_QUERIES, start=1):
            safe = q.encode("cp1252", errors="replace").decode("cp1252")
            print(f"{idx:3}. {safe}")
        if not args.query:
            sys.exit(0)

    output_path = Path(args.output) if args.output else (
        LOG_DIR / f"query_analysis_{datetime.now().strftime('%Y%m%d')}.log"
    )

    idx, query_text = resolve_query(args.query)
    run_and_log(query_text, idx, model=args.model, output_path=output_path)
