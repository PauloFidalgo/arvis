"""Control Flow Graph construction."""

from typing import Dict, List, Set

from .models import BasicBlock, Instruction


class CFGBuilder:
    def __init__(self, instructions: List[Instruction]):
        self.instructions = sorted(instructions, key=lambda i: i.address)
        self.blocks: Dict[int, BasicBlock] = {}
        self.addr_to_block: Dict[int, int] = {}
        self._counter = 0

    def build(self) -> Dict[int, BasicBlock]:
        leaders = self._find_leaders()
        self._create_blocks(leaders)
        self._connect_blocks()
        return self.blocks

    def _find_leaders(self) -> Set[int]:
        leaders = set()
        if self.instructions:
            leaders.add(self.instructions[0].address)
        for i, inst in enumerate(self.instructions):
            if inst.is_terminator:
                if inst.branch_target_addr is not None:
                    leaders.add(inst.branch_target_addr)
                if i + 1 < len(self.instructions):
                    leaders.add(self.instructions[i + 1].address)
        return leaders

    def _create_blocks(self, leaders: Set[int]):
        current = None
        prev = None
        for inst in self.instructions:
            if inst.address in leaders:
                if current is not None and prev is not None:
                    current.end_addr = prev.address
                    self.blocks[current.id] = current
                bid = self._counter
                self._counter += 1
                current = BasicBlock(id=bid, start_addr=inst.address, end_addr=inst.address)
            if current is not None:
                current.instructions.append(inst)
                self.addr_to_block[inst.address] = current.id
            prev = inst
        if current is not None and prev is not None:
            current.end_addr = prev.address
            self.blocks[current.id] = current

    def _connect_blocks(self):
        sorted_blocks = sorted(self.blocks.values(), key=lambda b: b.start_addr)
        for i, block in enumerate(sorted_blocks):
            if not block.instructions:
                continue
            last = block.instructions[-1]

            if last.is_branch:
                if last.branch_target_addr is not None and last.branch_target_addr in self.addr_to_block:
                    tid = self.addr_to_block[last.branch_target_addr]
                    if tid not in block.successors:
                        block.successors.append(tid)
                        self.blocks[tid].predecessors.append(block.id)
                if i + 1 < len(sorted_blocks):
                    ft = sorted_blocks[i + 1].id
                    if ft not in block.successors:
                        block.successors.append(ft)
                        self.blocks[ft].predecessors.append(block.id)

            elif last.is_unconditional_jump:
                if last.branch_target_addr is not None and last.branch_target_addr in self.addr_to_block:
                    tid = self.addr_to_block[last.branch_target_addr]
                    if tid not in block.successors:
                        block.successors.append(tid)
                        self.blocks[tid].predecessors.append(block.id)

            elif last.is_ret:
                pass

            elif last.is_call:
                if i + 1 < len(sorted_blocks):
                    ft = sorted_blocks[i + 1].id
                    if ft not in block.successors:
                        block.successors.append(ft)
                        self.blocks[ft].predecessors.append(block.id)
            else:
                if i + 1 < len(sorted_blocks):
                    ft = sorted_blocks[i + 1].id
                    if ft not in block.successors:
                        block.successors.append(ft)
                        self.blocks[ft].predecessors.append(block.id)
