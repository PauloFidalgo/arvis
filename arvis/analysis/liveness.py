"""Global liveness analysis and per-n-gram liveness queries."""

from typing import Dict, Set

from .models import BasicBlock, LivenessResult, RegisterStatus


class LivenessAnalyzer:
    def __init__(self, blocks: Dict[int, BasicBlock]):
        self.blocks = blocks
        self._live_in: Dict[int, Set[str]] = {}
        self._live_out: Dict[int, Set[str]] = {}
        self._computed = False

    def compute(self):
        if self._computed:
            return
        use_sets: Dict[int, Set[str]] = {}
        def_sets: Dict[int, Set[str]] = {}
        for bid, block in self.blocks.items():
            uses, defs = set(), set()
            for inst in block.instructions:
                for r in inst.uses:
                    if r not in defs:
                        uses.add(r)
                for r in inst.defs:
                    defs.add(r)
            use_sets[bid] = uses
            def_sets[bid] = defs

        for bid in self.blocks:
            self._live_in[bid] = set()
            self._live_out[bid] = set()

        changed, iters = True, 0
        while changed and iters < 2000:
            changed = False
            iters += 1
            for bid in self.blocks:
                new_out = set()
                for succ in self.blocks[bid].successors:
                    if succ in self._live_in:
                        new_out |= self._live_in[succ]
                new_in = use_sets.get(bid, set()) | (new_out - def_sets.get(bid, set()))
                if new_in != self._live_in[bid] or new_out != self._live_out[bid]:
                    self._live_in[bid] = new_in
                    self._live_out[bid] = new_out
                    changed = True
        self._computed = True
        print(f"  Liveness converged in {iters} iterations")

    def analyze_ngram(self, block_id: int, start_idx: int, end_idx: int) -> LivenessResult:
        self.compute()
        block = self.blocks[block_id]
        ngram = block.instructions[start_idx:end_idx]
        after = block.instructions[end_idx:]
        result = LivenessResult()

        ngram_defs: Dict[str, int] = {}
        uses_before_def: Set[str] = set()
        defined = set()
        for i, inst in enumerate(ngram):
            for r in inst.uses:
                if r != "x0" and r not in defined:
                    uses_before_def.add(r)
            for r in inst.defs:
                if r != "x0":
                    ngram_defs[r] = i
                    defined.add(r)
        result.all_defs = set(ngram_defs.keys())

        # What is live after the n-gram
        live_after = set(self._live_out.get(block_id, set()))
        for inst in reversed(after):
            for r in inst.defs:
                live_after.discard(r)
            for r in inst.uses:
                if r != "x0":
                    live_after.add(r)

        # Classify
        for reg in set(ngram_defs.keys()) | uses_before_def:
            is_in = reg in uses_before_def
            is_def = reg in ngram_defs
            is_live = reg in live_after

            if is_in and is_def and is_live:
                result.classifications[reg] = RegisterStatus.INPUT_AND_OUTPUT
                result.must_read.add(reg)
                result.must_write.add(reg)
            elif is_in and not is_def:
                result.classifications[reg] = RegisterStatus.EXTERNAL_INPUT
                result.must_read.add(reg)
            elif is_in and is_def and not is_live:
                result.classifications[reg] = RegisterStatus.EXTERNAL_INPUT
                result.must_read.add(reg)
                result.truly_eliminable.add(reg)
            elif not is_in and is_def and is_live:
                result.classifications[reg] = RegisterStatus.EXTERNAL_OUTPUT
                result.must_write.add(reg)
            elif not is_in and is_def and not is_live:
                result.classifications[reg] = RegisterStatus.INTERNAL_ONLY
                result.truly_eliminable.add(reg)

        return result
