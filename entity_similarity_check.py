import re
import math
from typing import Dict, Any, Tuple, List, Optional
from collections import Counter
from datetime import datetime

# -----------------------------
# 1. Deterministic identifiers
# -----------------------------

DETERMINISTIC_KEYS = {
    "eori_number",
    "vat_number",
    "uid",
    "che",
    "ahv",
    "iban",
    "company_registration_number",
    "passport_number",
    "tax_id",
}


def extract_deterministic_ids(attrs: Dict[str, str]) -> Dict[str, str]:
    ids = {}
    for k, v in attrs.items():
        key_norm = k.strip().lower()
        if key_norm in DETERMINISTIC_KEYS and v:
            ids[key_norm] = v.strip()
    return ids


def deterministic_match(new_attrs: Dict[str, str],
                        existing_attrs: Dict[str, str]) -> float:
    new_ids = extract_deterministic_ids(new_attrs)
    old_ids = extract_deterministic_ids(existing_attrs)

    if not new_ids or not old_ids:
        return 0.0

    for k, v in new_ids.items():
        if k in old_ids and old_ids[k] == v:
            return 1.0

    return 0.0


# -----------------------------
# 2. Normalization helpers
# -----------------------------

LEGAL_FORMS = [
    "gmbh", "ag", "sarl", "sa", "ltd", "llc", "kg", "ohg", "ug", "kgaa"
]


def normalize_name(name: str) -> str:
    if not name:
        return ""
    n = name.lower()
    # remove legal forms
    for lf in LEGAL_FORMS:
        n = re.sub(rf"\b{lf}\b", "", n)
    # remove c/o, parentheses, commas, extra spaces
    n = re.sub(r"c/o", "", n)
    n = re.sub(r"[\(\),]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    return re.sub(r"\D", "", phone)


def normalize_email(email: str) -> str:
    return email.strip().lower() if email else ""


def normalize_date(value: str) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    # try a few common formats; extend as needed
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def tokenize_address(addr: str) -> List[str]:
    if not addr:
        return []
    a = addr.lower()
    a = re.sub(r"[,\.;]", " ", a)
    a = re.sub(r"\s+", " ", a).strip()
    return a.split()


def jaccard_similarity(a: List[str], b: List[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


# -----------------------------
# 3. Normalized attribute similarity
# -----------------------------

def normalized_attribute_similarity(new_attrs: Dict[str, str],
                                    existing_attrs: Dict[str, str]) -> float:
    scores = []
    # name
    new_name = normalize_name(new_attrs.get("name", ""))
    old_name = normalize_name(existing_attrs.get("name", ""))
    if new_name and old_name:
        # simple token overlap
        scores.append(jaccard_similarity(new_name.split(), old_name.split()))

    # phone
    new_phone = normalize_phone(new_attrs.get("phone", ""))
    old_phone = normalize_phone(existing_attrs.get("phone", ""))
    if new_phone and old_phone:
        scores.append(1.0 if new_phone == old_phone else 0.0)

    # email
    new_email = normalize_email(new_attrs.get("email", ""))
    old_email = normalize_email(existing_attrs.get("email", ""))
    if new_email and old_email:
        scores.append(1.0 if new_email == old_email else 0.0)

    # address
    new_addr = new_attrs.get("address", "") or new_attrs.get("adresse", "")
    old_addr = existing_attrs.get("address", "") or existing_attrs.get("adresse", "")
    if new_addr and old_addr:
        scores.append(jaccard_similarity(tokenize_address(new_addr),
                                         tokenize_address(old_addr)))

    # dates (e.g. founding date)
    for key in ("gründungsdatum", "founding_date", "registration_date"):
        n = normalize_date(new_attrs.get(key, ""))
        o = normalize_date(existing_attrs.get(key, ""))
        if n and o:
            scores.append(1.0 if n == o else 0.0)

    if not scores:
        return 0.0
    return sum(scores) / len(scores)


# -----------------------------
# 4. Sparse & dense similarity
# -----------------------------

def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def sparse_cosine_similarity(idx1: List[int], val1: List[float],
                             idx2: List[int], val2: List[float]) -> float:
    if not idx1 or not idx2:
        return 0.0
    v1 = dict(zip(idx1, val1))
    v2 = dict(zip(idx2, val2))
    common = set(v1.keys()) & set(v2.keys())
    if not common:
        return 0.0
    dot = sum(v1[i] * v2[i] for i in common)
    n1 = math.sqrt(sum(x * x for x in v1.values()))
    n2 = math.sqrt(sum(x * x for x in v2.values()))
    if n1 == 0 or n2 == 0:
        return 0.0
    return dot / (n1 * n2)


def attributes_to_text(attrs: Dict[str, str]) -> str:
    parts = []
    for k, v in attrs.items():
        if not v:
            continue
        parts.append(f"{k}: {v}")
    return " | ".join(parts)


def sparse_similarity(new_attrs: Dict[str, str],
                      existing_attrs: Dict[str, str],
                      embed_sparse) -> float:
    t1 = attributes_to_text(new_attrs)
    t2 = attributes_to_text(existing_attrs)
    idx1, val1 = embed_sparse(t1)
    idx2, val2 = embed_sparse(t2)
    return sparse_cosine_similarity(idx1, val1, idx2, val2)


def dense_similarity(new_attrs: Dict[str, str],
                     existing_attrs: Dict[str, str],
                     embed_dense) -> float:
    t1 = attributes_to_text(new_attrs)
    t2 = attributes_to_text(existing_attrs)
    v1 = embed_dense(t1)
    v2 = embed_dense(t2)
    return cosine_similarity(v1, v2)


# -----------------------------
# 5. Final entity similarity
# -----------------------------

def entity_similarity(
    new_entity: Dict[str, Any],
    existing_entity: Dict[str, Any],
    embed_sparse,
    embed_dense,
    weights: Dict[str, float] = None
) -> Dict[str, float]:
    """
    new_entity / existing_entity:
        {
          "name": str,
          "attributes": { attr_name: attr_value, ... }
        }
    """
    if weights is None:
        weights = {
            "deterministic": 0.6,
            "normalized": 0.2,
            "sparse": 0.15,
            "dense": 0.05,
        }

    new_attrs = dict(new_entity.get("attributes", {}))
    existing_attrs = dict(existing_entity.get("attributes", {}))

    # also inject name/email/phone/address into attrs if stored separately
    for key in ("name", "email", "phone", "address", "adresse"):
        if key in new_entity and key not in new_attrs:
            new_attrs[key] = new_entity[key]
        if key in existing_entity and key not in existing_attrs:
            existing_attrs[key] = existing_entity[key]

    det = deterministic_match(new_attrs, existing_attrs)
    norm = normalized_attribute_similarity(new_attrs, existing_attrs)
    sp = sparse_similarity(new_attrs, existing_attrs, embed_sparse)
    dn = dense_similarity(new_attrs, existing_attrs, embed_dense)

    final_score = (
        weights["deterministic"] * det +
        weights["normalized"] * norm +
        weights["sparse"] * sp +
        weights["dense"] * dn
    )

    return {
        "deterministic": det,
        "normalized": norm,
        "sparse": sp,
        "dense": dn,
        "final": final_score,
    }


def is_same_entity(
    new_entity: Dict[str, Any],
    existing_entity: Dict[str, Any],
    embed_sparse,
    embed_dense,
    threshold: float = 0.8,
    weights: Dict[str, float] = None
) -> Tuple[bool, Dict[str, float]]:
    scores = entity_similarity(new_entity, existing_entity,
                               embed_sparse, embed_dense, weights)
    return scores["final"] >= threshold, scores

# -----------------------------
# Overlap-only attribute similarity
# -----------------------------

def overlap_only_similarity(
    new_attrs: Dict[str, str],
    existing_attrs: Dict[str, str]
) -> float:
    """
    Compare ONLY attributes that exist in BOTH entities.
    Prevents penalizing missing attributes.
    """

    overlap_keys = set(new_attrs.keys()) & set(existing_attrs.keys())
    if not overlap_keys:
        return 0.0

    scores = []

    for key in overlap_keys:
        n_val = new_attrs.get(key)
        e_val = existing_attrs.get(key)

        if not n_val or not e_val:
            continue

        key_l = key.lower()

        # --- Name ---
        if key_l in ("name", "full_name"):
            n1 = normalize_name(n_val)
            n2 = normalize_name(e_val)
            scores.append(jaccard_similarity(n1.split(), n2.split()))
            continue

        # --- Email ---
        if "email" in key_l:
            scores.append(1.0 if normalize_email(n_val) == normalize_email(e_val) else 0.0)
            continue

        # --- Phone ---
        if "phone" in key_l or "tel" in key_l:
            scores.append(1.0 if normalize_phone(n_val) == normalize_phone(e_val) else 0.0)
            continue

        # --- Address ---
        if "address" in key_l or "adresse" in key_l:
            scores.append(jaccard_similarity(tokenize_address(n_val), tokenize_address(e_val)))
            continue

        # --- Dates ---
        if "date" in key_l or "datum" in key_l:
            d1 = normalize_date(n_val)
            d2 = normalize_date(e_val)
            scores.append(1.0 if d1 and d2 and d1 == d2 else 0.0)
            continue

        # --- Default: token Jaccard ---
        scores.append(jaccard_similarity(n_val.lower().split(), e_val.lower().split()))

    if not scores:
        return 0.0

    return sum(scores) / len(scores)


# -----------------------------
# Wrapper for entity comparison
# -----------------------------

def compare_entities_overlap_only(
    new_entity: Dict[str, Any],
    existing_entity: Dict[str, Any]
) -> float:
    """
    Compare two entities using ONLY overlapping attributes.
    """

    new_attrs = dict(new_entity.get("attributes", {}))
    existing_attrs = dict(existing_entity.get("attributes", {}))

    # Inject name/email/phone/address if stored at top level
    for key in ("name", "email", "phone", "address", "adresse", "city"):
        if key in new_entity and key not in new_attrs:
            new_attrs[key] = new_entity[key]
        if key in existing_entity and key not in existing_attrs:
            existing_attrs[key] = existing_entity[key]

    return overlap_only_similarity(new_attrs, existing_attrs)
