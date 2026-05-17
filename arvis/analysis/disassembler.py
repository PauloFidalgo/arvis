"""RISC-V ELF disassembly via objdump."""

import re
import subprocess
from typing import List, Optional, Tuple

from .models import Instruction, normalize_reg


class RISCVDisassembler:
    OBJDUMP_CANDIDATES = [
        "riscv32-unknown-elf-objdump",
        "riscv64-unknown-elf-objdump",
        "riscv-none-elf-objdump",
        "riscv-none-embed-objdump",
        "riscv32-none-elf-objdump",
        "llvm-objdump",
    ]

    def __init__(self, objdump_path: Optional[str] = None):
        self.objdump = objdump_path or self._find_objdump()

    def _find_objdump(self) -> str:
        for candidate in self.OBJDUMP_CANDIDATES:
            try:
                result = subprocess.run([candidate, "--version"], capture_output=True, timeout=5)
                if result.returncode == 0:
                    return candidate
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        raise RuntimeError("Cannot find RISC-V objdump. Tried: " + ", ".join(self.OBJDUMP_CANDIDATES))

    def disassemble(self, elf_path: str) -> List[Instruction]:
        print(f"  Disassembling {elf_path} using {self.objdump}...")
        result = subprocess.run(
            [self.objdump, "-d", "-M", "no-aliases", "--no-show-raw-insn", elf_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            result = subprocess.run([self.objdump, "-d", elf_path], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"objdump failed: {result.stderr}")
        instructions = self._parse_objdump(result.stdout)
        print(f"  Parsed {len(instructions)} instructions")
        return instructions

    def _parse_objdump(self, output: str) -> List[Instruction]:
        instructions = []
        pattern = re.compile(r"^\s*([0-9a-f]+):\s+(?:([0-9a-fA-F]{4,})\s+)?(\S+)(?:\s+(.*))?$")
        for line in output.splitlines():
            m = pattern.match(line)
            if not m:
                continue
            addr_str, raw_str, mnemonic, operands_str = m.groups()
            if mnemonic in ("...",):
                continue
            addr = int(addr_str, 16)
            # Handle .insn directives (custom instructions) — parse them as
            # opaque instructions so they appear in address-to-block mappings.
            if mnemonic.startswith("."):
                if mnemonic == ".insn":
                    # .insn <size>, <encoding>  e.g. ".insn 4, 0x06c5cb0b"
                    insn_size = 4
                    raw_val = 0
                    operands_str = operands_str.strip() if operands_str else ""
                    if operands_str:
                        insn_parts = [p.strip() for p in operands_str.split(",")]
                        if len(insn_parts) >= 1:
                            try:
                                insn_size = int(insn_parts[0])
                            except ValueError:
                                pass
                        if len(insn_parts) >= 2:
                            try:
                                raw_val = int(insn_parts[1], 0)
                            except ValueError:
                                pass
                    inst = Instruction(
                        address=addr,
                        raw=raw_val,
                        mnemonic=".insn",
                        operands_raw=operands_str,
                        size=insn_size,
                    )
                    instructions.append(inst)
                continue
            raw = int(raw_str, 16) if raw_str else 0
            operands_str = operands_str.strip() if operands_str else ""
            size = 2 if (raw & 0x3) != 0x3 and raw <= 0xFFFF else 4
            rd, rs1, rs2, imm, branch_target = self._parse_operands(mnemonic, operands_str, addr)
            inst = Instruction(
                address=addr,
                raw=raw,
                mnemonic=mnemonic,
                operands_raw=operands_str,
                rd=rd,
                rs1=rs1,
                rs2=rs2,
                imm=imm,
                size=size,
                branch_target_addr=branch_target,
            )
            instructions.append(inst)
        return instructions

    def _parse_operands(
        self, mnemonic: str, operands: str, pc: int
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int], Optional[int]]:
        rd = rs1 = rs2 = None
        imm = None
        branch_target = None

        if not operands:
            return rd, rs1, rs2, imm, branch_target

        operands = operands.split("#")[0].strip()
        operands = re.sub(r"<[^>]+>", "", operands).strip()
        parts = [p.strip() for p in operands.split(",")]

        # ── Branch: rs1, rs2, target ──
        if mnemonic in ("beq", "bne", "blt", "bge", "bltu", "bgeu"):
            if len(parts) >= 3:
                rs1 = normalize_reg(parts[0])
                rs2 = normalize_reg(parts[1])
                branch_target = self._parse_target(parts[2])
            return rd, rs1, rs2, imm, branch_target

        if mnemonic in ("c.beqz", "c.bnez"):
            if len(parts) >= 2:
                rs1 = normalize_reg(parts[0])
                branch_target = self._parse_target(parts[1])
            return rd, rs1, rs2, imm, branch_target

        # ── JAL ──
        if mnemonic == "jal":
            if len(parts) == 1:
                rd = "x1"
                branch_target = self._parse_target(parts[0])
            elif len(parts) >= 2:
                rd = normalize_reg(parts[0])
                branch_target = self._parse_target(parts[1])
            return rd, rs1, rs2, imm, branch_target

        # ── JALR ──
        if mnemonic == "jalr":
            if len(parts) == 3:
                rd = normalize_reg(parts[0])
                rs1 = normalize_reg(parts[1])
                imm = self._parse_imm(parts[2])
            elif len(parts) == 2:
                rd = normalize_reg(parts[0])
                rs1, imm = self._parse_mem_operand(parts[1])
            elif len(parts) == 1:
                rd = "x1"
                rs1 = normalize_reg(parts[0])
            return rd, rs1, rs2, imm, branch_target

        if mnemonic in ("c.j", "c.jal"):
            if parts:
                branch_target = self._parse_target(parts[0])
                rd = "x0" if mnemonic == "c.j" else "x1"
            return rd, rs1, rs2, imm, branch_target

        if mnemonic in ("c.jr", "c.jalr"):
            if parts:
                rs1 = normalize_reg(parts[0])
                rd = "x0" if mnemonic == "c.jr" else "x1"
            return rd, rs1, rs2, imm, branch_target

        # ── Loads ──
        if mnemonic in ("lb", "lbu", "lh", "lhu", "lw", "flw", "c.lw"):
            if len(parts) >= 2:
                rd = normalize_reg(parts[0])
                rs1, imm = self._parse_mem_operand(parts[1])
            return rd, rs1, rs2, imm, branch_target

        if mnemonic == "c.lwsp":
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])
                rs1 = "x2"  # sp is ALWAYS the base
                if len(parts) >= 2:
                    if "(" in parts[1]:
                        _, imm = self._parse_mem_operand(parts[1])
                    else:
                        imm = self._parse_imm(parts[1])
            return rd, rs1, rs2, imm, branch_target

        # ── Stores ──
        if mnemonic in ("sb", "sh", "sw", "fsw", "c.sw"):
            if len(parts) >= 2:
                rs2 = normalize_reg(parts[0])
                rs1, imm = self._parse_mem_operand(parts[1])
            return rd, rs1, rs2, imm, branch_target

        if mnemonic == "c.swsp":
            if len(parts) >= 1:
                rs2 = normalize_reg(parts[0])
                rs1 = "x2"  # sp is ALWAYS the base
                if len(parts) >= 2:
                    if "(" in parts[1]:
                        _, imm = self._parse_mem_operand(parts[1])
                    else:
                        imm = self._parse_imm(parts[1])
            return rd, rs1, rs2, imm, branch_target

        # ── SP-relative compressed ──
        if mnemonic == "c.addi16sp":
            # c.addi16sp sp, imm  →  sp = sp + imm
            rd = "x2"
            rs1 = "x2"
            for p in parts:
                v = self._parse_imm(p)
                if v is not None:
                    imm = v
                    break
            return rd, rs1, rs2, imm, branch_target

        if mnemonic == "c.addi4spn":
            # c.addi4spn rd, sp, imm  →  rd = sp + imm
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])
            rs1 = "x2"  # sp is ALWAYS the source
            for p in parts[1:]:
                v = self._parse_imm(p)
                if v is not None:
                    imm = v
                    break
            return rd, rs1, rs2, imm, branch_target

        # ── R-type ALU ──
        if mnemonic in (
            "add",
            "sub",
            "sll",
            "slt",
            "sltu",
            "xor",
            "srl",
            "sra",
            "or",
            "and",
            "mul",
            "mulh",
            "mulhsu",
            "mulhu",
            "div",
            "divu",
            "rem",
            "remu",
        ):
            if len(parts) >= 3:
                rd = normalize_reg(parts[0])
                rs1 = normalize_reg(parts[1])
                rs2 = normalize_reg(parts[2])
            return rd, rs1, rs2, imm, branch_target

        # ── I-type ALU ──
        if mnemonic in ("addi", "slti", "sltiu", "xori", "ori", "andi", "slli", "srli", "srai"):
            if len(parts) >= 3:
                rd = normalize_reg(parts[0])
                rs1 = normalize_reg(parts[1])
                imm = self._parse_imm(parts[2])
            return rd, rs1, rs2, imm, branch_target

        # ── U-type ──
        if mnemonic in ("lui", "auipc"):
            if len(parts) >= 2:
                rd = normalize_reg(parts[0])
                imm = self._parse_imm(parts[1])
            return rd, rs1, rs2, imm, branch_target

        # ── Compressed ALU ──
        if mnemonic in ("c.addi", "c.slli", "c.srli", "c.srai", "c.andi"):
            if len(parts) >= 2:
                rd = normalize_reg(parts[0])
                rs1 = rd
                imm = self._parse_imm(parts[1])
            return rd, rs1, rs2, imm, branch_target

        if mnemonic in ("c.add", "c.sub", "c.and", "c.or", "c.xor"):
            if len(parts) >= 2:
                rd = normalize_reg(parts[0])
                rs1 = rd
                rs2 = normalize_reg(parts[1])
            return rd, rs1, rs2, imm, branch_target

        if mnemonic == "c.mv":
            if len(parts) >= 2:
                rd = normalize_reg(parts[0])
                rs2 = normalize_reg(parts[1])
            return rd, rs1, rs2, imm, branch_target

        if mnemonic in ("c.li", "c.lui"):
            if len(parts) >= 2:
                rd = normalize_reg(parts[0])
                imm = self._parse_imm(parts[1])
            return rd, rs1, rs2, imm, branch_target

        # ── CSR instructions ──
        # csrrs/csrrw/csrrc rd, csr_name, rs1  (3 operands)
        # csrrwi/csrrsi/csrrci rd, csr_name, imm  (3 operands)
        # csrr rd, csr_name  (pseudo, 2 operands)
        # csrw csr_name, rs1  (pseudo, 2 operands)
        if mnemonic in ("csrrw", "csrrs", "csrrc", "csrrwi", "csrrsi", "csrrci"):
            if len(parts) >= 3:
                rd = normalize_reg(parts[0])
                # parts[1] is the CSR name (e.g., "mcycle") — convert to address
                try:
                    imm = int(parts[1], 0)
                except ValueError:
                    # CSR name string — look up address
                    from arvis.analysis.instruction_usage import CSR_NAMES

                    csr_name = parts[1].strip()
                    # Reverse lookup: name → address
                    csr_addr = None
                    for addr, name in CSR_NAMES.items():
                        if name == csr_name:
                            csr_addr = addr
                            break
                    imm = csr_addr  # None if not found (that's ok)
                rs1 = normalize_reg(parts[2])
            elif len(parts) >= 2:
                rd = normalize_reg(parts[0])
                try:
                    imm = int(parts[1], 0)
                except ValueError:
                    imm = None
            return rd, rs1, rs2, imm, branch_target

        if mnemonic in ("csrr", "csrw"):
            if mnemonic == "csrr" and len(parts) >= 2:
                rd = normalize_reg(parts[0])
                try:
                    imm = int(parts[1], 0)
                except ValueError:
                    from arvis.analysis.instruction_usage import CSR_NAMES

                    csr_name = parts[1].strip()
                    csr_addr = None
                    for addr, name in CSR_NAMES.items():
                        if name == csr_name:
                            csr_addr = addr
                            break
                    imm = csr_addr
            elif mnemonic == "csrw" and len(parts) >= 2:
                try:
                    imm = int(parts[0], 0)
                except ValueError:
                    from arvis.analysis.instruction_usage import CSR_NAMES

                    csr_name = parts[0].strip()
                    csr_addr = None
                    for addr, name in CSR_NAMES.items():
                        if name == csr_name:
                            csr_addr = addr
                            break
                    imm = csr_addr
                rs1 = normalize_reg(parts[1])
            return rd, rs1, rs2, imm, branch_target

        # ── System ──
        if mnemonic in (
            "ecall",
            "ebreak",
            "mret",
            "sret",
            "wfi",
            "fence",
            "fence.i",
            "nop",
            "c.nop",
            "c.ebreak",
        ):
            return rd, rs1, rs2, imm, branch_target

        raise ValueError(f"Cannot parse operands for {mnemonic}: {operands}")

    def _parse_mem_operand(self, s: str) -> Tuple[Optional[str], Optional[int]]:
        m = re.match(r"(-?\d+)\((\w+)\)", s.strip())
        if m:
            return normalize_reg(m.group(2)), int(m.group(1))
        reg = normalize_reg(s)
        return (reg, 0) if reg else (None, None)

    def _parse_imm(self, s: str) -> Optional[int]:
        s = s.strip()
        try:
            if s.startswith("0x") or s.startswith("-0x"):
                return int(s, 16)
            return int(s)
        except (ValueError, TypeError):
            return None

    def _parse_target(self, s: str) -> Optional[int]:
        s = s.strip().split("<")[0].strip().split()[0] if s else ""
        try:
            return int(s, 16)
        except (ValueError, TypeError):
            return None
