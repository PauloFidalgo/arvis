"""Memory access pattern analysis for cache vs scratchpad decision."""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class MemoryRegion:
    start: int
    end: int
    accesses: int
    is_read: bool = True
    is_write: bool = True

    @property
    def size(self) -> int:
        return self.end - self.start


@dataclass
class MemoryAnalysis:
    total_accesses: int = 0
    total_loads: int = 0
    total_stores: int = 0
    unique_addresses: int = 0
    address_range: Tuple[int, int] = (0, 0)
    hotspots: List[MemoryRegion] = field(default_factory=list)
    locality_score: float = 0.0  # 0=poor, 1=excellent
    recommendation: str = ""
    scratchpad_regions: List[MemoryRegion] = field(default_factory=list)
    scratchpad_total_size: int = 0


class MemoryAnalyzer:
    """Analyze memory access patterns from Spike commit log."""

    # Threshold: if top N regions cover >X% of accesses, use scratchpad
    HOTSPOT_THRESHOLD = 0.70  # 70% of accesses in hotspots = scratchpad candidate
    MIN_REGION_ACCESSES = 0.05  # Region needs 5%+ of accesses to be a hotspot
    CACHE_LINE_SIZE = 32  # bytes

    def __init__(self, text_start: int = 0, text_end: int = 0):
        self.load_addrs: List[int] = []
        self.store_addrs: List[int] = []
        # Filter: exclude code region (text_start to text_end)
        self.text_start = text_start
        self.text_end = text_end

    def parse_commit_log(self, trace_path: str) -> None:
        """Parse Spike --log-commits output for memory addresses."""
        # Pattern: "mem 0xADDRESS" for loads, "mem 0xADDRESS 0xVALUE" for stores
        mem_pattern = re.compile(r"mem\s+0x([0-9a-f]+)(?:\s+0x[0-9a-f]+)?")
        # Commit line pattern to distinguish load vs store
        commit_pattern = re.compile(r"core\s+\d+:\s+3\s+0x[0-9a-f]+\s+\(0x[0-9a-f]+\)\s*(.*)")

        with open(trace_path, "r", errors="ignore") as f:
            for line in f:
                # Look for commit lines with mem access
                m = commit_pattern.match(line)
                if not m:
                    continue
                rest = m.group(1)
                mem_match = mem_pattern.search(rest)
                if not mem_match:
                    continue

                addr = int(mem_match.group(1), 16)

                # Filter: skip if in code region
                if self.text_start and self.text_end:
                    if self.text_start <= addr < self.text_end:
                        continue

                # Store has "mem ADDR VALUE", load has "xN VALUE mem ADDR"
                # If "mem" comes after register write, it's a load
                # If line has two hex values after mem, it's a store
                parts = rest.split()
                is_store = False
                for i, p in enumerate(parts):
                    if p == "mem" and i + 2 < len(parts):
                        # Check if there's a value after address
                        if parts[i + 2].startswith("0x"):
                            is_store = True
                        break

                if is_store:
                    self.store_addrs.append(addr)
                else:
                    self.load_addrs.append(addr)

    def analyze(self) -> MemoryAnalysis:
        """Analyze collected memory accesses."""
        result = MemoryAnalysis()

        all_addrs = self.load_addrs + self.store_addrs
        if not all_addrs:
            result.recommendation = "No memory accesses found"
            return result

        result.total_accesses = len(all_addrs)
        result.total_loads = len(self.load_addrs)
        result.total_stores = len(self.store_addrs)
        result.unique_addresses = len(set(all_addrs))
        result.address_range = (min(all_addrs), max(all_addrs))

        # Count accesses per cache line
        line_counts: Dict[int, int] = defaultdict(int)
        for addr in all_addrs:
            line = addr // self.CACHE_LINE_SIZE
            line_counts[line] += 1

        # Sort lines by access count
        sorted_lines = sorted(line_counts.items(), key=lambda x: -x[1])

        # Find hotspot regions (contiguous cache lines with high access)
        hotspots = self._find_hotspot_regions(sorted_lines, result.total_accesses)
        result.hotspots = hotspots

        # Calculate locality score
        # Good locality = few unique lines relative to accesses
        reuse_factor = result.total_accesses / max(len(line_counts), 1)
        # Normalize: reuse of 1 = no locality, reuse of 100+ = excellent
        result.locality_score = min(reuse_factor / 50, 1.0)

        # Calculate hotspot coverage
        hotspot_accesses = sum(h.accesses for h in hotspots)
        hotspot_coverage = hotspot_accesses / result.total_accesses if result.total_accesses else 0

        # Decision
        if hotspot_coverage >= self.HOTSPOT_THRESHOLD:
            result.recommendation = "SCRATCHPAD"
            result.scratchpad_regions = hotspots
            result.scratchpad_total_size = sum(h.size for h in hotspots)
        else:
            result.recommendation = "CACHE"

        return result

    def _find_hotspot_regions(self, sorted_lines: List[Tuple[int, int]], total: int) -> List[MemoryRegion]:
        """Find contiguous regions with high access counts."""
        if not sorted_lines:
            return []

        min_accesses = int(total * self.MIN_REGION_ACCESSES)

        # Get hot lines (above threshold)
        hot_lines = {line for line, count in sorted_lines if count >= min_accesses}
        if not hot_lines:
            # Fall back to top 10 lines
            hot_lines = {line for line, _ in sorted_lines[:10]}

        # Group into contiguous regions
        regions = []
        sorted_hot = sorted(hot_lines)

        region_start = sorted_hot[0]
        region_end = sorted_hot[0]
        region_accesses = 0

        line_to_count = dict(sorted_lines)

        for line in sorted_hot:
            if line <= region_end + 2:  # Allow small gaps (2 cache lines)
                region_end = line
                region_accesses += line_to_count.get(line, 0)
            else:
                # Save current region
                regions.append(
                    MemoryRegion(
                        start=region_start * self.CACHE_LINE_SIZE,
                        end=(region_end + 1) * self.CACHE_LINE_SIZE,
                        accesses=region_accesses,
                    )
                )
                region_start = line
                region_end = line
                region_accesses = line_to_count.get(line, 0)

        # Don't forget last region
        regions.append(
            MemoryRegion(
                start=region_start * self.CACHE_LINE_SIZE,
                end=(region_end + 1) * self.CACHE_LINE_SIZE,
                accesses=region_accesses,
            )
        )

        # Sort by accesses and return top regions
        regions.sort(key=lambda r: -r.accesses)
        return regions[:5]  # Top 5 hotspot regions
