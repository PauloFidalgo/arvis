"""
Pipeline phases for CV32E40P workload specialization.

Each phase is a self-contained module that receives a shared context
(ToolConfig + results from previous phases) and returns its own results.

Pipeline order:
  1. profiling    — Disassemble, CFG, loops, trace, memory, ALU usage
  2. selection    — ILP fusion selection, accelerator/SPM candidacy
  3. pruning      — RTL parameter gating + decoder specialization
  4. spm          — (Future) Scratchpad memory generation
  5. fusion_rtl   — (Future) Fused instruction RTL + firmware patching
  6. accelerator  — (Future) Custom accelerator implementation
  7. verification — Verilator simulation + Yosys synthesis
  8. reporting    — HTML, text, CSV reports
"""
