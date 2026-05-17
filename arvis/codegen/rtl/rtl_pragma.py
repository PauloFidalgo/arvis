"""
Unified RTL Pragma Processor.

Processes ARVIS_<PREFIX>_BEGIN/END pragmas in SystemVerilog files.
Supports multiple feature prefixes (HWLP, DBG, IRQ, etc.) with
a single processing engine.

Three pragma types per prefix:
  - REMOVE   — always deleted when feature disabled
  - KEEP     — deleted when feature disabled, kept when enabled
  - <name>   — deleted when disabled, replaced with generated code when enabled

Usage:
    from arvis.codegen.rtl.rtl_pragma import RTLPragmaProcessor

    proc = RTLPragmaProcessor()
    proc.add_feature("HWLP", hw_loop=2, generators=hwlp_generators)
    proc.add_feature("DBG", enabled=False, generators=dbg_generators)
    stats = proc.process_dir(rtl_dir)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class PragmaStats:
    """Statistics from pragma processing."""

    file: str = ""
    removed: int = 0
    kept: int = 0
    replaced: int = 0
    unknown: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.removed + self.kept + self.replaced

    def summary(self) -> str:
        parts = []
        if self.removed:
            parts.append(f"{self.removed} removed")
        if self.kept:
            parts.append(f"{self.kept} kept")
        if self.replaced:
            parts.append(f"{self.replaced} replaced")
        if self.unknown:
            parts.append(f"{len(self.unknown)} unknown: {self.unknown}")
        return f"{self.file}: {', '.join(parts)}" if parts else f"{self.file}: no pragmas"


@dataclass
class FeatureConfig:
    """Configuration for a single pragma feature."""

    prefix: str  # e.g., "HWLP", "DBG", "IRQ"
    enabled: bool = False  # Whether the feature is active
    level: int = 0  # For parameterized features (e.g., HW_LOOP=2)
    generators: Dict[str, Callable] = field(default_factory=dict)


class RTLPragmaProcessor:
    """Process ARVIS_*_BEGIN/END pragmas in SystemVerilog files.

    Supports multiple feature prefixes in a single pass.
    """

    def __init__(self):
        self._features: Dict[str, FeatureConfig] = {}

    def add_feature(
        self,
        prefix: str,
        enabled: bool = False,
        level: int = 0,
        generators: Optional[Dict[str, Callable]] = None,
    ) -> None:
        """Register a feature with its pragma prefix and generators.

        Args:
            prefix: Pragma prefix (e.g., "HWLP" for ARVIS_HWLP_BEGIN/END)
            enabled: Whether the feature is enabled
            level: Parameterized level (e.g., hw_loop count)
            generators: Dict of tag_name -> generator_fn(level, indent) -> str
        """
        self._features[prefix] = FeatureConfig(
            prefix=prefix,
            enabled=enabled or level > 0,
            level=level,
            generators=generators or {},
        )

    def process(self, text: str, filename: str = "") -> Tuple[str, List[PragmaStats]]:
        """Process all registered feature pragmas in a file.

        Handles nested pragmas (innermost first).
        Returns (processed_text, list_of_stats_per_feature).
        """
        all_stats = []

        for prefix, feature in self._features.items():
            stats = PragmaStats(file=f"{filename}:{prefix}")

            # Process iteratively (innermost first for nesting)
            max_iterations = 20
            for _ in range(max_iterations):
                found = False

                def _make_replacer(feat: FeatureConfig, st: PragmaStats):
                    def _replace_match(m: re.Match) -> str:
                        nonlocal found
                        found = True
                        indent = m.group(1)
                        tag = m.group(2)
                        original_content = m.group(3)

                        if tag == "REMOVE":
                            st.removed += 1
                            return ""

                        if tag == "KEEP":
                            if not feat.enabled:
                                st.removed += 1
                                return ""
                            else:
                                st.kept += 1
                                return original_content

                        # Named pragma — call generator
                        gen_fn = feat.generators.get(tag)
                        if gen_fn is None:
                            if not feat.enabled:
                                st.removed += 1
                                return ""
                            st.unknown.append(tag)
                            return original_content

                        replacement = gen_fn(feat.level, indent)
                        if replacement:
                            st.replaced += 1
                        else:
                            st.removed += 1
                        return replacement

                    return _replace_match

                replacer = _make_replacer(feature, stats)

                # Match innermost pragmas first (no nested BEGIN inside content)
                # END line tolerates trailing content (e.g. decoder gen appends comments)
                pattern = re.compile(
                    rf"^([ \t]*)// ARVIS_{re.escape(prefix)}_BEGIN:\s*(\w+)\s*\n"
                    rf"((?:(?!// ARVIS_{re.escape(prefix)}_BEGIN:).)*?)"
                    rf"^[ \t]*// ARVIS_{re.escape(prefix)}_END:\s*\2[^\n]*\n",
                    re.MULTILINE | re.DOTALL,
                )
                text, n = pattern.subn(replacer, text)
                if not found:
                    break

            if stats.total > 0:
                all_stats.append(stats)

        return text, all_stats

    def process_file(self, filepath: Path) -> List[PragmaStats]:
        """Process pragmas in a file in-place."""
        text = filepath.read_text()
        has_any_pragma = any(f"// ARVIS_{prefix}_BEGIN:" in text for prefix in self._features)
        if not has_any_pragma:
            return []
        text, stats = self.process(text, filepath.name)
        filepath.write_text(text)
        return stats

    def process_dir(self, rtl_dir: Path) -> List[PragmaStats]:
        """Process all .sv files in a directory."""
        all_stats = []
        for sv_file in sorted(rtl_dir.glob("*.sv")):
            stats = self.process_file(sv_file)
            all_stats.extend(stats)
        return all_stats
