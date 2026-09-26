import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_house.core.errors import EvidenceIntegrityError
from trading_house.core.exits import NoExitPolicy
from trading_house.core.schemas import Side
from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.result import BacktestResult, SimulatedTrade
from trading_house.research.canonical import DOMAIN_SEPARATOR, canonical_bytes, canonical_sha256
from trading_house.research.evidence import (
    CostSummary,
    DailyReturnPoint,
    EvidenceBundle,
    EvidenceProvenance,
    EvidenceStore,
)
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    HoldoutState,
    RegistrationState,
    ReturnSeriesBasis,
)

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def _result() -> BacktestResult:
    trade = SimulatedTrade(
        proposal_id="p-1",
        side=Side.BUY,
        lots=Decimal("0.10"),
        entry_price=Decimal("1.10000"),
        entry_at=NOW,
        exit_price=Decimal("1.10100"),
        exit_at=NOW + timedelta(minutes=12),
        exit_kind=ExitKind.TIME,
        gross_pnl=Decimal("100"),
        commission=Decimal("7"),
        swap=Decimal("-3"),
        net_pnl=Decimal("90"),
    )
    return BacktestResult(
        run_id="run-1",
        strategy_id="strat-1",
        strategy_version="v1",
        exit_policy=NoExitPolicy(kind="none"),
        constitution_sha256="a" * 64,
        contract_sha256="b" * 64,
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        start=NOW,
        end=NOW + timedelta(hours=1),
        firm_equity=Decimal("100000"),
        cost_model=CostModel(
            commission_per_lot_per_side=Decimal("3.50"),
            slippage_points_per_side=Decimal("1"),
            swap_long_points_per_day=Decimal("-1"),
            swap_short_points_per_day=Decimal("-1"),
            triple_swap_weekday=2,
        ),
        atr_period=14,
        spread_window=20,
        defective_bar_tolerance=Fraction(0),
        trades=(trade,),
        rejections=(),
        bars_seen=60,
        snapshots_skipped=0,
        net_pnl=Decimal("90"),
    )


def _bundle(*, day_offset: int = 0) -> EvidenceBundle:
    result = _result()
    return EvidenceBundle(
        result_schema_version=1,
        trial_id="trial-1",
        attempt_id="attempt-1",
        spec_sha256="d" * 64,
        source_result_sha256=result.digest(),
        result=result,
        daily_returns=(
            DailyReturnPoint(day=NOW.date() + timedelta(days=day_offset), value=Decimal("0")),
        ),
        return_series_basis=ReturnSeriesBasis.REALIZED_CLOSED_TRADES,
        costs=CostSummary(
            status=CostAttributionStatus.PARTIAL,
            commission=Decimal("7"),
            swap=Decimal("-3"),
            spread_cost=None,
            slippage_cost=None,
        ),
        provenance=EvidenceProvenance(
            agent_run_id="run-1",
            source_artifact_sha256="e" * 64,
            dataset_sha256="f" * 64,
            registered_at=NOW,
            occurred_at=NOW,
            registration_state=RegistrationState.PROSPECTIVE,
            holdout_state=HoldoutState.NOT_DEFINED,
        ),
    )


def _with(bundle: EvidenceBundle, **overrides: object) -> EvidenceBundle:
    """Rebuild a bundle with one field changed, revalidating it.

    ``model_dump`` cannot be used here: every research contract is
    ``strict=True``, and a dump hands nested models back as dicts, which a
    strict field will not accept. ``model_copy`` would validate nothing, which
    is the opposite of what these tests need.
    """

    payload = {name: getattr(bundle, name) for name in EvidenceBundle.model_fields}
    return EvidenceBundle(**{**payload, **overrides})


def test_bundle_digest_is_the_sha256_of_canonical_bytes(tmp_path: Path) -> None:
    bundle = _bundle()
    stored = EvidenceStore(tmp_path).write(bundle)

    assert stored.sha256 == canonical_sha256(bundle)
    assert stored.path == f"{stored.sha256[:2]}/{stored.sha256}.json"


def test_store_never_overwrites_different_bytes_at_an_existing_digest(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    first = store.write(_bundle())
    target = store.root / first.path
    target.write_bytes(b'{"tampered":true}\n')

    with pytest.raises(EvidenceIntegrityError):
        store.write(_bundle())


def test_store_read_rejects_a_file_whose_digest_does_not_match(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    stored = store.write(_bundle())
    (store.root / stored.path).write_bytes(b'{"changed":true}')

    with pytest.raises(EvidenceIntegrityError):
        store.read(stored.sha256)


def test_atomic_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    store.write(_bundle())

    assert not list(store.root.rglob("*.tmp"))


def test_a_legacy_partial_cost_summary_leaves_spread_and_slippage_unattributed() -> None:
    """A Phase 7 artifact charges spread and slippage inside the fill prices,
    so it cannot split them out. PARTIAL with two ``None`` components is the
    honest statement of that; inventing zeros would be a lie a promotion gate
    could not detect."""

    costs = _bundle().costs

    assert costs.status is CostAttributionStatus.PARTIAL
    assert costs.spread_cost is None
    assert costs.slippage_cost is None


def test_source_result_digest_is_the_backtest_result_digest() -> None:
    """The ledger records a Phase 7 artifact by its own digest, so the bundle
    has to carry that same value -- not this chain's."""

    bundle = _bundle()

    assert bundle.source_result_sha256 == bundle.result.digest()
    assert bundle.source_result_sha256 != canonical_sha256(bundle.result)


def test_a_complete_cost_summary_may_not_omit_a_component() -> None:
    """COMPLETE means every modelled term is attributed. Accepting a None here
    would let a bundle claim full attribution while carrying no spread or
    slippage figure at all."""

    attributed = {
        "status": CostAttributionStatus.COMPLETE,
        "commission": Decimal("7"),
        "swap": Decimal("-3"),
        "spread_cost": Decimal("2"),
        "slippage_cost": Decimal("1"),
    }
    # The control: with both components present, COMPLETE is accepted. Without
    # it, the rejections below would not prove the validator is what fired.
    assert CostSummary(**attributed).status is CostAttributionStatus.COMPLETE

    for omitted in ("spread_cost", "slippage_cost"):
        with pytest.raises(ValidationError, match="attributed"):
            CostSummary(**{**attributed, omitted: None})


def test_a_bundle_may_not_misreport_the_digest_of_its_result() -> None:
    with pytest.raises(ValidationError, match="source_result_sha256"):
        _with(_bundle(), source_result_sha256="f" * 64)


def test_provenance_timestamps_must_be_utc() -> None:
    """``registered_at`` is operator-declared, not authoritative -- but it is
    still a cross-timezone comparison waiting to happen if it can be naive."""

    bundle = _bundle()
    payload = bundle.provenance.model_dump()

    for field in ("registered_at", "occurred_at"):
        with pytest.raises(ValidationError):
            EvidenceProvenance(**{**payload, field: payload[field].replace(tzinfo=None)})


def test_writing_the_same_bundle_twice_is_idempotent(tmp_path: Path) -> None:
    """Re-sealing identical evidence is a normal retry, not a conflict."""

    store = EvidenceStore(tmp_path)

    assert store.write(_bundle()) == store.write(_bundle())


def test_verify_rejects_a_digest_the_store_never_wrote(tmp_path: Path) -> None:
    with pytest.raises(EvidenceIntegrityError):
        EvidenceStore(tmp_path).verify("a" * 64)


def test_read_rejects_bytes_that_are_not_a_bundle_at_all(tmp_path: Path) -> None:
    """A file can sit at a digest-shaped path and still be nothing. Only
    parsing knows that, and it has to fail closed rather than hand back a
    default."""

    store = EvidenceStore(tmp_path)
    data = b"not a bundle at all"
    digest = hashlib.sha256(DOMAIN_SEPARATOR + data).hexdigest()
    target = tmp_path / digest[:2] / f"{digest}.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(data)

    with pytest.raises(EvidenceIntegrityError):
        store.read(digest)


def test_read_rejects_content_that_digests_correctly_but_is_not_canonical(tmp_path: Path) -> None:
    """The digest alone cannot catch a re-serialized document. Re-encoding the
    parsed bundle must reproduce the exact stored bytes, or the file is not
    the evidence its digest names."""

    store = EvidenceStore(tmp_path)
    data = canonical_bytes(_bundle())
    noncanonical = json.dumps(json.loads(data), indent=1).encode()
    digest = hashlib.sha256(DOMAIN_SEPARATOR + noncanonical).hexdigest()
    target = tmp_path / digest[:2] / f"{digest}.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(noncanonical)

    with pytest.raises(EvidenceIntegrityError):
        store.read(digest)


@pytest.mark.parametrize(
    "digest",
    ["..", "../../escaped/escaped", "", "NOTHEX" * 8, "a" * 63, "A" * 64, "a" * 65],
)
def test_the_store_never_follows_a_digest_out_of_its_own_root(tmp_path: Path, digest: str) -> None:
    """The ledger, not the caller, owns paths. A digest that is not 64
    lower-case hex characters is not a digest, and must never become a
    directory name or a path outside the evidence root."""

    store = EvidenceStore(tmp_path)

    with pytest.raises(EvidenceIntegrityError):
        store.read(digest)

    with pytest.raises(EvidenceIntegrityError):
        store.verify(digest)


def test_write_wraps_an_unusable_root_in_an_integrity_error(tmp_path: Path) -> None:
    """An OSError must not escape as a bare OSError: a caller that only knows
    the evidence contract has no way to read one, and the path it carries is
    the one thing an operator must not see echoed out of a failed seal."""

    root = tmp_path / "root"
    root.write_bytes(b"not a directory")

    with pytest.raises(EvidenceIntegrityError):
        EvidenceStore(root).write(_bundle())


def test_the_stored_file_is_exactly_the_canonical_bytes(tmp_path: Path) -> None:
    bundle = _bundle()
    store = EvidenceStore(tmp_path)
    stored = store.write(bundle)
    data = (store.root / stored.path).read_bytes()

    assert data == canonical_bytes(bundle)
    assert stored.size_bytes == len(data)
    assert store.read(stored.sha256) == bundle
    store.verify(stored.sha256)
