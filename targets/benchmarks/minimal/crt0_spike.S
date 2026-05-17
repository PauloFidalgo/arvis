# Spike-compatible startup code (uses tohost for exit)
.section .text.start
.global _start
.type _start, @function

_start:
    la sp, _stack_end

    la t0, _bss_start
    la t1, _bss_end
1:  bge t0, t1, 2f
    sw zero, 0(t0)
    addi t0, t0, 4
    j 1b
2:
    call main

    la t0, tohost
    li t1, 1
    sw t1, 0(t0)

3:  j 3b

.size _start, .-_start

.section .tohost, "aw", @progbits
.global tohost
.global fromhost
.align 4
tohost:   .word 0
fromhost: .word 0