import datetime

import hashlib

import io
from unittest.mock import MagicMock

import pytest

from onyx.connectors.cross_connector_utils.miscellaneous_utils import (
    check_content_hash_exists,
    compute_document_content_metrics,
    time_str_to_utc,
)
from onyx.connectors.file.connector import _process_file


def test_time_str_to_utc() -> None:
    str_to_dt = {
        "Tue, 5 Oct 2021 09:38:25 GMT": datetime.datetime(
            2021, 10, 5, 9, 38, 25, tzinfo=datetime.timezone.utc
        ),
        "Sat, 24 Jul 2021 09:21:20 +0000 (UTC)": datetime.datetime(
            2021, 7, 24, 9, 21, 20, tzinfo=datetime.timezone.utc
        ),
        "Thu, 29 Jul 2021 04:20:37 -0400 (EDT)": datetime.datetime(
            2021, 7, 29, 8, 20, 37, tzinfo=datetime.timezone.utc
        ),
        "30 Jun 2023 18:45:01 +0300": datetime.datetime(
            2023, 6, 30, 15, 45, 1, tzinfo=datetime.timezone.utc
        ),
        "22 Mar 2020 20:12:18 +0000 (GMT)": datetime.datetime(
            2020, 3, 22, 20, 12, 18, tzinfo=datetime.timezone.utc
        ),
        "Date: Wed, 27 Aug 2025 11:40:00 +0200": datetime.datetime(
            2025, 8, 27, 9, 40, 0, tzinfo=datetime.timezone.utc
        ),
    }
    for strptime, expected_datetime in str_to_dt.items():
        assert time_str_to_utc(strptime) == expected_datetime


def test_time_str_to_utc_recovers_from_concatenated_headers() -> None:
    # TZ is dropped during recovery, so the expected result is UTC rather
    # than the original offset.
    assert time_str_to_utc(
        'Sat, 3 Nov 2007 14:33:28 -0200To: "jason" <jason@example.net>'
    ) == datetime.datetime(2007, 11, 3, 14, 33, 28, tzinfo=datetime.timezone.utc)

    assert time_str_to_utc(
        "Fri, 20 Feb 2015 10:30:00 +0500Cc: someone@example.com"
    ) == datetime.datetime(2015, 2, 20, 10, 30, 0, tzinfo=datetime.timezone.utc)


def test_time_str_to_utc_raises_on_impossible_dates() -> None:
    for bad in (
        "Wed, 33 Sep 2007 13:42:59 +0100",
        "Thu, 11 Oct 2007 31:50:55 +0900",
        "not a date at all",
        "",
    ):
        with pytest.raises(ValueError):
            time_str_to_utc(bad)


def test_compute_document_content_metrics_empty() -> None:
    assert compute_document_content_metrics("") == {}
    assert compute_document_content_metrics("   \n\t  ") == {}
    assert compute_document_content_metrics(None) == {}


def test_compute_document_content_metrics_standard() -> None:
    sample_text = (
        "Onyx is an enterprise Gen-AI search platform.\n\n"
        "It connects to documents, databases, and apps seamlessly. "
        "Employees can find accurate information in seconds!"
    )
    metrics = compute_document_content_metrics(sample_text)

    assert "word_count" in metrics
    assert int(metrics["word_count"]) > 0
    assert "char_count" in metrics
    assert int(metrics["char_count"]) == len(sample_text.strip())
    assert "reading_time_mins" in metrics
    assert metrics["reading_time_mins"] == "1"
    assert "paragraph_count" in metrics
    assert metrics["paragraph_count"] == "2"
    assert "sentence_count" in metrics
    assert int(metrics["sentence_count"]) >= 3
    assert "content_hash" in metrics
    expected_hash = hashlib.sha256(sample_text.strip().encode("utf-8")).hexdigest()
    assert metrics["content_hash"] == expected_hash
    assert "lexical_diversity" in metrics
    assert "avg_word_length" in metrics
    assert "enriched_at" in metrics


def test_compute_document_content_metrics_deterministic_hash() -> None:
    text = "Deterministic hashing verification for Onyx file connector."
    m1 = compute_document_content_metrics(text)
    m2 = compute_document_content_metrics(text)
    assert m1["content_hash"] == m2["content_hash"]
    assert m1["word_count"] == m2["word_count"]
    assert m1["char_count"] == m2["char_count"]


def test_compute_document_content_metrics_multilingual() -> None:
    urdu_arabic_text = "اونکس ایک بہترین سرچ انجن ہے۔ یہ معلومات کو فوری تلاش کرتا ہے۔"
    metrics = compute_document_content_metrics(urdu_arabic_text)
    assert int(metrics["word_count"]) > 0
    assert int(metrics["char_count"]) == len(urdu_arabic_text)
    assert len(metrics["content_hash"]) == 64


def test_check_content_hash_exists_empty_inputs() -> None:
    assert not check_content_hash_exists(None)
    assert not check_content_hash_exists("")


def test_check_content_hash_exists_in_memory() -> None:
    seen_hashes: set[str] = set()
    sample_text = "Unique technical content for duplicate detection test."
    metrics = compute_document_content_metrics(sample_text)
    content_hash = metrics["content_hash"]

    # First check: hash does not exist (non-duplicate)
    assert not check_content_hash_exists(content_hash, existing_hashes=seen_hashes)

    # Register hash in cache
    seen_hashes.add(content_hash)

    # Second check: hash now exists (duplicate)
    assert check_content_hash_exists(content_hash, existing_hashes=seen_hashes)

    # Different text: non-duplicate
    different_metrics = compute_document_content_metrics("Different content completely.")
    assert not check_content_hash_exists(
        different_metrics["content_hash"], existing_hashes=seen_hashes
    )


def test_check_content_hash_exists_with_mock_db() -> None:
    mock_session = MagicMock()

    # When DB query scalar() returns True (duplicate exists in DB)
    mock_session.execute.return_value.scalar.return_value = True
    assert check_content_hash_exists("hash123", db_session=mock_session)

    # When DB query scalar() returns False (new content)
    mock_session.execute.return_value.scalar.return_value = False
    assert not check_content_hash_exists("hash456", db_session=mock_session)


def test_process_file_duplicate_content_skipping() -> None:
    seen_hashes: set[str] = set()
    text_content = b"Enterprise policy document body for testing duplicate skipping."

    # 1. First ingestion (new content -> non-duplicate)
    file_io_1 = io.BytesIO(text_content)
    docs_1 = _process_file(
        file_id="doc_1",
        file_name="policy_v1.txt",
        file=file_io_1,
        metadata={},
        pdf_pass=None,
        file_type="text/plain",
        stage=None,
        existing_hashes=seen_hashes,
    )
    assert len(docs_1) == 1
    assert docs_1[0].metadata.get("word_count") is not None
    assert docs_1[0].metadata.get("content_hash") in seen_hashes

    # 2. Second ingestion with identical content (duplicate -> skipped)
    file_io_2 = io.BytesIO(text_content)
    docs_2 = _process_file(
        file_id="doc_2",
        file_name="policy_v1_copy.txt",
        file=file_io_2,
        metadata={},
        pdf_pass=None,
        file_type="text/plain",
        stage=None,
        existing_hashes=seen_hashes,
    )
    assert len(docs_2) == 0  # Safely skipped duplicate processing

    # 3. Third ingestion with new content (non-duplicate -> processed)
    new_text_content = b"Completely different guidelines for non-duplicate test."
    file_io_3 = io.BytesIO(new_text_content)
    docs_3 = _process_file(
        file_id="doc_3",
        file_name="new_policy.txt",
        file=file_io_3,
        metadata={},
        pdf_pass=None,
        file_type="text/plain",
        stage=None,
        existing_hashes=seen_hashes,
    )
    assert len(docs_3) == 1
    assert docs_3[0].id == "FILE_CONNECTOR__doc_3"

