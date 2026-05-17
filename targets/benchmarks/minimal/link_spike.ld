/* Spike-compatible linker script */
OUTPUT_ARCH("riscv")
ENTRY(_start)

MEMORY {
    RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 4M
}

SECTIONS {
    .text.start : { *(.text.start) } > RAM
    .text       : { *(.text*) } > RAM
    .rodata     : { *(.rodata*) } > RAM
    .data       : { *(.data*) } > RAM
    .sdata      : { *(.sdata*) } > RAM
    .tohost     : { *(.tohost) } > RAM
    .bss : {
        _bss_start = .;
        *(.bss*) *(COMMON)
        _bss_end = .;
    } > RAM
    .stack : {
        . = ALIGN(16);
        _stack_start = .;
        . = . + 0x10000;
        _stack_end = .;
    } > RAM
}