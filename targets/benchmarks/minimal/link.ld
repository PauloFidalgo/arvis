/* CV32E40P linker script - compatible with example_tb */
OUTPUT_ARCH(riscv)
ENTRY(_start)

MEMORY {
    RAM (rwxai) : ORIGIN = 0x00000000, LENGTH = 4M
}

/* Boot address expected by testbench */
__boot_address = 0x180;

SECTIONS {
    . = ORIGIN(RAM);
    
    /* Interrupt vectors at start */
    .vectors : {
        KEEP(*(.vectors))
    } > RAM
    
    /* Boot code at 0x180 */
    .init __boot_address : {
        KEEP(*(.text.start))
        KEEP(*(.init))
    } > RAM
    
    .text : {
        *(.text*)
    } > RAM
    
    .rodata : {
        *(.rodata*)
        *(.srodata*)
    } > RAM
    
    .data : {
        *(.data*)
        *(.sdata*)
    } > RAM
    
    .bss : {
        _bss_start = .;
        *(.bss*)
        *(.sbss*)
        *(COMMON)
        _bss_end = .;
    } > RAM
    
    . = ALIGN(16);
    _stack_start = .;
    . = . + 0x10000;  /* 64KB stack */
    _sp = .;
    _stack_end = .;
    
    _end = .;
}
