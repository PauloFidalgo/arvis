"""Dynamic trace parsing and profile construction."""

import re
from typing import Dict, Set

from .models import BasicBlock, DynamicProfile


class TraceParser:
    def __init__(self, blocks: Dict[int, BasicBlock]):
        self.blocks = blocks
        self.addr_to_block: Dict[int, int] = {}
        self.block_starts: Set[int] = set()
        for bid, block in blocks.items():
            for inst in block.instructions:
                self.addr_to_block[inst.address] = bid
            if block.instructions:
                self.block_starts.add(block.instructions[0].address)

    def parse_trace(self, trace_path: str) -> DynamicProfile:
        profile = DynamicProfile()
        print(f"  Parsing trace: {trace_path}")
        with open(trace_path, "r") as f:
            first_line = f.readline()
        if "core" in first_line.lower():
            self._parse_spike(trace_path, profile)
        else:
            self._parse_pc(trace_path, profile)
        print(f"  Total dynamic instructions: {profile.total_instructions:,}")
        return profile

    def _parse_spike(self, path: str, profile: DynamicProfile):
        pat = re.compile(r"core\s+\d+:\s+0x([0-9a-fA-F]+)")
        with open(path, "r") as f:
            for line in f:
                m = pat.match(line)
                if m:
                    self._record(int(m.group(1), 16), profile)

    def _parse_pc(self, path: str, profile: DynamicProfile):
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    self._record(int(line.replace("0x", ""), 16), profile)
                except ValueError:
                    continue

    def _record(self, pc: int, profile: DynamicProfile):
        profile.instruction_exec_counts[pc] += 1
        profile.total_instructions += 1
        if pc in self.addr_to_block and pc in self.block_starts:
            profile.block_exec_counts[self.addr_to_block[pc]] += 1

    def estimate_from_static(self, blocks: Dict[int, BasicBlock]) -> DynamicProfile:
        print("  No trace — using static estimation")
        profile = DynamicProfile()
        for bid, block in blocks.items():
            profile.block_exec_counts[bid] = 1
            for inst in block.instructions:
                profile.instruction_exec_counts[inst.address] = 1
            profile.total_instructions += len(block.instructions)
        return profile
