# ARVIS benchmark Makefile.
# Builds two ELFs from the same source:
#   <name>_spike.elf   for the RISC-V Spike simulator (Linux ABI baseline)
#   <name>.elf         for the bare-metal CV32E40P + Verilator testbench
# The .hex output for Verilator is generated alongside the CV32E40P ELF.
#
# Override BENCHMARK and SOURCES from the command line, or by editing this
# file directly when adding a new benchmark.

BENCHMARK ?= minimal
SOURCES   ?= main.c

# RISC-V cross-compiler. The pipeline can override via PREFIX=.
CROSS_COMPILE ?= riscv32-unknown-elf-
ifdef PREFIX
CROSS_COMPILE := $(PREFIX)
endif

CC      = $(CROSS_COMPILE)gcc
OBJCOPY = $(CROSS_COMPILE)objcopy
SIZE    = $(CROSS_COMPILE)size

# RV32IMC matches the CV32E40P baseline configuration.
ARCH = rv32imc_zicsr
ABI  = ilp32

CFLAGS  = -march=$(ARCH) -mabi=$(ABI) -O3 -g
CFLAGS += -fno-builtin -fno-common -nostdlib -nostartfiles
CFLAGS += -Wall -Wextra
LDFLAGS = -static -Wl,--gc-sections

.PHONY: all clean
all: $(BENCHMARK)_spike.elf $(BENCHMARK).elf $(BENCHMARK).hex

# Spike build: uses the spike crt0 + linker script.
$(BENCHMARK)_spike.elf: $(SOURCES) crt0_spike.S link_spike.ld
	$(CC) $(CFLAGS) $(LDFLAGS) -T link_spike.ld -o $@ crt0_spike.S $(SOURCES)
	-$(SIZE) $@

# CV32E40P build: uses the bare-metal crt0 + linker script.
$(BENCHMARK).elf: $(SOURCES) crt0.S link.ld
	$(CC) $(CFLAGS) $(LDFLAGS) -T link.ld -o $@ crt0.S $(SOURCES)
	-$(SIZE) $@

# Verilog hex for the Verilator testbench.
$(BENCHMARK).hex: $(BENCHMARK).elf
	$(OBJCOPY) -O verilog $< $@

clean:
	rm -f *.elf *.hex *.s *.o
