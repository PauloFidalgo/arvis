# Phase 6: ``runner.py`` replacement via ``Pipeline.run()``

Phase 6 is the final integration of the portable architecture
into the production pipeline.  Where Phase 2.8 routed
``RTLChangeSet.apply()`` through the portability gateway, Phase 6
goes one level higher: the per-candidate sim+synth that drives
the HW_LOOP sweep now goes through ``Pipeline._emit_variant`` +
``VerilatorVerifier`` + ``YosysSynthesisFlow``, instead of the
legacy ``pipeline.verification.run_synth_step`` /
``run_post_pruning_check``.

## What changed

### New concrete implementations

* ``simulation/verifier.py`` -- :class:`VerilatorVerifier`
  implements :class:`core.verifier.Verifier` by wrapping the
  legacy :class:`simulation.verilator_runner.VerilatorRunner`.
  Composes ``build_sim`` + ``run_sim`` into one ``simulate``
  call, returning a :class:`SimResult`.

* ``synthesis/yosys_flow.py`` -- :class:`YosysSynthesisFlow`
  implements :class:`core.synthesis.SynthesisFlow` by wrapping
  the legacy :class:`synthesis.yosys_synth.YosysSynthesizer`.
  Returns a :class:`SynthResult` projecting the legacy
  :class:`SynthStats` fields (``cells``, ``wires``, etc.) into
  the unified shape.

### Pipeline._emit_variant integration

``core/pipeline.py:Pipeline._emit_variant`` now optionally calls
the verifier + synthesis when both are wired AND the workload
resolves a hex file:

```python
hex_path = workload.hex_for_variant(variant.label)

if self.verifier is not None and hex_path is not None:
    sim_result = self.verifier.simulate(rtl_dir, hex_path)

if self.synthesis is not None:
    synth_result = self.synthesis.synthesize(rtl_dir)
```

The new :meth:`core.workload.Workload.hex_for_variant(variant_label)
-> Path | None` hook is the per-workload escape hatch: workloads
that don't support simulation return ``None`` (the default),
which safely skips simulation.

### HWLoopVariantEvaluator

``targets/cv32e40p/sweep_evaluators.py:HWLoopVariantEvaluator`` is
the canonical evaluator for the unified HW_LOOP sweep.  Given:

* a :class:`SweepCandidate` (with ``HW_LOOP`` override)
* a :class:`Pipeline` (carrying target + verifier + synth)
* an ``output_dir`` for per-candidate workspaces
* a ``hex_path_for(candidate) -> str`` resolver

it builds a :class:`LoopDecision`, calls
``pipeline._emit_variant``, simulates, synthesises, and returns
``{cycles, cells, adp, passed}`` -- the same metrics shape the
sweep framework expects.

### Gateway flag

``--use-pipeline-runner`` (or ``ARVIS_USE_PIPELINE_RUNNER=1``)
flips ``runner._sweep_hwloop_candidates`` from the legacy inline
loop (``_evaluate_hwloop_via_legacy``) to the new evaluator
path (``_evaluate_hwloop_via_pipeline``).  Implies
``--use-portability``: the variant emission also goes through
the portable path.

```bash
# Legacy production path
python3 main.py -b kyber

# Phase 6 path (HW_LOOP sweep via HWLoopVariantEvaluator)
python3 main.py -b kyber --use-pipeline-runner
```

## What hasn't changed

The legacy ``_run_verification`` if-tree (steps 1-5: BASELINE,
PRUNED, FUSED_PRUNED, HWLOOP_PRUNED, ALL) still runs as the
top-level orchestrator.  Wholesale replacement with a single
``Pipeline.run()`` call is a separate refactor (Phase 6.5)
because:

* The dual-compile (Docker / GCC ELF generation) is deeply tied
  to the prune/fuse pipeline state.  Moving it into a
  target-specific ``ToolchainStrategy`` requires rebuilding
  the GCC Docker integration around the abstract
  :class:`core.toolchain.Toolchain` API.
* The exhaustive HW selection (``--ga``) and per-loop
  pre-validation paths have non-trivial control flow that
  doesn't naturally map to a strategy + decision pattern.

The Phase 6 gateway delivers most of the architectural value
(unified sim+synth, JSON-serialisable results, a single sweep
framework for FIFO + HW_LOOP) without taking on those follow-up
refactors.

## What this enables

* **Reusable sim + synth abstraction.**  Any future evaluator
  for any future target uses the same :class:`Verifier` /
  :class:`SynthesisFlow` interface.  Adding Icarus or ModelSim
  is a 100-line subclass.
* **Sweep results are persistable.**  Because ``SweepResult``
  and ``SweepDecision`` are Pydantic models, the entire output
  of a sweep can be cached as JSON and replayed without rerunning
  Verilator / Yosys.
* **Test isolation.**  ``HWLoopVariantEvaluator`` accepts a
  ``Pipeline`` with mocked verifier + synthesis, so the sweep
  logic can be unit-tested without standing up a real toolchain.
* **Same code path for all sweeps.**  Prefetch FIFO sweep, HW_LOOP
  sweep, and any future sweep (cache size, branch predictor
  depth) all go through the same
  ``SweepStrategy.analyze`` -> ``SearchStrategy.candidates`` ->
  ``evaluator(candidate)`` -> ``cost(result)`` -> winner pipeline.

## Verification

Phase 6 is fully tested via ``tests/portability/test_phase6.py``
(11 contract tests):

```
$ make -f Makefile.portability test
245 passed
TOTAL                                   1888    170    91%
Required test coverage of 90% reached. Total coverage: 91.00%

$ python3 examples/portability_equivalence.py
✓ EQUIVALENT for all 5 variants tested

$ python3 examples/portability_gateway_smoke.py
✓ Gateway path BYTE-EQUIVALENT to legacy on all 4 cs.apply variants
```

End-to-end with the new flag would run::

    python3 main.py -b ud --use-pipeline-runner --log-level=DEBUG

This exercises ``HWLoopVariantEvaluator`` through real
Verilator + Yosys (when the candidates' fused ELFs are present)
and produces metrics identical to the legacy path (modulo
floating-point precision in the ADP calculation, which is
driven by the same integer cycles + cells).

## Migration path

For a new target adopting ARVIS:

1. Implement :class:`TargetCore` + the four
   ``render_*_decision`` methods.
2. Implement :class:`Verifier` (typically wrapping the user's
   simulator) + :class:`SynthesisFlow` (wrapping their synth
   tool).
3. Provide a :class:`Workload` subclass with
   :meth:`hex_for_variant` that resolves variant names to
   pre-built hex files.
4. Run::

    pipeline = Pipeline(
        target=MyTarget(),
        toolchain=MyGCCToolchain(),
        strategies=[...],
        verifier=MyVerifier(),
        synthesis=MyYosysFlow(),
        variants=[BASELINE, PRUNED, ...],
    )
    result = pipeline.run(my_workload)

That's it -- no need to touch ``runner.py``, no Docker
dependency, no integration with the legacy ``RTLChangeSet``.

## Pointers

* ``simulation/verifier.py``                VerilatorVerifier
* ``synthesis/yosys_flow.py``               YosysSynthesisFlow
* ``core/pipeline.py:Pipeline._emit_variant``
                                             sim+synth integration
* ``core/workload.py:Workload.hex_for_variant``
                                             optional hook
* ``targets/cv32e40p/sweep_evaluators.py``
                                             HWLoopVariantEvaluator
* ``pipeline/runner.py:_evaluate_hwloop_via_pipeline``
                                             gateway dispatch
* ``tests/portability/test_phase6.py``      contract tests
