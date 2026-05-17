/*
 * hwloop_pass.c — GCC RTL pass plugin for late hardware loop insertion.
 *
 * Scans RTL for backward conditional branches (same approach as the
 * assembly patcher) and inserts CV32E40P hwloop setup instructions.
 * Runs after all optimizations to preserve code quality.
 *
 * Build:
 *   docker run --rm -v $(pwd):/work -w /work riscv-gcc-hwloop bash -c '
 *     GCC=/opt/riscv/bin/riscv32-unknown-elf-gcc
 *     PLUGIN_INC=$($GCC -print-file-name=plugin)/include
 *     g++ -shared -fPIC -fno-rtti -O2 -fno-exceptions -I$PLUGIN_INC \
 *         -Wno-unused-parameter -xc++ -o hwloop_pass.so hwloop_pass.c'
 */

#include "gcc-plugin.h"
#include "plugin-version.h"
#include "rtl.h"
#include "tree.h"
#include "tree-pass.h"
#include "context.h"
#include "basic-block.h"
#include "memmodel.h"
#include "cfgloop.h"
#include "df.h"
#include "regs.h"
#include "insn-config.h"
#include "recog.h"
#include "output.h"
#include "emit-rtl.h"
#include "cfgrtl.h"
#include "diagnostic-core.h"

#include <stdio.h>
#include <string.h>
#include <vector>

int plugin_is_GPL_compatible;

static int max_hw_loop = 1;
static int max_convert = 999;
static int only_convert = -1;
static int min_body_insns = 2;
static int max_body_insns = 200;
static bool debug_print = true;

#define DBG(...) do { if (debug_print) inform(UNKNOWN_LOCATION, __VA_ARGS__); } while(0)

/* --------------- Loop structure (from backward branch scan) --------------- */

struct rtl_loop {
    rtx_insn *target_label;     /* Loop start label */
    rtx_insn *back_branch;      /* Back-edge branch insn */
    basic_block branch_bb;      /* BB containing the back-edge */
    int target_uid;             /* UID of target label */
    int branch_uid;             /* UID of branch insn */

    /* Analysis results */
    unsigned int iv_reg;
    HOST_WIDE_INT step_val;
    rtx_insn *iv_step;          /* IV step insn */
    HOST_WIDE_INT count;
    bool count_is_const;
    unsigned int count_reg;
    bool needs_calc;
    unsigned int calc_init_reg;
    unsigned int calc_bound_reg;
    HOST_WIDE_INT calc_step;

    int body_insns;
    bool has_call;
    bool has_inner_back_branch;  /* Another backward branch inside this loop */
    bool eligible;
};

/* --------------- Helpers --------------- */

static bool
match_add_imm(rtx_insn *insn, unsigned int *reg, HOST_WIDE_INT *val)
{
    if (!NONJUMP_INSN_P(insn)) return false;
    rtx pat = PATTERN(insn);
    if (GET_CODE(pat) != SET) return false;
    rtx dst = SET_DEST(pat), src = SET_SRC(pat);
    if (!REG_P(dst)) return false;
    if (GET_CODE(src) == PLUS && REG_P(XEXP(src,0))
        && REGNO(XEXP(src,0)) == REGNO(dst) && CONST_INT_P(XEXP(src,1))) {
        *reg = REGNO(dst);
        *val = INTVAL(XEXP(src,1));
        return true;
    }
    return false;
}

/* Extract IV and bound from branch condition */
static bool
get_branch_cond(rtx_insn *branch, unsigned int *iv_reg,
                bool *bound_is_zero, unsigned int *bound_reg,
                HOST_WIDE_INT *bound_const, bool *bound_is_const_nonzero)
{
    rtx set = pc_set(branch);
    if (!set) return false;
    rtx ite = SET_SRC(set);
    if (GET_CODE(ite) != IF_THEN_ELSE) return false;
    rtx cond = XEXP(ite, 0);
    enum rtx_code code = GET_CODE(cond);
    if (code != NE && code != EQ && code != GT && code != GE && code != LT && code != LE
        && code != GTU && code != GEU && code != LTU && code != LEU)
        return false;
    rtx op0 = XEXP(cond, 0), op1 = XEXP(cond, 1);
    if (!REG_P(op0)) return false;
    *iv_reg = REGNO(op0);
    *bound_is_zero = false;
    *bound_is_const_nonzero = false;
    if (CONST_INT_P(op1)) {
        if (INTVAL(op1) == 0) { *bound_is_zero = true; }
        else { *bound_is_const_nonzero = true; *bound_const = INTVAL(op1); }
    } else if (REG_P(op1)) {
        *bound_reg = REGNO(op1);
    } else return false;
    return true;
}

/* --------------- Scan & Analyze --------------- */

static std::vector<rtl_loop>
find_loops(void)
{
    std::vector<rtl_loop> loops;

    /* Scan all BBs for backward conditional branches */
    basic_block bb;
    FOR_EACH_BB_FN(bb, cfun) {
        rtx_insn *insn;
        FOR_BB_INSNS(bb, insn) {
            if (!JUMP_P(insn)) continue;
            rtx set = pc_set(insn);
            if (!set) continue;
            rtx src = SET_SRC(set);
            if (GET_CODE(src) != IF_THEN_ELSE) continue; /* only conditional */

            rtx label_ref = XEXP(src, 1);
            if (GET_CODE(label_ref) != LABEL_REF) continue;
            rtx_insn *target = as_a<rtx_insn *>(XEXP(label_ref, 0));

            /* Backward branch? */
            if (INSN_UID(target) >= INSN_UID(insn)) continue;

            rtl_loop lp = {};
            lp.target_label = target;
            lp.back_branch = insn;
            lp.branch_bb = bb;
            lp.target_uid = INSN_UID(target);
            lp.branch_uid = INSN_UID(insn);
            loops.push_back(lp);
        }
    }
    return loops;
}

static bool
analyze_rtl_loop(rtl_loop &lp)
{
    /* Scan body: all insns between target_label and back_branch */
    lp.body_insns = 0;
    lp.has_call = false;
    lp.has_inner_back_branch = false;
    lp.iv_step = NULL;

    /* Extract IV from branch condition */
    bool bound_is_zero = false, bound_is_const_nz = false;
    unsigned int bound_reg = 0;
    HOST_WIDE_INT bound_const = 0;
    if (!get_branch_cond(lp.back_branch, &lp.iv_reg, &bound_is_zero,
                         &bound_reg, &bound_const, &bound_is_const_nz)) {
        DBG("hwloop: uid%d: can't extract branch cond", lp.branch_uid);
        return false;
    }

    /* Scan body */
    bool in_body = false;
    rtx_insn *insn;
    for (insn = NEXT_INSN(lp.target_label); insn && insn != lp.back_branch; insn = NEXT_INSN(insn)) {
        if (!NONDEBUG_INSN_P(insn) && !LABEL_P(insn)) continue;
        if (LABEL_P(insn)) continue; /* labels don't count */

        if (CALL_P(insn)) { lp.has_call = true; }
        if (JUMP_P(insn) && insn != lp.back_branch) {
            /* Check if this is another backward branch (inner loop) */
            rtx set2 = pc_set(insn);
            if (set2) {
                rtx src2 = SET_SRC(set2);
                rtx lr2 = NULL;
                if (GET_CODE(src2) == IF_THEN_ELSE) lr2 = XEXP(src2, 1);
                else if (GET_CODE(src2) == LABEL_REF) lr2 = src2;
                if (lr2 && GET_CODE(lr2) == LABEL_REF) {
                    rtx_insn *t2 = as_a<rtx_insn *>(XEXP(lr2, 0));
                    if (INSN_UID(t2) < INSN_UID(insn) && INSN_UID(t2) >= lp.target_uid)
                        lp.has_inner_back_branch = true;
                }
            }
        }

        /* Find IV step */
        unsigned int r; HOST_WIDE_INT v;
        if (match_add_imm(insn, &r, &v) && r == lp.iv_reg) {
            if (lp.iv_step) { /* multiple steps — take the last one */ }
            lp.iv_step = insn;
            lp.step_val = v;
        }

        lp.body_insns++;
    }

    if (lp.has_call) { DBG("hwloop: uid%d: has call", lp.branch_uid); return false; }
    if (lp.body_insns < min_body_insns) { DBG("hwloop: uid%d: too small (%d)", lp.branch_uid, lp.body_insns); return false; }
    if (lp.body_insns > max_body_insns) { DBG("hwloop: uid%d: too large (%d)", lp.branch_uid, lp.body_insns); return false; }
    if (!lp.iv_step) {
        /* Try swapping: maybe op1 is the IV and op0 is the bound */
        if (bound_reg) {
            unsigned int swap_iv = bound_reg;
            /* Scan body for step on the other operand */
            rtx_insn *scan;
            for (scan = NEXT_INSN(lp.target_label); scan && scan != lp.back_branch; scan = NEXT_INSN(scan)) {
                unsigned int r; HOST_WIDE_INT v;
                if (match_add_imm(scan, &r, &v) && r == swap_iv) {
                    lp.iv_step = scan;
                    lp.step_val = v;
                    /* Swap IV and bound */
                    bound_reg = lp.iv_reg;
                    lp.iv_reg = swap_iv;
                    DBG("hwloop: uid%d: swapped IV to r%d (step=%ld)", lp.branch_uid, swap_iv, (long)v);
                    break;
                }
            }
        }
        if (!lp.iv_step) {
            DBG("hwloop: uid%d: no IV step for r%d", lp.branch_uid, lp.iv_reg);
            return false;
        }
    }

    /* Find IV init: scan backwards from target_label (up to 30 insns) */
    bool found_init = false;
    HOST_WIDE_INT init_val = 0;
    bool init_is_const = false;
    unsigned int init_reg = 0;
    int scan_limit = 30;

    for (insn = PREV_INSN(lp.target_label); insn && scan_limit > 0; insn = PREV_INSN(insn)) {
        if (BARRIER_P(insn)) break;
        if (!NONDEBUG_INSN_P(insn)) continue;
        scan_limit--;
        rtx pat = PATTERN(insn);
        if (GET_CODE(pat) != SET) continue;
        rtx dst = SET_DEST(pat);
        if (!REG_P(dst) || REGNO(dst) != lp.iv_reg) continue;
        rtx src = SET_SRC(pat);
        if (CONST_INT_P(src)) { init_is_const = true; init_val = INTVAL(src); found_init = true; }
        else if (REG_P(src)) { init_is_const = false; init_reg = REGNO(src); found_init = true; }
        else if (GET_CODE(src) == PLUS && REG_P(XEXP(src,0)) && CONST_INT_P(XEXP(src,1))) {
            /* addi iv, base, offset — treat as register init */
            init_is_const = false; init_reg = REGNO(XEXP(src,0)); found_init = true;
        }
        break;
    }
    if (!found_init) { DBG("hwloop: uid%d: no IV init", lp.branch_uid); return false; }

    /* Compute count */
    HOST_WIDE_INT abs_step = lp.step_val < 0 ? -lp.step_val : lp.step_val;
    lp.needs_calc = false;
    lp.calc_step = abs_step;

    if (bound_is_zero && lp.step_val < 0) {
        if (init_is_const) {
            lp.count = init_val / abs_step;
            lp.count_is_const = true;
            lp.count_reg = lp.iv_reg;
        } else if (abs_step == 1) {
            lp.count_is_const = false;
            lp.count_reg = init_reg;
        } else {
            lp.needs_calc = true;
            lp.calc_init_reg = init_reg;
            lp.count_is_const = false;
        }
    } else if ((bound_is_zero || bound_is_const_nz || bound_reg) && lp.step_val > 0) {
        if (init_is_const && init_val == 0 && abs_step == 1 && !bound_is_zero) {
            lp.count_is_const = false;
            lp.count_reg = bound_reg;
        } else {
            lp.needs_calc = true;
            lp.calc_init_reg = init_is_const ? 0 : init_reg;
            lp.calc_bound_reg = bound_reg;
            lp.count_is_const = false;
        }
    } else if (!bound_is_zero && lp.step_val < 0) {
        lp.needs_calc = true;
        lp.calc_init_reg = init_is_const ? 0 : init_reg;
        lp.calc_bound_reg = bound_reg;
        lp.count_is_const = false;
    } else {
        DBG("hwloop: uid%d: unsupported pattern", lp.branch_uid);
        return false;
    }

    lp.eligible = true;
    return true;
}

/* --------------- Emission --------------- */

static int n_converted = 0;
static int loop_index = 0;

static void
convert_rtl_loop(rtl_loop &lp)
{
    if (n_converted >= max_convert) { loop_index++; return; }
    if (only_convert >= 0 && loop_index != only_convert) { loop_index++; return; }
    loop_index++;
    int loop_id = n_converted % max_hw_loop;

    /* Find first real insn after target label */
    rtx_insn *first_insn = NEXT_INSN(lp.target_label);
    while (first_insn && !NONDEBUG_INSN_P(first_insn) && first_insn != lp.back_branch)
        first_insn = NEXT_INSN(first_insn);
    if (!first_insn || first_insn == lp.back_branch) return;

    /* Create labels */
    rtx_code_label *start_label = gen_label_rtx();
    rtx_code_label *end_label = gen_label_rtx();

    /* Place start label before first body insn */
    emit_label_before(start_label, first_insn);

    /* Place end label after the last insn before back-edge */
    rtx_insn *last_body = PREV_INSN(lp.back_branch);
    while (last_body && !NONDEBUG_INSN_P(last_body) && last_body != lp.target_label)
        last_body = PREV_INSN(last_body);
    if (!last_body) return;
    /* Add NOP padding if loop body is too small (prefetch buffer needs >= 16 bytes) */
    if (lp.body_insns < 8) {
        int nops_needed = 8 - lp.body_insns;
        char nop_buf[64];
        for (int i = 0; i < nops_needed; i++) {
            rtx nop_asm = gen_rtx_ASM_OPERANDS(VOIDmode,
                ggc_strdup(".word 0x00000013  # hwloop pad NOP"), "", 0,
                rtvec_alloc(0), rtvec_alloc(0), rtvec_alloc(0), UNKNOWN_LOCATION);
            nop_asm = gen_rtx_PARALLEL(VOIDmode, gen_rtvec(1, nop_asm));
            emit_insn_after(nop_asm, last_body);
        }
        /* Update last_body to point to the last NOP */
        for (rtx_insn *p = NEXT_INSN(last_body); p; p = NEXT_INSN(p)) {
            if (NONDEBUG_INSN_P(p)) last_body = p;
            else break;
        }
    }

    /* Skip loops with body too small for prefetch buffer (need >= 16 bytes).
     * Allow adding 1 NOP (4 bytes) — if still too small, skip. */
    if (lp.body_insns < 3) {
        DBG("hwloop: uid%d: body too small (%d insns), skip", lp.branch_uid, lp.body_insns);
        return;
    }
    bool need_pad = (lp.body_insns < 5);  /* conservative: 5 insns * ~3 bytes avg >= 16 */

    emit_label_after(as_a<rtx_insn *>(end_label), last_body);

    if (need_pad) {
        rtx nop_asm = gen_rtx_ASM_OPERANDS(VOIDmode,
            ggc_strdup(".word 0x00000013  # hwloop pad"), "", 0,
            rtvec_alloc(0), rtvec_alloc(0), rtvec_alloc(0), UNKNOWN_LOCATION);
        nop_asm = gen_rtx_PARALLEL(VOIDmode, gen_rtvec(1, nop_asm));
        emit_insn_before(nop_asm, as_a<rtx_insn *>(end_label));
    }

    /* Determine count register */
    unsigned int count_reg = lp.count_is_const ? lp.iv_reg : lp.count_reg;
    /* TODO: for needs_calc, emit sub/shift instructions and set count_reg */
    if (lp.needs_calc) {
        /* Emit count calculation as inline asm before the loop.
         * Use the IV register as the count register (it will be overwritten
         * by the loop body anyway). */
        count_reg = lp.iv_reg;
        char calc_buf[256];

        if (lp.calc_bound_reg && lp.calc_init_reg && lp.calc_step > 1) {
            /* count = (bound - init) / step */
            int shift = 0;
            HOST_WIDE_INT s = lp.calc_step;
            while (s > 1 && (s & 1) == 0) { shift++; s >>= 1; }
            if (s == 1) {
                snprintf(calc_buf, sizeof(calc_buf),
                    "sub x%d, x%d, x%d\n\tsrli x%d, x%d, %d",
                    count_reg, lp.calc_bound_reg, lp.calc_init_reg,
                    count_reg, count_reg, shift);
            } else {
                DBG("hwloop: uid%d: non-power-of-2 step=%ld, skip", lp.branch_uid, (long)lp.calc_step);
                return;
            }
        } else if (lp.calc_bound_reg && lp.calc_step == 1 && lp.calc_init_reg == 0) {
            /* count = bound (init is zero reg) */
            count_reg = lp.calc_bound_reg;
        } else if (lp.calc_bound_reg && lp.calc_init_reg && lp.calc_step == 1) {
            /* count = bound - init */
            snprintf(calc_buf, sizeof(calc_buf),
                "sub x%d, x%d, x%d",
                count_reg, lp.calc_bound_reg, lp.calc_init_reg);
        } else if (lp.calc_bound_reg && !lp.calc_init_reg && lp.calc_step > 1) {
            /* count = bound / step (init=0) */
            int shift = 0;
            HOST_WIDE_INT s = lp.calc_step;
            while (s > 1 && (s & 1) == 0) { shift++; s >>= 1; }
            if (s == 1) {
                snprintf(calc_buf, sizeof(calc_buf),
                    "srli x%d, x%d, %d",
                    count_reg, lp.calc_bound_reg, shift);
            } else {
                DBG("hwloop: uid%d: non-power-of-2 step=%ld, skip", lp.branch_uid, (long)lp.calc_step);
                return;
            }
        } else if (lp.calc_init_reg && !lp.calc_bound_reg && lp.calc_step > 1) {
            /* countdown: count = init / step */
            int shift = 0;
            HOST_WIDE_INT s = lp.calc_step;
            while (s > 1 && (s & 1) == 0) { shift++; s >>= 1; }
            if (s == 1) {
                snprintf(calc_buf, sizeof(calc_buf),
                    "srli x%d, x%d, %d",
                    count_reg, lp.calc_init_reg, shift);
            } else {
                DBG("hwloop: uid%d: non-power-of-2 step=%ld, skip", lp.branch_uid, (long)lp.calc_step);
                return;
            }
        } else {
            DBG("hwloop: uid%d: unsupported calc: init_reg=%d bound_reg=%d step=%ld", lp.branch_uid, lp.calc_init_reg, lp.calc_bound_reg, (long)lp.calc_step);
            return;
        }

        rtx calc_asm = gen_rtx_ASM_OPERANDS(VOIDmode, ggc_strdup(calc_buf), "", 0, rtvec_alloc(0), rtvec_alloc(0), rtvec_alloc(0), UNKNOWN_LOCATION);
        calc_asm = gen_rtx_PARALLEL(VOIDmode, gen_rtvec(1, calc_asm));
        emit_insn_before(calc_asm, as_a<rtx_insn *>(start_label));
    }

    /* Emit hwloop setup as inline asm with label references */
    int sl = CODE_LABEL_NUMBER(start_label);
    int el = CODE_LABEL_NUMBER(end_label);
    unsigned int cr_enc = (count_reg & 0x1f) << 27 | (0x3 << 12) | 0x7b;

    char tmpl[512];
    snprintf(tmpl, sizeof(tmpl),
        ".p2align 2\n\t"
        ".option push\n\t"
        ".option norelax\n\t"
        ".word ((.L%d - .) >> 1) << 20 | (((.L%d - .) >> 1) >> 3) << 15 "
        "| (2 << 12) | (((.L%d - .) >> 1) & 7) << 9 | (%d << 7) | 0x7b\n\t"
        ".insn 0x%08x\n\t"
        ".option pop\n\t"
        ".p2align 2",
        el, sl, sl, loop_id, cr_enc);

    rtx body = gen_rtx_ASM_OPERANDS(VOIDmode, ggc_strdup(tmpl), "", 0, rtvec_alloc(0), rtvec_alloc(0), rtvec_alloc(0), UNKNOWN_LOCATION);
    body = gen_rtx_PARALLEL(VOIDmode, gen_rtvec(1, body));
    DBG("hwloop: emitting bounds before label .L%d", CODE_LABEL_NUMBER(start_label));
    emit_insn_before(body, as_a<rtx_insn *>(start_label));

    /* Replace back-edge branch with NOP to preserve fall-through */
    {
        rtx nop_rtx = gen_rtx_ASM_OPERANDS(VOIDmode,
            ggc_strdup("nop  # hwloop: back-edge removed"), "", 0,
            rtvec_alloc(0), rtvec_alloc(0), rtvec_alloc(0), UNKNOWN_LOCATION);
        nop_rtx = gen_rtx_PARALLEL(VOIDmode, gen_rtvec(1, nop_rtx));
        emit_insn_before(nop_rtx, lp.back_branch);
    }
    delete_insn(lp.back_branch);

    /* Delete IV step if IV is not live after the loop.
     * Use dataflow analysis to check liveness. */
    if (lp.iv_step) {
        basic_block latch_bb = BLOCK_FOR_INSN(lp.iv_step);
        if (latch_bb) {
            /* Check: is IV live out of the latch BB's successor (the exit edge)? */
            edge e;
            edge_iterator ei;
            bool iv_live_after = false;
            FOR_EACH_EDGE(e, ei, latch_bb->succs) {
                /* The exit edge (not back to header) */
                if (e->dest != BLOCK_FOR_INSN(lp.target_label)) {
                    bitmap live = df_get_live_in(e->dest);
                    if (live && bitmap_bit_p(live, lp.iv_reg))
                        iv_live_after = true;
                }
            }
            if (!iv_live_after) {
                rtx nop_iv = gen_rtx_ASM_OPERANDS(VOIDmode,
                    ggc_strdup("nop  # hwloop: IV step removed"), "", 0,
                    rtvec_alloc(0), rtvec_alloc(0), rtvec_alloc(0), UNKNOWN_LOCATION);
                nop_iv = gen_rtx_PARALLEL(VOIDmode, gen_rtvec(1, nop_iv));
                emit_insn_before(nop_iv, lp.iv_step);
                delete_insn(lp.iv_step);
                DBG("hwloop: uid%d: deleted IV step (r%d not live after loop)",
                    lp.branch_uid, lp.iv_reg);
            } else {
                DBG("hwloop: uid%d: kept IV step (r%d live after loop)",
                    lp.branch_uid, lp.iv_reg);
            }
        }
    }

    DBG("hwloop: uid%d [idx=%d]: count_reg=r%d, needs_calc=%d, count_is_const=%d, count=%ld", lp.branch_uid, loop_index-1, count_reg, lp.needs_calc, lp.count_is_const, (long)lp.count);
    n_converted++;
    DBG("hwloop: uid%d converted (id=%d, %d insns, IV=r%d, count=%s)",
        lp.branch_uid, loop_id, lp.body_insns, lp.iv_reg,
        lp.count_is_const ? "const" : "reg");
}

/* --------------- Pass --------------- */

static unsigned int
execute_hwloop_pass(void)
{
    // n_converted persists across functions for max_convert limit

    auto loops = find_loops();
    DBG("hwloop: %d backward branches in %s", (int)loops.size(), current_function_name());

    /* Mark inner loops (skip them for HW_LOOP=1) */
    for (size_t i = 0; i < loops.size(); i++) {
        for (size_t j = 0; j < loops.size(); j++) {
            if (i == j) continue;
            /* j is inside i if j's range is within i's range */
            if (loops[j].target_uid > loops[i].target_uid
                && loops[j].branch_uid < loops[i].branch_uid)
                loops[i].has_inner_back_branch = true;
        }
    }

    /* Analyze each loop */
    int n_eligible = 0;
    for (auto &lp : loops) {
        if (max_hw_loop == 1 && lp.has_inner_back_branch) {
            /* Skip outer loops when HW_LOOP=1 — can only do innermost */
            continue;
        }
        if (analyze_rtl_loop(lp)) {
            n_eligible++;
        }
    }

    /* Convert eligible loops (innermost first for nesting) */
    for (auto &lp : loops) {
        if (!lp.eligible) continue;
        convert_rtl_loop(lp);
    }

    if (n_converted > 0)
        inform(UNKNOWN_LOCATION, "hwloop_pass: %d/%d loops converted in %s",
               n_converted, n_eligible, current_function_name());
    return 0;
}

/* --------------- Registration --------------- */

namespace {
const pass_data pd = { RTL_PASS, "hwloop_pass", OPTGROUP_NONE, TV_NONE, 0,0,0,0,0 };
class hwloop_pass : public rtl_opt_pass {
public:
    hwloop_pass(gcc::context *c) : rtl_opt_pass(pd, c) {}
    bool gate(function *) final override { return max_hw_loop > 0; }
    unsigned int execute(function *) final override { return execute_hwloop_pass(); }
};
}

int plugin_init(struct plugin_name_args *pi, struct plugin_gcc_version *v)
{
    if (!plugin_default_version_check(v, &gcc_version)) return 1;
    for (int i = 0; i < pi->argc; i++) {
        if (!strcmp(pi->argv[i].key, "max_hw_loop"))
            max_hw_loop = atoi(pi->argv[i].value);
        if (!strcmp(pi->argv[i].key, "only_convert"))
            only_convert = atoi(pi->argv[i].value);
        if (!strcmp(pi->argv[i].key, "max_convert"))
            max_convert = atoi(pi->argv[i].value);
        if (!strcmp(pi->argv[i].key, "quiet"))
            debug_print = false;
    }
    inform(UNKNOWN_LOCATION, "hwloop_pass: max_hw_loop=%d", max_hw_loop);
    struct register_pass_info info;
    info.pass = new hwloop_pass(g);
    info.reference_pass_name = "mach";
    info.ref_pass_instance_number = 1;
    info.pos_op = PASS_POS_INSERT_AFTER;
    register_callback(pi->base_name, PLUGIN_PASS_MANAGER_SETUP, NULL, &info);
    return 0;
}
