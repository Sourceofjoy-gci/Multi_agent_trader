"""The validation report is a second document kind in the one evidence store (P-9, P-10).

Same root, same layout, same atomic link-publish, same domain-separated digest as a bundle;
and neither kind is readable as the other. Every case below breaks one clause and names it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.unit.research.test_evidence import _bundle
from tests.unit.research.test_promotion import _report
from trading_house.core.errors import EvidenceIntegrityError
from trading_house.research.canonical import DOMAIN_SEPARATOR, canonical_bytes, canonical_sha256
from trading_house.research.evidence import EvidenceStore


def _place(store: EvidenceStore, data: bytes) -> str:
    """Write ``data`` at the digest the store's own formula gives it, bypassing ``write``."""

    digest = hashlib.sha256(DOMAIN_SEPARATOR + data).hexdigest()
    path = store.root / digest[:2] / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return digest


def test_a_report_round_trips_through_the_same_root_and_layout_as_a_bundle(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    report = _report()

    stored = store.write_report(report)
    bundle = store.write(_bundle())

    assert stored.sha256 == report.digest()
    assert stored.sha256 == hashlib.sha256(DOMAIN_SEPARATOR + canonical_bytes(report)).hexdigest()
    assert stored.path == f"{stored.sha256[:2]}/{stored.sha256}.json"
    assert bundle.path == f"{bundle.sha256[:2]}/{bundle.sha256}.json"
    assert stored.size_bytes == len(canonical_bytes(report))
    assert store.read_report(stored.sha256) == report
    assert store.read(bundle.sha256) == _bundle()
    assert (tmp_path / stored.path).read_bytes() == canonical_bytes(report)


def test_writing_the_same_report_twice_is_one_file(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)

    first, second = store.write_report(_report()), store.write_report(_report())

    assert first == second
    assert len(list(tmp_path.rglob("*.json"))) == 1
    assert list(tmp_path.rglob(".evidence-*.tmp")) == []


def test_different_bytes_already_at_the_reports_digest_are_a_conflict(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    digest = canonical_sha256(_report())
    path = tmp_path / digest[:2] / f"{digest}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"something else")

    with pytest.raises(EvidenceIntegrityError):
        store.write_report(_report())
    assert path.read_bytes() == b"something else"


def test_read_refuses_a_reports_digest_and_read_report_refuses_a_bundles(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    report_digest = store.write_report(_report()).sha256
    bundle_digest = store.write(_bundle()).sha256

    with pytest.raises(EvidenceIntegrityError):
        store.read(report_digest)
    with pytest.raises(EvidenceIntegrityError):
        store.read_report(bundle_digest)
    with pytest.raises(EvidenceIntegrityError):
        store.verify(report_digest)


def test_a_report_with_one_flipped_byte_is_refused(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    stored = store.write_report(_report())
    path = tmp_path / stored.path
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))

    with pytest.raises(EvidenceIntegrityError):
        store.read_report(stored.sha256)


def test_a_missing_report_is_an_integrity_error_not_an_oserror(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    stored = store.write_report(_report())
    (tmp_path / stored.path).unlink()

    with pytest.raises(EvidenceIntegrityError):
        store.read_report(stored.sha256)


def test_a_report_digest_that_is_not_a_digest_cannot_walk_out_of_the_root(tmp_path: Path) -> None:
    with pytest.raises(EvidenceIntegrityError):
        EvidenceStore(tmp_path).read_report("../../etc/passwd")


def test_a_document_that_hashes_to_its_name_but_is_not_a_report_is_refused(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)

    with pytest.raises(EvidenceIntegrityError):
        store.read_report(_place(store, b'{"a":1}'))


def test_a_report_in_a_non_canonical_encoding_is_refused_even_at_its_own_digest(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)
    spaced = _report().model_dump_json(indent=2).encode("utf-8")

    with pytest.raises(EvidenceIntegrityError):
        store.read_report(_place(store, spaced))


def test_a_report_filed_under_another_digest_is_refused(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    stored = store.write_report(_report())
    other = "9" * 64
    target = tmp_path / other[:2] / f"{other}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((tmp_path / stored.path).read_bytes())

    with pytest.raises(EvidenceIntegrityError):
        store.read_report(other)
