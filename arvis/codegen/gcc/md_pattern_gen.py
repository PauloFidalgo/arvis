"""Generate GCC RISC-V machine description patterns for fused instructions.

Converts FusedOperation objects (with SV expressions) to GCC RTL
`define_insn` patterns that the instruction selector matches during
code generation.

Handles multiple variants of the same mnemonic pair:
  add_sub_V0: add rd,rs1,rs2 → sub rd2,rd,rs3   (chain via rs1 of sub)
  add_sub_V1: add rd,rs1,rs2 → sub rd2,rs3,rd    (chain via rs2 of sub)

Each variant gets its own define_insn with a unique name and the
correct operand tree structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

# ── Mnemonic → GCC RTL operator mapping ──────────────────────────────

# Maps RISC-V ALU mnemonics to GCC RTL expression codes.
# For binary ops: (OP:SI operand1 operand2)
# For unary ops:  (OP:SI operand1)
MNEM_TO_RTL = {
    "add": ("plus", 2),
    "addi": ("plus", 2),
    "sub": ("minus", 2),
    "and": ("and", 2),
    "andi": ("and", 2),
    "or": ("ior", 2),
    "ori": ("ior", 2),
    "xor": ("xor", 2),
    "xori": ("xor", 2),
    "sll": ("ashift", 2),
    "slli": ("ashift", 2),
    "srl": ("lshiftrt", 2),
    "srli": ("lshiftrt", 2),
    "sra": ("ashiftrt", 2),
    "srai": ("ashiftrt", 2),
    "mul": ("mult", 2),
    "slt": ("lt", 2),
    "slti": ("lt", 2),
    "sltu": ("ltu", 2),
    "sltiu": ("ltu", 2),
    # lui is a special unary op: rd = imm << 12.
    # In fusion it always has a hardcoded immediate; _build_rtl_tree
    # handles it via the _LUI_MNEMONICS special case below.
    "lui": ("ashift", 1),
}

# Mnemonics that produce a constant from a hardcoded immediate
# without consuming a register input.  When one of these appears
# at position 0 with a hardcoded immediate, _build_rtl_tree emits
# a bare (const_int shifted_value) instead of an operation on a
# register operand.
_LUI_MNEMONICS = frozenset({"lui"})

# Memory mnemonics → GCC RTL memory access mode
# These are handled specially: the ALU ops compute the address,
# and the final load/store wraps it in (mem:MODE addr).
MNEM_TO_MEM_RTL: Dict[str, Dict[str, str]] = {
    "lw": {"mode": "SI", "type": "load"},
    "lh": {"mode": "HI", "type": "load", "extend": "sign_extend"},
    "lhu": {"mode": "HI", "type": "load", "extend": "zero_extend"},
    "lb": {"mode": "QI", "type": "load", "extend": "sign_extend"},
    "lbu": {"mode": "QI", "type": "load", "extend": "zero_extend"},
    "sw": {"mode": "SI", "type": "store"},
    "sh": {"mode": "HI", "type": "store"},
    "sb": {"mode": "QI", "type": "store"},
}


@dataclass
class GCCPattern:
    """A single GCC define_insn pattern."""

    name: str  # e.g., "custom_slli_srai"
    rtl_pattern: str  # GCC RTL expression
    asm_template: str  # Assembly output template
    n_operands: int  # Number of operands (output + inputs)
    condition: str = "TARGET_CUSTOM_FUSED"
    attributes: str = '[(set_attr "type" "arith")]'
    comment: str = ""


def generate_patterns(fused_ops: list) -> List[GCCPattern]:
    """Generate GCC define_insn patterns from FusedOperation objects.

    For each FusedOperation, analyzes the mnemonic chain and generates
    the appropriate RTL pattern. Handles:
    - Hardcoded immediates (slli rd,rs,16 → ashift with const_int)
    - Chain positions (which rd feeds which rs)
    - LSU fusions (compute→load, compute→store) with memory RTL
    - Multiple outputs (not yet: requires parallel RTL)

    Parameters
    ----------
    fused_ops : list of FusedOperation
        From the RTL generator (may include LSUFusedOperation).

    Returns
    -------
    list of GCCPattern
    """
    patterns = []

    for fop in fused_ops:
        mnemonics = fop.mnemonics
        imm_values = fop.imm_values
        n = len(mnemonics)

        if n < 2:
            continue

        # 3-gram GIMPLE patterns (opcode 0x5B) use the GIMPLE plugin,
        # not .md define_insn patterns — skip with explanation.
        # Cat2 GIMPLE patterns (opcode 0x2B) also use the plugin.
        fop_opcode = getattr(fop, "opcode", 0x0B)
        if fop_opcode in (0x5B, 0x2B, 0x7B) and fop_opcode != 0x0B:
            print(f"  ℹ️  CUSTOM_{fop_opcode:02X} pattern {fop.name}: from GIMPLE plugin, not for .md file")
            continue

        # Check if this is an LSU fused operation
        memory_type = getattr(fop, "memory_type", "")

        try:
            if memory_type == "load_compute":
                rtl, n_inputs, asm_tmpl = _build_load_compute_rtl_tree(
                    mnemonics,
                    imm_values,
                    fop.funct3,
                    fop.funct7,
                    fop.n_inputs,
                )
                attr = '[(set_attr "type" "load")]'
            elif memory_type in ("load", "store"):
                rtl, n_inputs, asm_tmpl = _build_lsu_rtl_tree(
                    mnemonics,
                    imm_values,
                    fop.funct3,
                    fop.funct7,
                    fop.n_inputs,
                    memory_type,
                )
                attr = '[(set_attr "type" "load")]' if memory_type == "load" else '[(set_attr "type" "store")]'
            else:
                rtl, n_inputs, asm_tmpl = _build_rtl_tree(
                    mnemonics,
                    imm_values,
                    fop.funct3,
                    fop.funct7,
                    fop.n_inputs,
                )
                attr = '[(set_attr "type" "arith")]'
        except ValueError as e:
            print(f"  ⚠️  Cannot generate GCC pattern for {fop.name}: {e}")
            continue

        pattern = GCCPattern(
            name=f"custom_{fop.name.lower()}",
            rtl_pattern=rtl,
            asm_template=asm_tmpl,
            n_operands=1 + n_inputs,  # 1 output + n inputs
            comment=fop.description,
            attributes=attr,
        )
        patterns.append(pattern)

    return patterns


def _build_rtl_tree(
    mnemonics: Tuple[str, ...],
    imm_values: Dict[int, int],
    funct3: int,
    funct7: int,
    n_inputs: int,
) -> Tuple[str, int, str]:
    """Build the GCC RTL expression tree for a fused operation.

    Returns (rtl_pattern, n_input_operands, asm_template).

    The RTL pattern uses %0 for output, %1..%N for inputs.
    Hardcoded immediates become (const_int VALUE).
    """
    n = len(mnemonics)

    # Track which operand index we're assigning to external inputs
    # %0 = output (rd), %1 = first input (rs1), %2 = second (rs2), etc.
    next_operand = 1  # 0 is the output

    # Build the expression tree from inside out
    # First instruction gets the external inputs directly
    # Subsequent instructions use the result of the previous

    # For the first instruction:
    mnem0 = mnemonics[0]
    rtl_op0 = MNEM_TO_RTL.get(mnem0)
    if rtl_op0 is None:
        raise ValueError(f"No RTL mapping for mnemonic '{mnem0}'")

    op_name0, arity0 = rtl_op0

    if mnem0 in _LUI_MNEMONICS:
        # LUI is special: rd = imm << 12.  No register input.
        if 0 in imm_values:
            # Hardcoded immediate → pure constant
            shifted = (imm_values[0] & 0xFFFFF) << 12
            expr = f"(const_int {shifted})"
        else:
            # Variable immediate promoted to register input:
            # lui rd, rs → rd = rs << 12
            op1 = next_operand
            next_operand += 1
            expr = f'(ashift:SI (match_operand:SI {op1} "register_operand" "r")\n                   (const_int 12))'
    elif 0 in imm_values:
        # First operand is external, second is hardcoded immediate
        expr = (
            f"({op_name0}:SI (match_operand:SI {next_operand} "
            f'"register_operand" "r")\n'
            f"                     (const_int {imm_values[0]}))"
        )
        next_operand += 1
    elif arity0 == 2:
        op1 = next_operand
        next_operand += 1
        op2 = next_operand
        next_operand += 1
        expr = (
            f"({op_name0}:SI (match_operand:SI {op1} "
            f'"register_operand" "r")\n'
            f"                   (match_operand:SI {op2} "
            f'"register_operand" "r"))'
        )
    else:
        op1 = next_operand
        next_operand += 1
        expr = f'({op_name0}:SI (match_operand:SI {op1} "register_operand" "r"))'

    # Chain subsequent instructions: each wraps the previous expression
    for i in range(1, n):
        mnem = mnemonics[i]
        rtl_op = MNEM_TO_RTL.get(mnem)
        if rtl_op is None:
            raise ValueError(f"No RTL mapping for mnemonic '{mnem}'")

        op_name, arity = rtl_op

        if i in imm_values:
            # Chained result + hardcoded immediate
            expr = f"({op_name}:SI\n          {expr}\n          (const_int {imm_values[i]}))"
        elif arity == 2:
            # Chained result + external input
            op_ext = next_operand
            next_operand += 1
            expr = f'({op_name}:SI\n          {expr}\n          (match_operand:SI {op_ext} "register_operand" "r"))'
        else:
            expr = f"({op_name}:SI\n          {expr})"

    # Wrap in (set (match_operand:SI 0 ...) ...)
    full_rtl = f'(set (match_operand:SI 0 "register_operand" "=r")\n        {expr})'

    # Build assembly template
    n_input_ops = next_operand - 1
    if n_inputs >= 3 or n_input_ops >= 3:
        funct2 = funct7 & 0x03
        operands = ", ".join(f"%{i}" for i in range(n_input_ops + 1))
        asm_tmpl = f".insn r4 0x0b, {funct3}, {funct2}, {operands}"
    else:
        operands = ", ".join(f"%{i}" for i in range(n_input_ops + 1))
        # R-type .insn always needs rd, rs1, rs2 (3 registers).
        # For 1-input ops (e.g., slli+srai), pad with zero (x0).
        if n_input_ops < 2:
            operands += ", zero"
        asm_tmpl = f".insn r 0x0b, {funct3}, 0x{funct7:02x}, {operands}"

    return full_rtl, n_input_ops, asm_tmpl


def _build_lsu_rtl_tree(
    mnemonics: Tuple[str, ...],
    imm_values: Dict[int, int],
    funct3: int,
    funct7: int,
    n_inputs: int,
    memory_type: str,
) -> Tuple[str, int, str]:
    """Build the GCC RTL expression tree for an LSU fused operation.

    For COMPUTE→LOAD (e.g., slli + add + lw):
      (set (match_operand:SI 0 "register_operand" "=r")
           (mem:SI (address_expr)))
      For sub-word loads with sign/zero extension:
      (set (match_operand:SI 0 "register_operand" "=r")
           (sign_extend:SI (mem:HI (address_expr))))

    For COMPUTE→STORE (e.g., slli + add + sw):
      (set (mem:SI (address_expr))
           (match_operand:SI N "register_operand" "r"))

    The address_expr is built from the ALU mnemonics (all except
    the last load/store mnemonic).
    """
    n = len(mnemonics)
    mem_mnem = mnemonics[-1]
    alu_mnems = mnemonics[:-1]

    mem_info = MNEM_TO_MEM_RTL.get(mem_mnem)
    if mem_info is None:
        raise ValueError(f"No memory RTL mapping for '{mem_mnem}'")

    mem_mode = mem_info["mode"]
    extend = mem_info.get("extend")

    # Build the address expression from ALU mnemonics
    next_operand = 1 if memory_type == "load" else 0
    # For stores: operand 0 is NOT the output (no rd write)

    # Build ALU chain for address computation
    if len(alu_mnems) == 0:
        # No ALU prefix — address is just a register (+ optional offset)
        addr_op = next_operand
        next_operand += 1
        load_imm_pos = n - 1
        load_offset = imm_values.get(load_imm_pos, 0)
        if load_offset:
            addr_expr = (
                f"(plus:SI (match_operand:SI {addr_op} "
                f'"register_operand" "r")\n'
                f"                  (const_int {load_offset}))"
            )
        else:
            addr_expr = f'(match_operand:SI {addr_op} "register_operand" "r")'
    else:
        # Build ALU chain
        mnem0 = alu_mnems[0]
        rtl_op0 = MNEM_TO_RTL.get(mnem0)
        if rtl_op0 is None:
            raise ValueError(f"No RTL mapping for mnemonic '{mnem0}'")
        op_name0, arity0 = rtl_op0

        if 0 in imm_values:
            addr_expr = (
                f"({op_name0}:SI (match_operand:SI {next_operand} "
                f'"register_operand" "r")\n'
                f"                     (const_int {imm_values[0]}))"
            )
            next_operand += 1
        elif arity0 == 2:
            op1 = next_operand
            next_operand += 1
            op2 = next_operand
            next_operand += 1
            addr_expr = (
                f"({op_name0}:SI (match_operand:SI {op1} "
                f'"register_operand" "r")\n'
                f"                   (match_operand:SI {op2} "
                f'"register_operand" "r"))'
            )
        else:
            op1 = next_operand
            next_operand += 1
            addr_expr = f'({op_name0}:SI (match_operand:SI {op1} "register_operand" "r"))'

        # Chain subsequent ALU ops
        for i in range(1, len(alu_mnems)):
            mnem = alu_mnems[i]
            rtl_op = MNEM_TO_RTL.get(mnem)
            if rtl_op is None:
                raise ValueError(f"No RTL mapping for mnemonic '{mnem}'")
            op_name, arity = rtl_op

            if i in imm_values:
                addr_expr = f"({op_name}:SI\n          {addr_expr}\n          (const_int {imm_values[i]}))"
            elif arity == 2:
                op_ext = next_operand
                next_operand += 1
                addr_expr = (
                    f"({op_name}:SI\n"
                    f"          {addr_expr}\n"
                    f"          (match_operand:SI {op_ext} "
                    f'"register_operand" "r"))'
                )
            else:
                addr_expr = f"({op_name}:SI\n          {addr_expr})"

        # Add load offset if present
        load_imm_pos = n - 1
        load_offset = imm_values.get(load_imm_pos, 0)
        if load_offset:
            addr_expr = f"(plus:SI\n          {addr_expr}\n          (const_int {load_offset}))"

    n_input_ops = next_operand - (1 if memory_type == "load" else 0)

    if memory_type == "load":
        # Build: (set rd (mem:MODE addr)) or
        #        (set rd (sign_extend:SI (mem:MODE addr)))
        mem_expr = f"(mem:{mem_mode} {addr_expr})"
        if extend and mem_mode != "SI":
            mem_expr = f"({extend}:SI {mem_expr})"

        full_rtl = f'(set (match_operand:SI 0 "register_operand" "=r")\n        {mem_expr})'
    else:
        # Store: (set (mem:MODE addr) data_operand)
        data_op = next_operand
        next_operand += 1
        n_input_ops = next_operand  # all operands are inputs for stores

        mem_expr = f"(mem:{mem_mode} {addr_expr})"
        full_rtl = f'(set {mem_expr}\n        (match_operand:{mem_mode} {data_op} "register_operand" "r"))'

    # Build assembly template
    if n_inputs >= 3 or (next_operand - 1) >= 3:
        funct2 = funct7 & 0x03
        operands = ", ".join(f"%{i}" for i in range(next_operand))
        if memory_type == "load":
            asm_tmpl = f".insn r4 0x0b, {funct3}, {funct2}, {operands}"
        else:
            asm_tmpl = f".insn r4 0x0b, {funct3}, {funct2}, x0, {operands}"
    else:
        operands = ", ".join(f"%{i}" for i in range(next_operand))
        if memory_type == "store":
            asm_tmpl = f".insn r 0x0b, {funct3}, 0x{funct7:02x}, x0, {operands}"
        else:
            if next_operand - 1 < 2:
                operands += ", zero"
            asm_tmpl = f".insn r 0x0b, {funct3}, 0x{funct7:02x}, {operands}"

    return full_rtl, n_input_ops, asm_tmpl


def _build_load_compute_rtl_tree(
    mnemonics: Tuple[str, ...],
    imm_values: Dict[int, int],
    funct3: int,
    funct7: int,
    n_inputs: int,
) -> Tuple[str, int, str]:
    """Build the GCC RTL expression tree for a LOAD→COMPUTE fused operation.

    Pattern: LOAD then ALU ops on the loaded data.
    Example: lhu → sub → result = (MEM[rs1+off] - rs2)

    The GCC RTL tree wraps the memory access inside the compute expression:
      (set (match_operand:SI 0 "register_operand" "=r")
           (minus:SI (zero_extend:SI (mem:HI (match_operand:SI 1 ...)))
                     (match_operand:SI 2 ...)))

    Returns (rtl_pattern, n_input_operands, asm_template).
    """
    mem_mnem = mnemonics[0]
    compute_mnems = mnemonics[1:]

    mem_info = MNEM_TO_MEM_RTL.get(mem_mnem)
    if mem_info is None:
        raise ValueError(f"No memory RTL mapping for '{mem_mnem}'")

    mem_mode = mem_info["mode"]
    extend = mem_info.get("extend")

    next_operand = 1  # 0 is the output (rd)

    # Build the memory access expression (address = rs1 + offset)
    addr_op = next_operand
    next_operand += 1
    load_offset = imm_values.get(0, 0)

    if load_offset:
        addr_expr = (
            f"(plus:SI (match_operand:SI {addr_op} "
            f'"register_operand" "r")\n'
            f"                  (const_int {load_offset}))"
        )
    else:
        addr_expr = f'(match_operand:SI {addr_op} "register_operand" "r")'

    # Build the load expression with optional sign/zero extension
    load_expr = f"(mem:{mem_mode} {addr_expr})"
    if extend and mem_mode != "SI":
        load_expr = f"({extend}:SI {load_expr})"

    # Now build the compute chain on top of the loaded data
    if len(compute_mnems) == 0:
        expr = load_expr
    else:
        # First compute instruction: operand a = loaded data, operand b = external/imm
        mnem0 = compute_mnems[0]
        rtl_op0 = MNEM_TO_RTL.get(mnem0)
        if rtl_op0 is None:
            raise ValueError(f"No RTL mapping for mnemonic '{mnem0}'")
        op_name0, arity0 = rtl_op0

        imm_idx = 1  # position 1 in original pattern = position 0 in compute_mnems
        if imm_idx in imm_values:
            expr = f"({op_name0}:SI\n          {load_expr}\n          (const_int {imm_values[imm_idx]}))"
        elif arity0 == 2:
            op_ext = next_operand
            next_operand += 1
            expr = (
                f'({op_name0}:SI\n          {load_expr}\n          (match_operand:SI {op_ext} "register_operand" "r"))'
            )
        else:
            expr = f"({op_name0}:SI\n          {load_expr})"

        # Chain remaining compute instructions
        for i in range(1, len(compute_mnems)):
            mnem = compute_mnems[i]
            rtl_op = MNEM_TO_RTL.get(mnem)
            if rtl_op is None:
                raise ValueError(f"No RTL mapping for mnemonic '{mnem}'")
            op_name, arity = rtl_op

            orig_idx = i + 1  # position in original pattern
            if orig_idx in imm_values:
                expr = f"({op_name}:SI\n          {expr}\n          (const_int {imm_values[orig_idx]}))"
            elif arity == 2:
                op_ext = next_operand
                next_operand += 1
                expr = f'({op_name}:SI\n          {expr}\n          (match_operand:SI {op_ext} "register_operand" "r"))'
            else:
                expr = f"({op_name}:SI\n          {expr})"

    # Wrap in set
    full_rtl = f'(set (match_operand:SI 0 "register_operand" "=r")\n        {expr})'

    n_input_ops = next_operand - 1

    # Build assembly template
    if n_inputs >= 3:
        funct2 = funct7 & 0x03
        operands = ", ".join(f"%{i}" for i in range(n_input_ops + 1))
        asm_tmpl = f".insn r4 0x0b, {funct3}, {funct2}, {operands}"
    else:
        operands = ", ".join(f"%{i}" for i in range(n_input_ops + 1))
        if n_input_ops < 2:
            operands += ", zero"
        asm_tmpl = f".insn r 0x0b, {funct3}, 0x{funct7:02x}, {operands}"

    return full_rtl, n_input_ops, asm_tmpl


def format_define_insn(pattern: GCCPattern) -> str:
    """Format a GCCPattern as a GCC define_insn string."""
    lines = []
    if pattern.comment:
        lines.append(f";; {pattern.comment}")

    lines.append(f'(define_insn "{pattern.name}"')
    lines.append(f"  [{pattern.rtl_pattern}]")
    lines.append(f'  "{pattern.condition}"')
    lines.append(f'  "{pattern.asm_template}"')
    lines.append(f"  {pattern.attributes})")
    lines.append("")

    return "\n".join(lines)


def generate_md_file(
    fused_ops: list,
    output_path: str,
) -> str:
    """Generate a complete .md file with all custom instruction patterns.

    This file can be included in GCC's riscv.md or used as a
    standalone file with (include "custom-fused.md").

    Parameters
    ----------
    fused_ops : list of FusedOperation
    output_path : str
        Where to write the .md file.

    Returns
    -------
    str
        The generated content.
    """
    patterns = generate_patterns(fused_ops)

    lines = [
        ";; Auto-generated custom fused instruction patterns",
        ";; Generated by cv32e40p_specializer",
        ";;",
        ";; Include this file in gcc/config/riscv/riscv.md:",
        ';;   (include "custom-fused.md")',
        ";;",
        ";; Enable with: -mcustom-fused",
        ";;",
        "",
    ]

    for p in patterns:
        lines.append(format_define_insn(p))

    content = "\n".join(lines)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(content)

    print(f"  Generated {len(patterns)} GCC patterns → {output_path}")
    for p in patterns:
        print(f"    {p.name}: {p.asm_template}")

    return content
