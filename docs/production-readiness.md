# Production Readiness Checklist

This document tracks the production-grade engineering criteria
ARVIS satisfies after Phase 6.5/6.6/6.7 polish.  Each row maps to
a concrete artefact or test in the codebase; reviewers can
verify any claim by running the indicated command.

## Test discipline

| Criterion | Status | Evidence |
|---|---|---|
| Unit tests | ✓ | `make -f Makefile.portability test` -> 263 passed |
| Coverage gate | ✓ | `--cov-fail-under=90` enforced; current 91.00% |
| Byte-equivalence regression guard | ✓ | `examples/portability_equivalence.py` -> 5/5 variants byte-identical |
| Gateway dispatch regression guard | ✓ | `examples/portability_gateway_smoke.py` -> 4/4 variants byte-identical |
| End-to-end pipeline smoke | ✓ | `examples/pipeline_runner_smoke.py` -> exit 0 with markdown report |
| Behavioural-equivalence vs legacy | ✓ | `test_select_winners_matches_legacy_pick_best` |
| Property-based tests | partial | Hypothesis available; not yet wired |
| Mutation testing | not done | `mutmut`/`cosmic-ray` are future polish |

## Type safety

| Criterion | Status | Evidence |
|---|---|---|
| `mypy --strict` on new packages | ✓ | `core/`, `strategies/`, `targets/`, `workloads/`: 0 errors |
| Pydantic v2 validation on Decisions | ✓ | `core/strategy.py` field validators reject malformed input |
| Generic types parametrised | ✓ | `disallow_any_generics` enforced in mypy strict scope |
| `mypy --strict` on legacy hot path | partial | `pipeline/runner.py`: 19 pre-existing errors documented in Phase 6.7 |

The legacy `pipeline/runner.py` carries 19 mypy errors that
predate the portability layer.  Most concern legacy `ctx`
attribute access (free-form attributes on a god-object) and
return-type drift in `pipeline.verification.run_post_pruning_check`.
Fixing them requires touching production code paths that the
21-benchmark sweep depends on; the cost-benefit is poor for a
file scheduled for future Pipeline.run() replacement (Phase 6.5
deferred).

## Style + lint

| Criterion | Status | Evidence |
|---|---|---|
| `ruff` lint clean | ✓ | pre-commit hook enforces on every commit |
| `ruff format` clean | ✓ | pre-commit hook enforces on every commit |
| `codespell` clean | ✓ | pre-commit hook enforces on every commit |
| Pre-commit hooks installed | ✓ | `.pre-commit-config.yaml` in both repos |

## Observability

| Criterion | Status | Evidence |
|---|---|---|
| Structured logging | ✓ | `core/logging_config.py` + per-module loggers |
| `--log-level` CLI flag | ✓ | `main.py` |
| Soft-skips logged with traceback | ✓ | every `try/except` in new packages calls `logger.warning(..., exc_info=True)` or `logger.exception(...)` |
| Per-pass timing | partial | not yet emitted; `apply_dce_cleanup` has `elapsed_seconds` in extra |

## Architecture

| Criterion | Status | Evidence |
|---|---|---|
| Abstract `TargetCore` | ✓ | `core/target.py` |
| Abstract `Strategy[D]` (PEP 695) | ✓ | `core/strategy.py` |
| Abstract `RTLPatch` | ✓ | `core/rtl_patch.py` |
| Abstract `Verifier` + concrete impl | ✓ | `core/verifier.py` + `simulation/verifier.py:VerilatorVerifier` |
| Abstract `SynthesisFlow` + concrete | ✓ | `core/synthesis.py` + `synthesis/yosys_flow.py:YosysSynthesisFlow` |
| Abstract `Reporter` + concrete | ✓ | `core/reporter.py` + `report/markdown_reporter.py:MarkdownReporter` |
| Abstract `Toolchain` | ✓ | `core/toolchain.py` |
| Concrete `Toolchain` impl | ✓ | `toolchains/riscv_gcc.py:RISCVGCCToolchain` wraps GCC + objdump; compile/assemble/disassemble verified |
| Generalised hyperparameter sweep | ✓ | `core/sweep.py` (4 search strategies, 3 concrete sweeps) |
| Pydantic JSON serialisation | ✓ | `Decision` + `SweepResult` + `SweepDecision` round-trip |

## Backwards compatibility

| Criterion | Status | Evidence |
|---|---|---|
| Legacy `RTLChangeSet.apply` works unchanged | ✓ | default path; legacy code untouched |
| Opt-in gateway: `--use-portability` | ✓ | Phase 2.8 commit `cc33db3` |
| Opt-in gateway: `--use-pipeline-runner` | ✓ | Phase 6 commit `75d22aa` |
| Legacy `sweep_prefetch_depth(cfg, ctx, candidates)` works unchanged | ✓ | `pipeline/prefetch_sweep.py` shim |
| Legacy `_sweep_hwloop_candidates` returns same `SweepWinners` shape | ✓ | `test_select_winners_matches_legacy_pick_best` |

## Documentation

| Criterion | Status | Evidence |
|---|---|---|
| Architecture overview | ✓ | `docs/architecture.md` (12 sections, UML + sequence diagrams) |
| Port-to-new-target playbook | ✓ | `docs/portability.md` |
| Sweep authoring tutorial | ✓ | `docs/sweep.md` |
| Runner replacement migration | ✓ | `docs/runner-replacement.md` |
| Production readiness checklist | ✓ | this file |
| API reference (Sphinx/pdoc) | not done | future polish |
| Changelog (CHANGELOG.md) | not done | reconstructable from git log |

## Reproducibility

| Criterion | Status | Evidence |
|---|---|---|
| Pinned Python version | ✓ | `requires-python = ">=3.12"` |
| Pinned dependencies | ✓ | `uv.lock` committed; deterministic resolution |
| Optional deps documented | ✓ | `[project.optional-dependencies]` section |
| Docker reproducibility for GCC | partial | runtime requires Docker; not packaged |
| Verilator + Yosys version-pinned | partial | runtime requires the system tools |

## Operational readiness

| Criterion | Status | Evidence |
|---|---|---|
| CI configuration | not done | recommended: GitHub Actions workflow with `make -f Makefile.portability test` |
| Coverage report in CI | not done | recommended: codecov upload |
| Public-repo gate (80%) vs private-repo gate (90%) | ✓ | reflects benchmark availability |
| Cross-repo test compatibility | ✓ | tests fall back to `arvis.*` import path |
| Release process | not done | future: tagged versions in pyproject.toml |

## Performance

| Criterion | Status | Evidence |
|---|---|---|
| Sequential variant emission | ✓ | `Pipeline.run()` iterates one at a time |
| Parallel variant emission | not done | could use `concurrent.futures.ProcessPoolExecutor`; each variant is independent |
| Sweep result caching | ✓ | `SweepDecision.model_dump_json` enables on-disk caching |
| Synthesis cache | ✓ | `_FIFO_AREA_CACHE` for the canonical depths |

## Outstanding gaps (intentional)

* **Full `runner.py` replacement.**  `_run_verification`'s
  five-step orchestration still uses the legacy if-tree.  The
  Phase 6 gateway routes the HW_LOOP per-candidate sim+synth
  through the new path; `Pipeline.run_full_verification()` now
  drives all 5 standard variants through the unified path with
  `RISCVGCCToolchain` available (Phase 6.7 complete).  Phase 7
  adds the top-level gateway: when ``--use-pipeline-runner`` is
  set, ``run_pipeline`` routes the entire verification step
  (legacy ``_run_verification``) through
  ``_run_verification_via_pipeline`` which uses
  ``Pipeline.run_full_verification``.  Binary generation
  (analysis, fusion, hwloop, pruning) still runs through the
  legacy code because it requires Docker GCC + assembly
  patching.

* **Legacy `pipeline/runner.py` mypy errors (19).**  Pre-existing
  type drift around `pipeline.verification.run_post_pruning_check`
  and `ctx` attribute access.  Fix is part of the wholesale
  runner replacement.

* **GA / exhaustive HW_LOOP selection** (Phase 6.7+ complete).
  Migrated to the sweep framework via :class:`GeneticSearch`
  (binary GA), :class:`LoopSelectionSweep`, and
  :class:`LoopSelectionEvaluator`.  Routes through
  ``_loop_selection_via_sweep`` when ``--use-pipeline-runner``
  is set; the legacy GA path stays available as fallback.
  See ``tests/portability/test_loop_selection_sweep.py``.

These gaps are tracked but not blockers for the current
production-grade quality baseline.

## Verification commands

```bash
# Full test suite + coverage gate
make -f Makefile.portability test

# Byte-equivalence on the 5 standard variants
.venv/bin/python examples/portability_equivalence.py

# Gateway dispatch byte-equivalence
.venv/bin/python examples/portability_gateway_smoke.py

# End-to-end pipeline smoke (analyze + emit + report)
.venv/bin/python examples/pipeline_runner_smoke.py

# Strict mypy on the new packages
.venv/bin/mypy --follow-imports=silent \
    core/ strategies/ \
    targets/cv32e40p/{passes,patches,portability_shim,encoding,variants,core,__init__,sweep_evaluators}.py \
    workloads/ report/markdown_reporter.py \
    simulation/verifier.py synthesis/yosys_flow.py

# Pre-commit hook run
.venv/bin/python -m pre_commit run --all-files
```

All commands return exit 0 on a clean working tree.
