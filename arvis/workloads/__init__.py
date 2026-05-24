"""Concrete :class:`core.workload.Workload` implementations.

Each module here wraps a real benchmark or benchmark suite into
the abstract :class:`Workload` interface so the new pipeline can
operate on it.

Currently:

- :class:`BenchmarkWorkload` — wraps a single ARVIS benchmark
  directory (the ``targets/benchmarks/<name>/`` layout used by
  the existing pipeline).

Future additions:
- ``EmbenchSuite`` — `WorkloadSuite` over Embench-IoT
  benchmarks.
- ``ManualWorkload`` — single-file ``.c`` / ``.s`` workloads
  for assembly-level testing.
"""

from arvis.workloads.benchmark import BenchmarkWorkload

__all__ = ["BenchmarkWorkload"]
