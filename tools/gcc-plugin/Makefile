# Makefile for fused_pass.so — GCC GIMPLE fusion plugin
#
# Build against a cross-compiler's plugin headers using the host GCC
# to produce the shared object.
#
# Usage:
#   make                                    # uses defaults
#   make GCC=/opt/riscv/bin/riscv32-unknown-elf-gcc HOST_GCC=gcc
#   make clean

# Cross-compiler whose plugin headers we use
GCC ?= riscv32-unknown-elf-gcc

# Host compiler that actually compiles the .so (must run on build machine)
HOST_GCC ?= gcc

# Detect plugin include directory from the cross-compiler
PLUGIN_DIR := $(shell $(GCC) -print-file-name=plugin 2>/dev/null)
PLUGIN_INC := $(PLUGIN_DIR)/include

# If cross-compiler not found, try host GCC's plugin dir
ifeq ($(wildcard $(PLUGIN_INC)/gcc-plugin.h),)
  PLUGIN_DIR := $(shell $(HOST_GCC) -print-file-name=plugin 2>/dev/null)
  PLUGIN_INC := $(PLUGIN_DIR)/include
endif

# Flags
CXXFLAGS := -shared -fPIC -fno-rtti -O2
CXXFLAGS += -Wall -Wextra -Wno-unused-parameter -Wno-narrowing
CXXFLAGS += -I$(PLUGIN_INC)

# On some systems GCC plugin headers need this
CXXFLAGS += -fno-exceptions

# Target
TARGET := fused_pass.so
SRC := fused_pass.c

# Installation
DESTDIR ?=
PREFIX ?= /opt/riscv
INSTALL_DIR := $(DESTDIR)$(PREFIX)/lib/gcc-plugin

.PHONY: all clean install

all: $(TARGET)

$(TARGET): $(SRC)
	@echo "  Building $(TARGET) with $(HOST_GCC)"
	@echo "  Plugin headers: $(PLUGIN_INC)"
	@test -f "$(PLUGIN_INC)/gcc-plugin.h" || \
		(echo "ERROR: gcc-plugin.h not found at $(PLUGIN_INC)"; exit 1)
	$(HOST_GCC) $(CXXFLAGS) -xc++ -o $@ $<

clean:
	rm -f $(TARGET)

install: $(TARGET)
	mkdir -p $(INSTALL_DIR)
	cp $(TARGET) $(INSTALL_DIR)/
	@echo "  Installed $(TARGET) to $(INSTALL_DIR)"