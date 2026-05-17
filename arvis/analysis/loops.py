"""Loop detection via backward-branch scanning with CFG-based natural loop construction."""

from typing import Dict, List, Set, Tuple

from .models import BasicBlock, Loop


class LoopDetector:
    def __init__(self, blocks: Dict[int, BasicBlock], entry_id: int):
        self.blocks = blocks
        self.entry_id = entry_id
        self.dominators: Dict[int, Set[int]] = {}
        # Build address-to-block mapping for resolving branch targets
        self._addr_to_block: Dict[int, int] = {}
        for bid, blk in self.blocks.items():
            for insn in blk.instructions:
                self._addr_to_block[insn.address] = bid

    def find_all_loops(self) -> List[Loop]:
        """Detect loops by finding backward branches in the actual instructions.

        Strategy:
        1. Scan all instructions for **conditional** backward branches
           (branches whose target address <= their own address).  These are
           the loop-closing back-edges.
        2. For each such back-edge, identify the header block (block
           containing the target address) and the tail block (block
           containing the branch instruction).
        3. Build the natural loop body by walking predecessors from the
           tail back to the header.
        4. Merge bodies that share the same header block.
        5. Also detect self-loops from unconditional jumps (e.g. ``wfi``
           spin loops).

        This avoids the false positives of pure dominator-based analysis
        which misidentifies function epilogues, straight-line setup code,
        and Duff's-device / switch-case unconditional backward jumps as
        loop back-edges.
        """
        # ── Step 1: Find conditional backward branches ──
        back_edge_pairs: List[Tuple[int, int]] = []  # (tail_block_id, header_block_id)
        seen_pairs: Set[Tuple[int, int]] = set()

        for bid, blk in self.blocks.items():
            for insn in blk.instructions:
                if insn.branch_target_addr is None:
                    continue
                if insn.is_call:
                    continue
                # Must be a backward branch (target <= current address)
                if insn.branch_target_addr > insn.address:
                    continue

                # Determine if this is a valid loop back-edge
                is_valid_back_edge = False

                if insn.is_branch:
                    # Conditional backward branches are ALWAYS loop back-edges
                    is_valid_back_edge = True
                elif insn.is_unconditional_jump:
                    # Unconditional backward jumps: only count self-loops
                    # (same block, e.g. wfi spin loop) to avoid switch-case
                    # Duff's device false positives
                    target_bid = self._addr_to_block.get(insn.branch_target_addr)
                    if target_bid is not None and target_bid == bid:
                        # Filter out wfi halt loops — these are infinite halt
                        # spin loops, not computational loops worth analyzing
                        is_halt_loop = any(i.mnemonic == "wfi" for i in blk.instructions)
                        if not is_halt_loop:
                            is_valid_back_edge = True

                if not is_valid_back_edge:
                    continue

                # Resolve target to a block
                header_bid = self._addr_to_block.get(insn.branch_target_addr)
                if header_bid is None:
                    continue
                tail_bid = bid

                pair = (tail_bid, header_bid)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    back_edge_pairs.append(pair)

        # ── Step 2: Build natural loops and merge by header ──
        by_header: Dict[int, Set[int]] = {}
        for tail_bid, header_bid in back_edge_pairs:
            body = self._natural_loop((tail_bid, header_bid))
            if header_bid in by_header:
                by_header[header_bid] |= body
            else:
                by_header[header_bid] = body

        # ── Step 3: Create Loop objects ──
        loops = []
        for header, body in by_header.items():
            hdr_block = self.blocks.get(header)
            if hdr_block is None:
                continue

            # Filter out "loops" where header is just a single unconditional jump
            if len(hdr_block.instructions) == 1:
                inst = hdr_block.instructions[0]
                if inst.is_jump and not inst.is_branch:
                    continue

            exits = set()
            for bid in body:
                if bid not in self.blocks:
                    continue
                for succ in self.blocks[bid].successors:
                    if succ not in body:
                        exits.add(succ)

            loops.append(
                Loop(
                    header_block_id=header,
                    body_block_ids=body,
                    back_edge=(min(body), header),  # Representative back edge
                    exit_block_ids=exits,
                )
            )

        self._build_hierarchy(loops)
        return loops

    # ── Dominator computation (kept for potential use by other analyses) ──

    def _natural_loop(self, back_edge: Tuple[int, int]) -> Set[int]:
        tail, header = back_edge
        body = {header, tail}
        wl = [tail] if tail != header else []
        while wl:
            node = wl.pop()
            if node not in self.blocks:
                continue
            for pred in self.blocks[node].predecessors:
                if pred not in body:
                    body.add(pred)
                    wl.append(pred)
        return body

    def _build_hierarchy(self, loops: List[Loop]):
        loops.sort(key=lambda lp: len(lp.body_block_ids))
        for i, inner in enumerate(loops):
            for outer in loops[i + 1 :]:
                if inner.body_block_ids < outer.body_block_ids:
                    inner.parent_loop = outer
                    inner.nesting_depth = outer.nesting_depth + 1
                    outer.child_loops.append(inner)
                    break
