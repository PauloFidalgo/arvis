"""
Core descriptor — typed access to a YAML core description file.

Loads a YAML file that describes a RISC-V core's RTL structure,
signal interface, prunable features, and simulation/synthesis settings.

This decouples the specialization tool from any specific core
implementation. To support a new core, create a new YAML descriptor.

Usage:
    from arvis.core_descriptor import CoreDescriptor

    desc = CoreDescriptor.load("targets/cv32e40p/core_descriptor.yaml")
    print(desc.alu.input_a)          # "operand_a_i"
    print(desc.rtl_file("decoder"))  # "cv32e40p_decoder.sv"
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# ═══════════════════════════════════════════════════════════════════════
# Sub-descriptors
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class CoreInfo:
    """Basic core identity."""

    name: str
    isa: str
    num_regs: int = 32
    read_ports: int = 3
    write_ports: int = 2


@dataclass(frozen=True)
class ALUDescriptor:
    """ALU signal interface."""

    input_a: str
    input_b: str
    input_c: str
    output: str
    operator_signal: str = "operator_i"
    enum_prefix: str = "ALU_"
    enum_width: int = 7
    enum_format: str = "{width}'b{value:0{width}b}"
    # Fused immediate bus for variable immediates in fused instructions.
    # Extracted from instruction encoding fields (rs2 + rs3).
    # Only wired to ALU when patterns with variable immediates are used.
    fused_imm: str = "fused_imm_i"
    fused_imm_width: int = 10


@dataclass(frozen=True)
class PragmaDescriptor:
    """Pragma markers for in-place RTL patching."""

    prefix: str = "ARVIS_FUSED"
    begin_format: str = "// {prefix}_BEGIN: {tag}"
    end_format: str = "// {prefix}_END: {tag}"
    tags: Dict[str, str] = field(default_factory=dict)

    def begin_marker(self, tag_key: str) -> str:
        """Generate the BEGIN pragma string for a tag key."""
        tag = self.tags.get(tag_key, tag_key)
        return self.begin_format.format(prefix=self.prefix, tag=tag)

    def end_marker(self, tag_key: str) -> str:
        """Generate the END pragma string for a tag key."""
        tag = self.tags.get(tag_key, tag_key)
        return self.end_format.format(prefix=self.prefix, tag=tag)


@dataclass(frozen=True)
class OpcodeEntry:
    """A custom opcode space entry."""

    name: str
    value: int


@dataclass(frozen=True)
class EncodingDescriptor:
    """Custom instruction encoding space."""

    opcodes: List[OpcodeEntry] = field(default_factory=list)
    funct3_map: Dict[str, int] = field(default_factory=dict)
    r_type_funct7_max: int = 32
    r4_type_funct2_max: int = 4

    @property
    def opcode_values(self) -> List[int]:
        """Return just the opcode integer values."""
        return [o.value for o in self.opcodes]


@dataclass
class PrunableFeature:
    """A single prunable hardware feature."""

    name: str
    parameter: str
    file_key: Optional[str] = None  # Key into rtl.files
    file_keys: Optional[List[str]] = None  # Multiple file keys
    module: Optional[str] = None
    modules: Optional[List[str]] = None
    instance_marker: Optional[str] = None
    fallback_expr: Optional[str] = None
    instructions: List[str] = field(default_factory=list)
    removable_case_labels: List[str] = field(default_factory=list)


@dataclass
class PropagationEntry:
    """Parameter propagation: which module gets which parameters."""

    module: str
    file_key: str
    params: List[str]


@dataclass
class PulpInstanceMarker:
    """A PULP-only instance to wrap in generate-if."""

    start: str
    param: str


@dataclass
class PulpInstanceGroup:
    """Group of PULP instances in a file."""

    file_key: str
    markers: List[PulpInstanceMarker]


@dataclass
class PruningDescriptor:
    """All pruning-related configuration."""

    features: Dict[str, PrunableFeature] = field(default_factory=dict)
    propagation: List[PropagationEntry] = field(default_factory=list)
    pulp_alu_ops: List[str] = field(default_factory=list)
    pulp_mul_ops: List[str] = field(default_factory=list)
    base_synthesis_params: Dict[str, int] = field(default_factory=dict)
    pulp_instances: List[PulpInstanceGroup] = field(default_factory=list)


@dataclass(frozen=True)
class SimulationDescriptor:
    """Simulation settings."""

    top_module: str = "tb_top"
    testbench_dir: str = "example_tb"
    timeout_cycles: int = 5_000_000
    timeout_seconds: int = 600


@dataclass(frozen=True)
class SynthesisDescriptor:
    """Synthesis settings."""

    top_module: str = "cv32e40p_core"
    exclude_files: List[str] = field(default_factory=list)
    bhv_clock_gate: str = ""


# ═══════════════════════════════════════════════════════════════════════
# Main CoreDescriptor
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class CoreDescriptor:
    """Complete descriptor for a RISC-V core's RTL structure.

    Loaded from a YAML file. Provides typed access to all core-specific
    knowledge needed by the specialization pipeline.
    """

    core: CoreInfo = field(default_factory=lambda: CoreInfo(name="unknown", isa="rv32i"))
    rtl_root: str = "rtl/"
    rtl_files: Dict[str, str] = field(default_factory=dict)
    alu: ALUDescriptor = field(
        default_factory=lambda: ALUDescriptor(
            input_a="operand_a_i",
            input_b="operand_b_i",
            input_c="operand_c_i",
            output="result_o",
        )
    )
    pragmas: PragmaDescriptor = field(default_factory=PragmaDescriptor)
    encoding: EncodingDescriptor = field(default_factory=EncodingDescriptor)
    pruning: PruningDescriptor = field(default_factory=PruningDescriptor)
    simulation: SimulationDescriptor = field(default_factory=SimulationDescriptor)
    synthesis: SynthesisDescriptor = field(default_factory=SynthesisDescriptor)

    # ── File path helpers ──

    # ── Module name helpers ──

    # ── Feature helpers ──

    def feature(self, name: str) -> Optional[PrunableFeature]:
        """Get a prunable feature by name."""
        return self.pruning.features.get(name)

    # ── Loading ──

    @classmethod
    def load(cls, path: str) -> "CoreDescriptor":
        """Load a CoreDescriptor from a YAML file."""
        yaml_path = Path(path)
        if not yaml_path.exists():
            raise FileNotFoundError(f"Core descriptor not found: {path}")

        with open(yaml_path) as f:
            data = yaml.safe_load(f)

        return cls._from_dict(data)

    @classmethod
    def load_for_target(cls, rtl_root: str) -> "CoreDescriptor":
        """Load the core descriptor from a target directory.

        Looks for core_descriptor.yaml in the target directory.
        If not found, returns a default CV32E40P descriptor.
        """
        candidates = [
            os.path.join(rtl_root, "core_descriptor.yaml"),
            os.path.join(rtl_root, "core_descriptor.yml"),
        ]
        for path in candidates:
            if os.path.exists(path):
                return cls.load(path)

        # Fallback: return default CV32E40P descriptor
        return cls._default_cv32e40p()

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "CoreDescriptor":
        """Parse a CoreDescriptor from a raw YAML dict."""
        desc = cls()

        # Core info
        core_data = data.get("core", {})
        rf = core_data.get("register_file", {})
        desc.core = CoreInfo(
            name=core_data.get("name", "unknown"),
            isa=core_data.get("isa", "rv32i"),
            num_regs=rf.get("num_regs", 32),
            read_ports=rf.get("read_ports", 3),
            write_ports=rf.get("write_ports", 2),
        )

        # RTL files
        rtl_data = data.get("rtl", {})
        desc.rtl_root = rtl_data.get("root", "rtl/")
        desc.rtl_files = dict(rtl_data.get("files", {}))

        # ALU
        alu_data = data.get("alu", {})
        if alu_data:
            desc.alu = ALUDescriptor(
                input_a=alu_data.get("input_a", "operand_a_i"),
                input_b=alu_data.get("input_b", "operand_b_i"),
                input_c=alu_data.get("input_c", "operand_c_i"),
                output=alu_data.get("output", "result_o"),
                operator_signal=alu_data.get("operator_signal", "operator_i"),
                enum_prefix=alu_data.get("enum_prefix", "ALU_"),
                enum_width=alu_data.get("enum_width", 7),
                enum_format=alu_data.get("enum_format", "{width}'b{value:0{width}b}"),
                fused_imm=alu_data.get("fused_imm", "fused_imm_i"),
                fused_imm_width=alu_data.get("fused_imm_width", 10),
            )

        # Pragmas
        pragma_data = data.get("pragmas", {})
        if pragma_data:
            desc.pragmas = PragmaDescriptor(
                prefix=pragma_data.get("prefix", "ARVIS_FUSED"),
                begin_format=pragma_data.get("begin_format", "// {prefix}_BEGIN: {tag}"),
                end_format=pragma_data.get("end_format", "// {prefix}_END: {tag}"),
                tags=dict(pragma_data.get("tags", {})),
            )

        # Encoding
        enc_data = data.get("encoding", {})
        if enc_data:
            opcodes = []
            for opc in enc_data.get("opcodes", []):
                if isinstance(opc, dict):
                    opcodes.append(
                        OpcodeEntry(
                            name=opc.get("name", ""),
                            value=opc.get("value", 0),
                        )
                    )
                else:
                    opcodes.append(OpcodeEntry(name=f"CUSTOM_{len(opcodes)}", value=opc))
            desc.encoding = EncodingDescriptor(
                opcodes=opcodes,
                funct3_map=dict(enc_data.get("funct3_map", {})),
                r_type_funct7_max=enc_data.get("r_type_funct7_max", 32),
                r4_type_funct2_max=enc_data.get("r4_type_funct2_max", 4),
            )

        # Pruning
        prune_data = data.get("pruning", {})
        if prune_data:
            features = {}
            for name, fdata in prune_data.get("features", {}).items():
                features[name] = PrunableFeature(
                    name=name,
                    parameter=fdata.get("parameter", ""),
                    file_key=fdata.get("file"),
                    file_keys=fdata.get("files"),
                    module=fdata.get("module"),
                    modules=fdata.get("modules"),
                    instance_marker=fdata.get("instance_marker"),
                    fallback_expr=fdata.get("fallback_expr"),
                    instructions=list(fdata.get("instructions", [])),
                    removable_case_labels=list(fdata.get("removable_case_labels", [])),
                )

            propagation = []
            for pdata in prune_data.get("propagation", []):
                propagation.append(
                    PropagationEntry(
                        module=pdata["module"],
                        file_key=pdata["file"],
                        params=list(pdata["params"]),
                    )
                )

            pulp_instances = []
            for pidata in prune_data.get("pulp_instances", []):
                markers = []
                for m in pidata.get("markers", []):
                    markers.append(
                        PulpInstanceMarker(
                            start=m["start"],
                            param=m["param"],
                        )
                    )
                pulp_instances.append(
                    PulpInstanceGroup(
                        file_key=pidata["file"],
                        markers=markers,
                    )
                )

            desc.pruning = PruningDescriptor(
                features=features,
                propagation=propagation,
                pulp_alu_ops=list(prune_data.get("pulp_alu_ops", [])),
                pulp_mul_ops=list(prune_data.get("pulp_mul_ops", [])),
                base_synthesis_params=dict(prune_data.get("base_synthesis_params", {})),
                pulp_instances=pulp_instances,
            )

        # Simulation
        sim_data = data.get("simulation", {})
        if sim_data:
            desc.simulation = SimulationDescriptor(
                top_module=sim_data.get("top_module", "tb_top"),
                testbench_dir=sim_data.get("testbench_dir", "example_tb"),
                timeout_cycles=sim_data.get("timeout_cycles", 5_000_000),
                timeout_seconds=sim_data.get("timeout_seconds", 600),
            )

        # Synthesis
        synth_data = data.get("synthesis", {})
        if synth_data:
            desc.synthesis = SynthesisDescriptor(
                top_module=synth_data.get("top_module", "cv32e40p_core"),
                exclude_files=list(synth_data.get("exclude_files", [])),
                bhv_clock_gate=synth_data.get("bhv_clock_gate", ""),
            )

        return desc

    @classmethod
    def _default_cv32e40p(cls) -> "CoreDescriptor":
        """Return a hardcoded default descriptor for CV32E40P.

        Used as fallback when no YAML file is found. This preserves
        backward compatibility with existing code.
        """
        return cls(
            core=CoreInfo(name="cv32e40p", isa="rv32imc"),
            rtl_root="rtl/",
            rtl_files={
                "pkg": "include/cv32e40p_pkg.sv",
                "decoder": "cv32e40p_decoder.sv",
                "alu": "cv32e40p_alu.sv",
                "mult": "cv32e40p_mult.sv",
                "core": "cv32e40p_core.sv",
                "top": "cv32e40p_top.sv",
                "ex_stage": "cv32e40p_ex_stage.sv",
                "if_stage": "cv32e40p_if_stage.sv",
                "id_stage": "cv32e40p_id_stage.sv",
                "cs_registers": "cv32e40p_cs_registers.sv",
            },
            alu=ALUDescriptor(
                input_a="operand_a_i",
                input_b="operand_b_i",
                input_c="operand_c_i",
                output="result_o",
            ),
            pragmas=PragmaDescriptor(
                prefix="ARVIS_FUSED",
                tags={
                    "alu_opcodes": "alu_opcodes",
                    "decoder_cases": "decoder_custom0",
                    "result_mux": "result_mux",
                },
            ),
            encoding=EncodingDescriptor(
                opcodes=[
                    OpcodeEntry("CUSTOM_0", 0x0B),
                    OpcodeEntry("CUSTOM_1", 0x2B),
                ],
                funct3_map={
                    "2in_1out": 0,
                    "2in_2out": 1,
                    "3in_1out": 2,
                    "3in_2out": 3,
                },
            ),
            simulation=SimulationDescriptor(
                top_module="tb_top",
                testbench_dir="example_tb",
            ),
            synthesis=SynthesisDescriptor(
                top_module="cv32e40p_core",
                exclude_files=[
                    "cv32e40p_register_file_latch.sv",
                    "cv32e40p_fp_wrapper.sv",
                    "cv32e40p_ispm.sv",
                ],
                bhv_clock_gate="cv32e40p_sim_clock_gate.sv",
            ),
        )
