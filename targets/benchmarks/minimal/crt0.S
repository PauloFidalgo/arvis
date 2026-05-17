# CV32E40P startup code - signals PASS/FAIL based on main() return value
.section .text.start
.global _start
.type _start, @function

_start:
.option push
.option norelax
    la gp, __global_pointer$
.option pop
    la sp, _sp

    # Clear BSS
    la t0, _bss_start
    la t1, _bss_end
1:  bge t0, t1, 2f
    sw zero, 0(t0)
    addi t0, t0, 4
    j 1b
2:
    call main
    # a0 = return value from main (0 = success)

    # Signal test result to testbench
    li t0, 0x20000000
    bne a0, zero, 3f
    # PASS: write 123456789
    li t1, 123456789
    sw t1, 0(t0)
    j 4f
3:  # FAIL: write 1
    li t1, 1
    sw t1, 0(t0)
4:
    # Write exit value
    li t0, 0x20000004
    sw a0, 0(t0)

    # Halt
5:  wfi
    j 5b

.size _start, .-_start

.weak __global_pointer$
.section .sdata
__global_pointer$:
    .word 0