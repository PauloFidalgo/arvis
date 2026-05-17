# Verilator testbench for RISC-V cores
# Usage: make all CORE_ROOT=<path> FIRMWARE=<hex> [TRACE_LOG=<path>]

VERILATOR ?= verilator

# Verilator flags
VERILATOR_FLAGS = --cc --exe --build -j 0
VERILATOR_FLAGS += -Wno-fatal -Wno-WIDTH -Wno-CASEINCOMPLETE -Wno-UNOPTFLAT
VERILATOR_FLAGS += -Wno-MULTIDRIVEN -Wno-BLKANDNBLK
VERILATOR_FLAGS += --top-module tb_top

# Core paths (must be provided or use defaults)
CORE_ROOT ?= ../../..
RTL_DIR ?= $(CORE_ROOT)/rtl
BHV_DIR ?= $(CORE_ROOT)/bhv
TB_DIR ?= ..

# Firmware and trace paths
FIRMWARE ?=
TRACE_LOG ?= trace.log
MAX_CYCLES ?= 500000000

# Include directories
INCDIRS = +incdir+$(RTL_DIR)/include +incdir+$(BHV_DIR)/include

# RTL packages (must be compiled first)
RTL_PKGS = \
    $(RTL_DIR)/include/cv32e40p_apu_core_pkg.sv \
    $(RTL_DIR)/include/cv32e40p_fpu_pkg.sv \
    $(RTL_DIR)/include/cv32e40p_pkg.sv


# RTL source files
RTL_SRCS = \
    $(RTL_DIR)/cv32e40p_if_stage.sv \
    $(RTL_DIR)/cv32e40p_cs_registers.sv \
    $(RTL_DIR)/cv32e40p_register_file_ff.sv \
    $(RTL_DIR)/cv32e40p_load_store_unit.sv \
    $(RTL_DIR)/cv32e40p_id_stage.sv \
    $(RTL_DIR)/cv32e40p_aligner.sv \
    $(RTL_DIR)/cv32e40p_decoder.sv \
    $(RTL_DIR)/cv32e40p_compressed_decoder.sv \
    $(RTL_DIR)/cv32e40p_fifo.sv \
    $(RTL_DIR)/cv32e40p_prefetch_buffer.sv \
    $(RTL_DIR)/cv32e40p_prefetch_controller.sv \
    $(RTL_DIR)/cv32e40p_obi_interface.sv \
    $(RTL_DIR)/cv32e40p_alu.sv \
    $(RTL_DIR)/cv32e40p_alu_div.sv \
    $(RTL_DIR)/cv32e40p_ff_one.sv \
    $(RTL_DIR)/cv32e40p_popcnt.sv \
    $(RTL_DIR)/cv32e40p_mult.sv \
    $(RTL_DIR)/cv32e40p_int_controller.sv \
    $(RTL_DIR)/cv32e40p_ex_stage.sv \
    $(RTL_DIR)/cv32e40p_hwloop_regs.sv \
    $(RTL_DIR)/cv32e40p_controller.sv \
    $(RTL_DIR)/cv32e40p_sleep_unit.sv \
    $(RTL_DIR)/cv32e40p_core.sv \
    $(RTL_DIR)/cv32e40p_top.sv

# Testbench packages
TB_PKGS = $(TB_DIR)/include/perturbation_pkg.sv

# Testbench files
TB_SRCS = \
    $(BHV_DIR)/cv32e40p_sim_clock_gate.sv \
    $(TB_DIR)/dp_ram.sv \
    $(TB_DIR)/amo_shim.sv \
    $(TB_DIR)/riscv_gnt_stall.sv \
    $(TB_DIR)/riscv_rvalid_stall.sv \
    $(TB_DIR)/mm_ram.sv \
    cv32e40p_tb_subsystem.sv \
    tb_top_verilator.sv

# C++ testbench
CPP_SRCS = sim_main.cpp

# Output
OBJ_DIR = obj_dir
SIM = $(OBJ_DIR)/Vtb_top

# Kyber benchmark path (for convenience target)
KYBER_HEX ?= ../../../../../benchmarks/kyber512_rv32/kyber512_cv32e40p.hex

.PHONY: all clean run kyber help

all: $(SIM)

$(SIM): $(RTL_PKGS) $(RTL_SRCS) $(TB_PKGS) $(TB_SRCS) $(CPP_SRCS)
	$(VERILATOR) $(VERILATOR_FLAGS) $(INCDIRS) \
		$(RTL_PKGS) $(TB_PKGS) $(RTL_SRCS) $(TB_SRCS) $(CPP_SRCS)

run: $(SIM)
	@if [ -z "$(FIRMWARE)" ]; then \
		echo "ERROR: FIRMWARE not specified"; \
		echo "Usage: make run FIRMWARE=path/to/firmware.hex [TRACE_LOG=path/to/trace.log]"; \
		exit 1; \
	fi
	$(SIM) +firmware=$(FIRMWARE) +trace=$(TRACE_LOG) +maxcycles=$(MAX_CYCLES)

# Convenience target for Kyber benchmark
kyber: $(SIM)
	@echo "Running Kyber-512 benchmark..."
	$(SIM) +firmware=$(KYBER_HEX) +trace=$(TRACE_LOG) +maxcycles=$(MAX_CYCLES)

clean:
	rm -rf $(OBJ_DIR) *.vcd *.log

help:
	@echo "Verilator Testbench for RISC-V Cores"
	@echo ""
	@echo "Targets:"
	@echo "  all     - Build simulator"
	@echo "  run     - Run simulation (requires FIRMWARE)"
	@echo "  kyber   - Run Kyber-512 benchmark"
	@echo "  clean   - Remove build artifacts"
	@echo ""
	@echo "Variables:"
	@echo "  CORE_ROOT   - Path to core RTL root (default: ../../..)"
	@echo "  RTL_DIR     - Path to RTL sources (default: CORE_ROOT/rtl)"
	@echo "  BHV_DIR     - Path to behavioral models (default: CORE_ROOT/bhv)"
	@echo "  FIRMWARE    - Path to firmware .hex file (required for run)"
	@echo "  TRACE_LOG   - Path to trace output (default: trace.log)"
	@echo "  MAX_CYCLES  - Maximum simulation cycles (default: 500000000)"
	@echo ""
	@echo "Example:"
	@echo "  make run FIRMWARE=/path/to/test.hex TRACE_LOG=/tmp/trace.log"
