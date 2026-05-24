"""Contract tests for the three width-narrowing strategies.

Covered ``analyze`` paths:

* ``PCWidthNarrowing``    happy path on a real ELF + the
                          empty-profile fallback (returns
                          ``pc_width=0``, the disabled sentinel).
* ``HWLPAddrNarrowing``   same shape; absent profile -> 32.
* ``CounterWidthNarrowing`` profile.loops driven; absent -> 16.

Also covers ``applicable()`` for each strategy: it checks for the
existence of a target parameter and returns ``False`` on a stub
target that lacks it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arvis.core.strategy import WidthDecision
from arvis.core.workload import WorkloadProfile
from arvis.strategies.width.counter_width import CounterWidthNarrowing
from arvis.strategies.width.hwlp_addr import HWLPAddrNarrowing
from arvis.strategies.width.pc_width import PCWidthNarrowing
from arvis.targets import CV32E40P

# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def target() -> CV32E40P:
    return CV32E40P()


@pytest.fixture
def empty_profile() -> WorkloadProfile:
    """Profile with no ELFs -- exercises the fallback paths."""
    return WorkloadProfile()


@pytest.fixture
def real_elf_profile() -> WorkloadProfile:
    """Profile pointing at the first benchmark with .elf files.

    Skips the test when no benchmarks are present (e.g. on
    arvis-public, which ships only the ``minimal`` benchmark).
    """
    bench_root = Path(__file__).resolve().parent.parent.parent / "targets" / "benchmarks"
    if not bench_root.exists():
        pytest.skip(f"No benchmarks at {bench_root}")
    elf = None
    for d in sorted(bench_root.iterdir()):
        if d.is_dir():
            elfs = sorted(d.glob("*.elf"))
            if elfs:
                elf = elfs[0]
                break
    if elf is None:
        pytest.skip("No .elf files available")
    return WorkloadProfile(elf_paths=(elf,))


class _StubTarget:
    """Target that exposes no parameters -- triggers ``applicable=False``."""

    def parameter(self, name: str) -> None:
        return None


# ─── PCWidthNarrowing ─────────────────────────────────────────────


def test_pc_width_applicable_to_cv32e40p(target: CV32E40P) -> None:
    assert PCWidthNarrowing().applicable(workload=None, target=target) is True  # type: ignore[arg-type]


def test_pc_width_not_applicable_to_stub() -> None:
    assert PCWidthNarrowing().applicable(workload=None, target=_StubTarget()) is False  # type: ignore[arg-type]


def test_pc_width_empty_profile_returns_disabled(
    target: CV32E40P, empty_profile: WorkloadProfile
) -> None:
    """No ELFs -> ``pc_width=0`` (disabled sentinel)."""
    d = PCWidthNarrowing().analyze(
        workload=None,  # type: ignore[arg-type]
        profile=empty_profile,
        target=target,
    )
    assert d.pc_width == 0


def test_pc_width_real_elf_yields_in_range_value(
    target: CV32E40P, real_elf_profile: WorkloadProfile
) -> None:
    """A real ELF produces a width in [minimum, maximum].

    Specific values depend on the chosen benchmark, so we only
    assert the contract: pc_width is either 0 (analyzer failed)
    or a sensible width.
    """
    d = PCWidthNarrowing(minimum=8, maximum=32).analyze(
        workload=None,  # type: ignore[arg-type]
        profile=real_elf_profile,
        target=target,
    )
    assert isinstance(d, WidthDecision)
    assert d.pc_width == 0 or 8 <= d.pc_width <= 32


# ─── HWLPAddrNarrowing ────────────────────────────────────────────


def test_hwlp_addr_applicable_to_cv32e40p(target: CV32E40P) -> None:
    assert HWLPAddrNarrowing().applicable(workload=None, target=target) is True  # type: ignore[arg-type]


def test_hwlp_addr_not_applicable_to_stub() -> None:
    assert HWLPAddrNarrowing().applicable(workload=None, target=_StubTarget()) is False  # type: ignore[arg-type]


def test_hwlp_addr_empty_profile_returns_no_op(
    target: CV32E40P, empty_profile: WorkloadProfile
) -> None:
    """No ELFs -> ``hwlp_addr_width = maximum`` (no-op)."""
    d = HWLPAddrNarrowing(maximum=32).analyze(
        workload=None,  # type: ignore[arg-type]
        profile=empty_profile,
        target=target,
    )
    assert d.hwlp_addr_width == 32


def test_hwlp_addr_real_elf_yields_in_range_value(
    target: CV32E40P, real_elf_profile: WorkloadProfile
) -> None:
    d = HWLPAddrNarrowing(minimum=12, maximum=32).analyze(
        workload=None,  # type: ignore[arg-type]
        profile=real_elf_profile,
        target=target,
    )
    assert isinstance(d, WidthDecision)
    assert 12 <= d.hwlp_addr_width <= 32


# ─── CounterWidthNarrowing ────────────────────────────────────────


def test_counter_width_applicable_to_cv32e40p(target: CV32E40P) -> None:
    assert CounterWidthNarrowing().applicable(workload=None, target=target) is True  # type: ignore[arg-type]


def test_counter_width_not_applicable_to_stub() -> None:
    assert CounterWidthNarrowing().applicable(workload=None, target=_StubTarget()) is False  # type: ignore[arg-type]


def test_counter_width_empty_profile_returns_conservative_default(
    target: CV32E40P, empty_profile: WorkloadProfile
) -> None:
    """No loop candidates -> ``counter_width=16`` (legacy default)."""
    d = CounterWidthNarrowing().analyze(
        workload=None,  # type: ignore[arg-type]
        profile=empty_profile,
        target=target,
    )
    assert d.counter_width == 16


def test_counter_width_with_loop_candidate(
    target: CV32E40P,
) -> None:
    """A profile carrying a candidate with asm_path drives the width."""
    bench_root = Path(__file__).resolve().parent.parent.parent / "targets" / "benchmarks"
    if not bench_root.exists():
        pytest.skip(f"No benchmarks at {bench_root}")
    asm = None
    for d in sorted(bench_root.iterdir()):
        if d.is_dir():
            asms = sorted(d.glob("*.s"))
            if asms:
                asm = asms[0]
                break
    if asm is None:
        pytest.skip("No .s assembly file available")

    class _Candidate:
        def __init__(self, asm_path: Path, hw_loop: int) -> None:
            self.asm_path = asm_path
            self.hw_loop = hw_loop

    profile = WorkloadProfile(
        elf_paths=(),
        loops=(_Candidate(asm, hw_loop=2),),  # type: ignore[arg-type]
    )
    d = CounterWidthNarrowing(minimum=8, maximum=32).analyze(
        workload=None,  # type: ignore[arg-type]
        profile=profile,
        target=target,
    )
    assert isinstance(d, WidthDecision)
    assert 8 <= d.counter_width <= 32 or d.counter_width == 16
