import hashlib
import math
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar
from urllib.parse import urljoin, urlparse

import requests
from dateutil.parser import parse

from onyx.configs.app_configs import CONNECTOR_LOCALHOST_OVERRIDE
from onyx.configs.constants import IGNORE_FOR_QA, DocumentSource
from onyx.connectors.models import BasicExpertInfo, OnyxMetadata
from onyx.utils.datetime import datetime_to_utc
from onyx.utils.logger import setup_logger
from onyx.utils.text_processing import is_valid_email

T = TypeVar("T")
U = TypeVar("U")
logger = setup_logger()


def time_str_to_utc(datetime_str: str) -> datetime:
    # Remove all timezone abbreviations in parentheses
    normalized = re.sub(r"\([A-Z]+\)", "", datetime_str).strip()

    # Remove any remaining parentheses and their contents
    normalized = re.sub(r"\(.*?\)", "", normalized).strip()

    candidates: list[str] = [normalized]

    # Some sources (e.g. Gmail) may prefix the value with labels like "Date:"
    label_stripped = re.sub(
        r"^\s*[A-Za-z][A-Za-z\s_-]*:\s*", "", normalized, count=1
    ).strip()
    if label_stripped and label_stripped != normalized:
        candidates.append(label_stripped)

    # Fix common format issues (e.g. "0000" => "+0000")
    for candidate in list(candidates):
        if " 0000" in candidate:
            fixed = candidate.replace(" 0000", " +0000")
            if fixed not in candidates:
                candidates.append(fixed)

    # dateutil is the primary; the stdlib RFC 2822 parser is a fallback for
    # inputs dateutil rejects (e.g. headers concatenated without a CRLF —
    # TZ may be dropped, datetime_to_utc then assumes UTC).
    for parser in (parse, parsedate_to_datetime):
        for candidate in candidates:
            try:
                return datetime_to_utc(parser(candidate))
            except (TypeError, ValueError, OverflowError):
                continue

    raise ValueError(f"Unable to parse datetime string: {datetime_str}")


# TODO: use this function in other connectors
def datetime_from_utc_timestamp(timestamp: int) -> datetime:
    """Convert a Unix timestamp to a datetime object in UTC"""

    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def basic_expert_info_representation(info: BasicExpertInfo) -> str | None:
    if info.first_name and info.last_name:
        return f"{info.first_name} {info.middle_initial} {info.last_name}"

    if info.display_name:
        return info.display_name

    if info.email and is_valid_email(info.email):
        return info.email

    if info.first_name:
        return info.first_name

    return None


def get_experts_stores_representations(
    experts: list[BasicExpertInfo] | None,
) -> list[str] | None:
    """Gets string representations of experts supplied.

    If an expert cannot be represented as a string, it is omitted from the
    result.
    """
    if not experts:
        return None

    reps: list[str | None] = [
        basic_expert_info_representation(owner) for owner in experts
    ]
    return [owner for owner in reps if owner is not None]


def process_in_batches(
    objects: list[T], process_function: Callable[[T], U], batch_size: int
) -> Iterator[list[U]]:
    for i in range(0, len(objects), batch_size):
        yield [process_function(obj) for obj in objects[i : i + batch_size]]


def get_metadata_keys_to_ignore() -> list[str]:
    return [IGNORE_FOR_QA]


def _parse_document_source(connector_type: Any) -> DocumentSource | None:
    if connector_type is None:
        return None

    if isinstance(connector_type, DocumentSource):
        return connector_type

    if not isinstance(connector_type, str):
        logger.warning("Invalid connector_type type: %s", type(connector_type).__name__)
        return None

    normalized = re.sub(r"[\s\-]+", "_", connector_type.strip().lower())
    try:
        return DocumentSource(normalized)
    except ValueError:
        logger.warning(
            "Invalid connector_type value: '%s' (normalized: '%s')",
            connector_type,
            normalized,
        )
        return None


def process_onyx_metadata(
    metadata: dict[str, Any],
) -> tuple[OnyxMetadata, dict[str, Any]]:
    """
    Users may set Onyx metadata and custom tags in text files. https://docs.onyx.app/admins/connectors/official/file
    Any unrecognized fields are treated as custom tags.
    """
    p_owner_names = metadata.get("primary_owners")
    p_owners = (
        [BasicExpertInfo(display_name=name) for name in p_owner_names]
        if p_owner_names
        else None
    )

    s_owner_names = metadata.get("secondary_owners")
    s_owners = (
        [BasicExpertInfo(display_name=name) for name in s_owner_names]
        if s_owner_names
        else None
    )
    source_type = _parse_document_source(metadata.get("connector_type"))

    dt_str = metadata.get("doc_updated_at")
    doc_updated_at = time_str_to_utc(dt_str) if dt_str else None

    return (
        OnyxMetadata(
            document_id=metadata.get("id"),
            source_type=source_type,
            link=metadata.get("link"),
            file_display_name=metadata.get("file_display_name"),
            title=metadata.get("title"),
            primary_owners=p_owners,
            secondary_owners=s_owners,
            doc_updated_at=doc_updated_at,
        ),
        {
            k: v
            for k, v in metadata.items()
            if k
            not in [
                "document_id",
                "time_updated",
                "doc_updated_at",
                "link",
                "primary_owners",
                "secondary_owners",
                "filename",
                "file_display_name",
                "title",
                "connector_type",
                "pdf_password",
                "mime_type",
            ]
        },
    )


def get_oauth_callback_uri(base_domain: str, connector_id: str) -> str:
    if CONNECTOR_LOCALHOST_OVERRIDE:
        # Used for development
        base_domain = CONNECTOR_LOCALHOST_OVERRIDE
    return f"{base_domain.strip('/')}/connector/oauth/callback/{connector_id}"


def is_atlassian_date_error(e: Exception) -> bool:
    return "field 'updated' is invalid" in str(e)


def get_cloudId(base_url: str) -> str:
    tenant_info_url = urljoin(base_url, "/_edge/tenant_info")
    response = requests.get(tenant_info_url, timeout=10)
    response.raise_for_status()
    return response.json()["cloudId"]


def scoped_url(url: str, product: str) -> str:
    parsed = urlparse(url)
    base_url = parsed.scheme + "://" + parsed.netloc
    cloud_id = get_cloudId(base_url)
    return f"https://api.atlassian.com/ex/{product}/{cloud_id}{parsed.path}"


def compute_document_content_metrics(
    text_content: str | None,
    words_per_minute: int = 200,
) -> dict[str, str]:
    """
    Computes comprehensive structural and content analytics for extracted document text.
    These metrics enrich document metadata for search relevance, deduplication,
    and user-facing reading time estimation.

    Args:
        text_content: The raw text string extracted from the document.
        words_per_minute: Estimated reading speed for calculating reading time.

    Returns:
        Dictionary mapping metric names to string-serialized values, suitable for
        direct injection into Onyx Document metadata / custom_tags.
    """
    if not text_content or not text_content.strip():
        return {}

    cleaned_text = text_content.strip()
    words = cleaned_text.split()
    word_count = len(words)
    if word_count == 0:
        return {}

    char_count = len(cleaned_text)
    char_count_no_spaces = sum(len(w) for w in words)

    # Sentence boundary detection using common terminal punctuation
    sentences = [s.strip() for s in re.split(r"[.!?]+", cleaned_text) if s.strip()]
    sentence_count = max(1, len(sentences))

    # Paragraph count based on non-empty line segments
    paragraphs = [p.strip() for p in cleaned_text.splitlines() if p.strip()]
    paragraph_count = max(1, len(paragraphs))

    # Reading time calculation (standard ~200 WPM, rounded up to nearest minute)
    reading_time_mins = max(1, math.ceil(word_count / words_per_minute))

    # Deterministic SHA-256 content hash for duplicate detection and document provenance
    content_hash = hashlib.sha256(cleaned_text.encode("utf-8")).hexdigest()

    # Content complexity indicators
    unique_words = {w.lower() for w in words}
    lexical_diversity = f"{len(unique_words) / word_count:.2f}"
    avg_word_length = f"{char_count_no_spaces / word_count:.1f}"

    return {
        "word_count": str(word_count),
        "char_count": str(char_count),
        "char_count_no_spaces": str(char_count_no_spaces),
        "sentence_count": str(sentence_count),
        "paragraph_count": str(paragraph_count),
        "reading_time_mins": str(reading_time_mins),
        "content_hash": content_hash,
        "lexical_diversity": lexical_diversity,
        "avg_word_length": avg_word_length,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
    }


def check_content_hash_exists(
    content_hash: str | None,
    db_session: Any | None = None,
    existing_hashes: set[str] | None = None,
) -> bool:
    """
    Checks whether a document with the given content_hash already exists.

    1. Checks fast in-memory / batch existing_hashes set if provided.
    2. Checks database storage via db_session or current tenant session if available.

    Returns True if content is duplicate, False otherwise.
    """
    if not content_hash:
        return False

    if existing_hashes is not None and content_hash in existing_hashes:
        return True

    try:
        if db_session is not None:
            from onyx.db.document import (
                check_content_hash_exists as db_check_content_hash,
            )

            return db_check_content_hash(content_hash, db_session)

        # Attempt resolving through tenant session context if database is configured
        from onyx.db.engine.sql_engine import get_session_with_current_tenant

        with get_session_with_current_tenant() as session:
            from onyx.db.document import (
                check_content_hash_exists as db_check_content_hash,
            )

            return db_check_content_hash(content_hash, session)
    except Exception as e:
        import logging

        logging.getLogger(__name__).debug(
            "Database content_hash check skipped or unavailable: %s", e
        )
        return False

