import hashlib
import json
import os
import re
import uuid
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    HnswConfigDiff,
    OptimizersConfigDiff,
    PayloadSchemaType,
    PointStruct,
    SparseIndexParams,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from sql_db import (
    create_tables,
    get_connection,
    insert_attribute,
    insert_document,
    insert_part,
    upsert_edge,
    upsert_node,
)


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "idms_proto")
IDMS_RECREATE_SCHEMA = os.getenv("IDMS_RECREATE_SCHEMA", "false").lower() == "true"

DENSE_MODEL = "text-embedding-3-small"

doc_path = os.path.abspath("./data/German-EORI-OCR.pdf")

input_json = """
{
  "theme": "Erteilung einer EORI-Nummer an das Unternehmen APHOTONIX GmbH durch die Generalzolldirektion Dresden.",
  "document_category": [
    "government_record"
  ],
  "keywords": [
    "EORI-Nummer",
    "EORI number",
    "Generalzolldirektion",
    "Zoll",
    "Stammdatenmanagement",
    "APHOTONIX GmbH",
    "Dienstort Dresden",
    "Zoll-Portal",
    "ELSTER-Zertifikat",
    "Wirtschaftsbeteiligter",
    "economic operator",
    "customs",
    "servicekonto",
    "EORI-Verwaltung",
    "Ort der Buchf\u00fchrung",
    "EORI-Ansprechpartner"
  ],
  "identifiers_key_value_pairs": {
    "EORI-Nummer": "DE892284377081981",
    "GZ": "O 1930 D - 01855496",
    "Datum": "12. Februar 2026",
    "Niederlassungsnummer": "0000",
    "Hauptwirtschaftsaktivit\u00e4t": "4690",
    "Rechtsform": "ausl. Rechtsform",
    "Zustimmung zur Internetver\u00f6ffentlichung": "ja",
    "Gr\u00fcndungsdatum": "23.09.2025",
    "Telefonnummer Unternehmen": "0041 415100300",
    "E-Mail-Adresse Unternehmen": "services@ynoves.ch",
    "Telefonnummer Ansprechpartner": "+41 (0) 415100300",
    "E-Mail-Adresse Ansprechpartner": "services@ynoves.ch",
    "Postanschrift Beh\u00f6rde": "Generalzolldirektion, Postfach 100761, 01077 Dresden",
    "Dienstgeb\u00e4ude": "Carusufer 3-5, 01099 Dresden",
    "Bearbeitet von E-Mail": "stammdatenmanagement@zoll.bund.de",
    "Bearbeitet von Telefon": "0228 303 26050",
    "Bearbeitet von Fax": "0228 303 98800"
  },
  "entities": [
    {
      "entity_id": "e1",
      "entity_type": "ORGANISATION",
      "entity_name": "APHOTONIX GmbH",
      "attributes": {
        "org_name": "APHOTONIX GmbH",
        "legal_name": "APHOTONIX GmbH",
        "org_type": "ausl. Rechtsform",
        "address_street": "Neugasse 4",
        "address_postal_code": "6300",
        "address_city": "Zug",
        "address_country": "CH",
        "phone": "0041 415100300",
        "email": "services@ynoves.ch",
        "eori_number": "DE892284377081981",
        "registration_number": "0000",
        "industry": "4690",
        "founded_date": "23.09.2025",
        "notes": "Ans\u00e4ssigkeit im Zollgebiet der Union: nein",
        "source_text": "Name/Firmenbezeichnung: APHOTONIX GmbH, Niederlassungsnummer: 0000, Stra\u00dfe und Hausnummer: Neugasse 4, Postleitzahl/Ort: 6300 Zug, L\u00e4ndercode: CH, Ans\u00e4ssigkeit im Zollgebiet der Union: nein, Hauptwirtschaftsaktivit\u00e4t: 4690, Rechtsform: ausl. Rechtsform, Datum der Gr\u00fcndung: 23.09.2025"
      },
      "source_text": "Name/Firmenbezeichnung: APHOTONIX GmbH",
      "confidence": 0.98
    },
    {
      "entity_id": "e2",
      "entity_type": "ORGANISATION",
      "entity_name": "Ynoves AG",
      "attributes": {
        "org_name": "Ynoves AG",
        "address_street": "Hinterbergstrasse 16",
        "address_postal_code": "6312",
        "address_city": "Steinhausen",
        "address_country": "CH",
        "phone": "+41 (0) 415100300",
        "email": "services@ynoves.ch",
        "notes": "Ort der Buchf\u00fchrung",
        "source_text": "Bezeichnung: Ynoves AG, Stra\u00dfe und Hausnummer: Hinterbergstrasse 16, Postleitzahl/Ort: 6312 Steinhausen, L\u00e4ndercode: CH"
      },
      "source_text": "Bezeichnung: Ynoves AG",
      "confidence": 0.95
    },
    {
      "entity_id": "e3",
      "entity_type": "PERSON",
      "entity_name": "Daniel Trottmann",
      "attributes": {
        "full_name": "Daniel Trottmann",
        "address_street": "Hinterbergstrasse 16",
        "address_postal_code": "6312",
        "address_city": "Steinhausen",
        "address_country": "CH",
        "phone": "+41 (0) 415100300",
        "email": "services@ynoves.ch",
        "role_description": "EORI-Ansprechpartner",
        "source_text": "Vollst\u00e4ndiger Name: Daniel Trottmann, Stra\u00dfe und Hausnummer: Hinterbergstrasse 16, Postleitzahl: 6312, Ort: Steinhausen, L\u00e4ndercode: CH, Telefonnummer: +41 (0) 415100300, E-Mail-Adresse: services@ynoves.ch"
      },
      "source_text": "Vollst\u00e4ndiger Name: Daniel Trottmann",
      "confidence": 0.97
    }
  ],
  "relationships": [
    {
      "relationship_type": "issued_by",
      "source_entity_id": "e1",
      "target_entity_id": "e3",
      "attributes": {
        "document_type": "EORI-Nummer",
        "issue_date": "12.02.2026",
        "reference_number": "DE892284377081981"
      },
      "source_text": "mit Wirkung vom 12.02.2026 wurde Ihrem Unternehmen die oben genannte EORI-Nummer erteilt.",
      "confidence": 0.98
    }
  ]
}
"""


def test_qdrant_module_smoke() -> None:
    assert isinstance(QDRANT_COLLECTION, str)
    assert bool(DENSE_MODEL.strip())


def _get_openai_client() -> OpenAI:
    return OpenAI(api_key=OPENAI_API_KEY)


def _get_qdrant_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL, api_key=os.getenv("QDRANT_API_KEY"))


def stable_token_index(token: str) -> int:
    digest = hashlib.md5(token.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def encode_sparse(text: str) -> tuple[list[int], list[float]]:
    tokens = re.findall(r"[\w\-/.:]+", text.lower())
    freq: dict[int, float] = {}
    for token in tokens:
        idx = stable_token_index(token)
        freq[idx] = freq.get(idx, 0.0) + 1.0

    indices = list(freq.keys())
    values = list(freq.values())
    return indices, values


def embed_text(text: str) -> list[float]:
    response = _get_openai_client().embeddings.create(model=DENSE_MODEL, input=text)
    return response.data[0].embedding


def ensure_qdrant_collection(collection_name: str = QDRANT_COLLECTION) -> None:
    qdrant = _get_qdrant_client()
    existing = {c.name for c in qdrant.get_collections().collections}
    if collection_name not in existing:
        qdrant.create_collection(
            collection_name=collection_name,
            vectors_config={
                "dense": VectorParams(size=1536, distance=Distance.COSINE),
            },
            sparse_vectors_config={
                "sparse": SparseVectorParams(index=SparseIndexParams()),
            },
            optimizers_config=OptimizersConfigDiff(
                indexing_threshold=20000,
                memmap_threshold=20000,
                default_segment_number=8,
            ),
            hnsw_config=HnswConfigDiff(m=32, ef_construct=200, full_scan_threshold=10000),
            on_disk_payload=True,
        )

    for field_name, schema in [
        ("doc_id", PayloadSchemaType.INTEGER),
        ("node_id", PayloadSchemaType.INTEGER),
        ("edge_id", PayloadSchemaType.INTEGER),
        ("feature_type", PayloadSchemaType.KEYWORD),
        ("entry_date", PayloadSchemaType.DATETIME),
        ("valid_from", PayloadSchemaType.DATETIME),
        ("valid_until", PayloadSchemaType.DATETIME),
    ]:
        try:
            qdrant.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=schema,
            )
        except Exception:
            # Index may already exist.
            pass


def upsert_dense_vectors_batch(
    items: list[dict],
    collection_name: str = QDRANT_COLLECTION,
) -> list[str]:
    qdrant = _get_qdrant_client()
    points: list[PointStruct] = []
    point_ids: list[str] = []
    for item in items:
        point_id = item.get("point_id") or str(uuid.uuid4())
        text = item["text"]
        payload = dict(item.get("payload") or {})
        payload.setdefault("text", text)
        points.append(
            PointStruct(id=point_id, vector={"dense": embed_text(text)}, payload=payload)
        )
        point_ids.append(point_id)

    if points:
        qdrant.upsert(collection_name=collection_name, points=points, wait=False)
    return point_ids


def upsert_sparse_vectors_batch(
    items: list[dict],
    collection_name: str = QDRANT_COLLECTION,
) -> list[str]:
    qdrant = _get_qdrant_client()
    points: list[PointStruct] = []
    point_ids: list[str] = []
    for item in items:
        point_id = item.get("point_id") or str(uuid.uuid4())
        text = item["text"]
        payload = dict(item.get("payload") or {})
        payload.setdefault("text", text)
        indices, values = encode_sparse(text)
        points.append(
            PointStruct(
                id=point_id,
                vector={"sparse": SparseVector(indices=indices, values=values)},
                payload=payload,
            )
        )
        point_ids.append(point_id)

    if points:
        qdrant.upsert(collection_name=collection_name, points=points, wait=False)
    return point_ids


def flatten_identifiers_for_sparse(identifiers_kv: dict) -> str:
    terms: list[str] = []
    for k, v in identifiers_kv.items():
        key = str(k).strip()
        value = str(v).strip()
        key_norm = re.sub(r"\s+", "_", key.lower())
        terms.extend([key, value, f"{key}:{value}", f"{key_norm}:{value}"])
    return "\n".join(terms)


def parse_date_maybe(value: str | None) -> date | None:
    if not value:
        return None

    raw = value.strip()
    formats = ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    german_months = {
        "januar": "01",
        "februar": "02",
        "maerz": "03",
        "märz": "03",
        "april": "04",
        "mai": "05",
        "juni": "06",
        "juli": "07",
        "august": "08",
        "september": "09",
        "oktober": "10",
        "november": "11",
        "dezember": "12",
    }
    m = re.match(r"^(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s+(\d{4})$", raw)
    if m:
        day, month_name, year = m.groups()
        month = german_months.get(month_name.lower())
        if month:
            return datetime.strptime(f"{year}-{month}-{int(day):02d}", "%Y-%m-%d").date()

    return None


def detect_doc_date(identifiers_kv: dict, relationships: list[dict]) -> date | None:
    date_candidates = [
        identifiers_kv.get("doc_date"),
        identifiers_kv.get("document_date"),
        identifiers_kv.get("Datum"),
        identifiers_kv.get("datum"),
        identifiers_kv.get("issue_date"),
    ]

    for rel in relationships:
        attrs = rel.get("attributes") or {}
        date_candidates.append(attrs.get("issue_date"))
        date_candidates.append(attrs.get("date"))

    for candidate in date_candidates:
        parsed = parse_date_maybe(str(candidate)) if candidate else None
        if parsed:
            return parsed
    return None


def detect_validity(identifiers_kv: dict, entry_date: date) -> tuple[date, date | None]:
    valid_from = (
        parse_date_maybe(str(identifiers_kv.get("valid_from")))
        or parse_date_maybe(str(identifiers_kv.get("gueltig_ab")))
        or parse_date_maybe(str(identifiers_kv.get("gültig_ab")))
        or entry_date
    )
    valid_until = (
        parse_date_maybe(str(identifiers_kv.get("valid_until")))
        or parse_date_maybe(str(identifiers_kv.get("gueltig_bis")))
        or parse_date_maybe(str(identifiers_kv.get("gültig_bis")))
    )
    return valid_from, valid_until


def normalize_category(value: str | list[str] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return value[0] if value else None
    return str(value)


def ingest_document_into_relational_and_vectors(
    document_payload: dict,
    doc_name: str,
    doc_path: str | None = None,
) -> dict:
    ensure_qdrant_collection()

    theme = (document_payload.get("theme") or "").strip()
    doc_type = normalize_category(document_payload.get("document_category"))
    keywords = [str(k) for k in (document_payload.get("keywords") or []) if str(k).strip()]
    identifiers_kv = document_payload.get("identifiers_key_value_pairs") or {}
    entities = document_payload.get("entities") or []
    relationships = document_payload.get("relationships") or []

    doc_date = detect_doc_date(identifiers_kv, relationships)
    entry_now = datetime.utcnow()
    valid_from, valid_until = detect_validity(identifiers_kv, entry_now.date())

    conn = get_connection()
    try:
        create_tables(conn, recreate=IDMS_RECREATE_SCHEMA)

        doc_id, entry_date_text = insert_document(
            conn=conn,
            doc_name=doc_name,
            doc_path=doc_path,
            doc_type=doc_type,
            doc_date=doc_date,
            doc_desc=theme,
            keywords=keywords,
            identifiers_kv=identifiers_kv,
            metadata={"category_raw": document_payload.get("document_category")},
            valid_from=valid_from,
            valid_until=valid_until,
        )

        doc_part_id = insert_part(
            conn=conn,
            doc_id=doc_id,
            part_key=f"document:{doc_id}",
            part_type="document",
        )
        insert_attribute(
            conn=conn,
            src_id=doc_part_id,
            src_type="part",
            attr_type="document_theme",
            attr_json={"theme": theme},
            doc_id=doc_id,
            valid_from=valid_from,
            valid_until=valid_until,
        )

        entity_to_node_id: dict[str, int] = {}
        entity_to_name: dict[str, str] = {}
        dense_items: list[dict] = []

        common_payload = {
            "doc_id": doc_id,
            "entry_date": entry_date_text,
            "valid_from": valid_from.isoformat() if valid_from else None,
            "valid_until": valid_until.isoformat() if valid_until else None,
        }

        # Theme into dense vector.
        if theme:
            dense_items.append(
                {
                    "text": theme,
                    "payload": {
                        **common_payload,
                        "feature_type": "document_theme",
                    },
                }
            )

        for entity in entities:
            entity_key = str(entity.get("entity_id") or "")
            entity_name = str(entity.get("entity_name") or "").strip()
            entity_type = str(entity.get("entity_type") or "OTHER").strip()
            entity_attrs = dict(entity.get("attributes") or {})
            source_text = entity.get("source_text") or entity_attrs.pop("source_text", None)

            node_id = upsert_node(
                conn=conn,
                node_name=entity_name,
                node_type=entity_type,
                metadata={"confidence": entity.get("confidence")},
            )
            if entity_key:
                entity_to_node_id[entity_key] = node_id
                entity_to_name[entity_key] = entity_name

            insert_attribute(
                conn=conn,
                src_id=node_id,
                src_type="node",
                attr_type="entity_attributes",
                attr_json=entity_attrs,
                doc_id=doc_id,
                valid_from=valid_from,
                valid_until=valid_until,
            )

            part_id = insert_part(
                conn=conn,
                doc_id=doc_id,
                part_key=entity_key or f"node:{node_id}",
                part_type="node",
                node_id=node_id,
            )
            if source_text:
                insert_attribute(
                    conn=conn,
                    src_id=part_id,
                    src_type="part",
                    attr_type="source_text",
                    attr_json={"source_text": source_text},
                    doc_id=doc_id,
                    valid_from=valid_from,
                    valid_until=valid_until,
                )
                dense_items.append(
                    {
                        "text": str(source_text),
                        "payload": {
                            **common_payload,
                            "feature_type": "entity_source_text",
                            "node_id": node_id,
                        },
                    }
                )

        sparse_items: list[dict] = []
        if keywords:
            sparse_items.append(
                {
                    "text": "\n".join(keywords),
                    "payload": {
                        **common_payload,
                        "feature_type": "keywords",
                    },
                }
            )

        if identifiers_kv:
            sparse_items.append(
                {
                    "text": flatten_identifiers_for_sparse(identifiers_kv),
                    "payload": {
                        **common_payload,
                        "feature_type": "identifiers",
                    },
                }
            )

        entity_names = [v for v in entity_to_name.values() if v]
        if entity_names:
            sparse_items.append(
                {
                    "text": "\n".join(entity_names),
                    "payload": {
                        **common_payload,
                        "feature_type": "entity_names",
                    },
                }
            )

        for idx, rel in enumerate(relationships):
            rel_type = str(rel.get("relationship_type") or "related_to")
            source_key = str(rel.get("source_entity_id") or "")
            target_key = str(rel.get("target_entity_id") or "")
            rel_attrs = dict(rel.get("attributes") or {})
            source_text = rel.get("source_text")
            confidence = rel.get("confidence")

            src_node_id = entity_to_node_id.get(source_key)
            tar_node_id = entity_to_node_id.get(target_key)
            if src_node_id is None or tar_node_id is None:
                continue

            edge_id = upsert_edge(
                conn=conn,
                edge_name=rel_type,
                edge_cat=rel_type,
                src_node_id=src_node_id,
                tar_node_id=tar_node_id,
                confidence=confidence,
                metadata={"source_entity_id": source_key, "target_entity_id": target_key},
            )

            insert_attribute(
                conn=conn,
                src_id=edge_id,
                src_type="edge",
                attr_type="relationship_attributes",
                attr_json=rel_attrs,
                doc_id=doc_id,
                valid_from=valid_from,
                valid_until=valid_until,
            )

            part_id = insert_part(
                conn=conn,
                doc_id=doc_id,
                part_key=f"rel:{idx}",
                part_type="edge",
                edge_id=edge_id,
            )

            if source_text:
                insert_attribute(
                    conn=conn,
                    src_id=part_id,
                    src_type="part",
                    attr_type="source_text",
                    attr_json={"source_text": source_text},
                    doc_id=doc_id,
                    valid_from=valid_from,
                    valid_until=valid_until,
                )

            dense_rel_text = "\n".join(
                [
                    f"source={entity_to_name.get(source_key, source_key)}",
                    f"relationship={rel_type}",
                    f"target={entity_to_name.get(target_key, target_key)}",
                    f"attributes={json.dumps(rel_attrs, ensure_ascii=False)}",
                    f"source_text={source_text or ''}",
                ]
            )

            dense_items.append(
                {
                    "text": dense_rel_text,
                    "payload": {
                        **common_payload,
                        "feature_type": "relationship",
                        "edge_id": edge_id,
                        "src_node_id": src_node_id,
                        "tar_node_id": tar_node_id,
                    },
                }
            )

        dense_ids = upsert_dense_vectors_batch(dense_items)
        sparse_ids = upsert_sparse_vectors_batch(sparse_items)

        return {
            "doc_id": doc_id,
            "dense_points": len(dense_ids),
            "sparse_points": len(sparse_ids),
            "nodes": len(entity_to_node_id),
            "relationships": len(relationships),
        }
    finally:
        conn.close()


def main() -> None:
    payload = json.loads(input_json)
    result = ingest_document_into_relational_and_vectors(
        payload,
        doc_name=os.path.basename(doc_path),
        doc_path=doc_path,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
