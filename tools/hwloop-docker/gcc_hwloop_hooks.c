/* Hardware loop support for CV32E40P-style zero-overhead loops.
   GCC's hw-doloop pass transforms eligible loops into countdown form:
     addi reg, reg, -1; bnez reg, .Ltop
   The post-patcher then converts this to hwloop.bounds + hwloop.count. */

static bool
riscv_can_use_doloop_p (const widest_int &, const widest_int &,
                        unsigned int loop_depth,
                        bool entered_at_top)
{
  if (!entered_at_top)
    return false;
  if (loop_depth > 1)
    return false;
  return TARGET_HWLOOP;
}

static const char *
riscv_invalid_within_doloop (const rtx_insn *insn)
{
  if (CALL_P (insn))
    return "function call in loop";
  if (tablejump_p (insn, NULL, NULL))
    return "tablejump in loop";
  return NULL;
}

#undef TARGET_CAN_USE_DOLOOP_P
#define TARGET_CAN_USE_DOLOOP_P riscv_can_use_doloop_p

#undef TARGET_INVALID_WITHIN_DOLOOP
#define TARGET_INVALID_WITHIN_DOLOOP riscv_invalid_within_doloop

