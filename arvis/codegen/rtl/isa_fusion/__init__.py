"""
ISA Fusion RTL generation.

Fuses instruction sequences into custom ALU operations.
"""

from arvis.analysis.registers import normalize_mnemonic as normalize_mnemonic

from .alu_single_cycle import IMM_FOLDS as IMM_FOLDS
from .alu_single_cycle import MNEMONIC_TO_SV as MNEMONIC_TO_SV
from .alu_single_cycle import EncodingAllocator as EncodingAllocator
from .alu_single_cycle import FusedOperation as FusedOperation
from .alu_single_cycle import RTLGenerator as RTLGenerator
from .alu_single_cycle import generate_intrinsic_header as generate_intrinsic_header
