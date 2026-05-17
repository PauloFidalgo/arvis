/*
 * fused_pass.c — GCC GIMPLE pass plugin for 3-gram instruction fusion.
 *
 * Reads a JSON config (patterns.json v2) describing 3-gram ALU patterns
 * discovered by the workload specialization tool.  Matches GIMPLE SSA
 * tree patterns and replaces them with inline asm `.insn` instructions.
 *
 * Key differences from v1:
 *   - Matches GIMPLE TREE structures (not linear stmt sequences)
 *   - Supports chain_edges for dependency topology (linear, fan-out)
 *   - Handles commutativity (swapped operands for commutative ops)
 *   - Supports hardcoded immediates (INTEGER_CST matching)
 *   - Uses gimple_codes from JSON (PLUS_EXPR, MINUS_EXPR, etc.)
 *
 * Fusion safety invariants:
 *   - Intermediate SSA defs are single-use
 *   - Intermediate values are NOT live-out of the basic block
 *   - The defining statements are in the same basic block
 *   - Replacement preserves control flow and memory ordering
 *
 * Build: make -C tools/gcc-plugin
 * Usage: riscv32-unknown-elf-gcc -fplugin=./fused_pass.so \
 *            -fplugin-arg-fused_pass-config=patterns.json ...
 */

#include "gcc-plugin.h"
#include "plugin-version.h"
#include "tree.h"
#include "gimple.h"
#include "gimple-iterator.h"
#include "tree-pass.h"
#include "context.h"
#include "basic-block.h"
#include "tree-ssa-operands.h"
#include "ssa.h"
#include "tree-cfg.h"
#include "gimple-pretty-print.h"
#include "diagnostic-core.h"
#include "intl.h"
#include "cgraph.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int plugin_is_GPL_compatible;

/* --------------- Constants --------------- */
#define MAX_PATTERNS     64
#define MAX_OPS          4
#define MAX_CHAIN_EDGES  8
#define MAX_HC_IMM       4
#define MAX_EXT_INPUTS   4

/* --------------- GIMPLE tree code resolution --------------- */

static enum tree_code
resolve_gimple_code(const char *name)
{
    if (!strcmp(name, "PLUS_EXPR"))     return PLUS_EXPR;
    if (!strcmp(name, "MINUS_EXPR"))    return MINUS_EXPR;
    if (!strcmp(name, "MULT_EXPR"))     return MULT_EXPR;
    if (!strcmp(name, "BIT_AND_EXPR"))  return BIT_AND_EXPR;
    if (!strcmp(name, "BIT_IOR_EXPR"))  return BIT_IOR_EXPR;
    if (!strcmp(name, "BIT_XOR_EXPR"))  return BIT_XOR_EXPR;
    if (!strcmp(name, "LSHIFT_EXPR"))   return LSHIFT_EXPR;
    if (!strcmp(name, "RSHIFT_EXPR"))   return RSHIFT_EXPR;
    if (!strcmp(name, "BIT_NOT_EXPR"))  return BIT_NOT_EXPR;
    if (!strcmp(name, "NEGATE_EXPR"))   return NEGATE_EXPR;
    return ERROR_MARK;
}

/* --------------- Pattern data structures --------------- */

struct chain_edge {
    int producer;       /* index of producing op */
    int consumer;       /* index of consuming op */
    int consumer_slot;  /* 0=rhs1, 1=rhs2 */
};

struct hardcoded_imm {
    int position;       /* which op in the chain */
    long value;         /* the constant value to match */
};

struct fusion_pattern {
    char name[64];
    int  n_ops;

    /* Per-op info */
    enum tree_code gimple_codes[MAX_OPS];
    int commutative[MAX_OPS];  /* 1 if op is commutative */

    /* Chain topology */
    int n_edges;
    struct chain_edge edges[MAX_CHAIN_EDGES];
    int is_linear;      /* 1 if simple 0->1->2 chain */

    /* Immediate handling */
    int n_hc_imm;
    struct hardcoded_imm hc_imm[MAX_HC_IMM];

    /* Encoding */
    unsigned opcode;
    unsigned funct3;
    unsigned funct7;
    int is_r4;          /* 1 if R4-type (3 reg inputs) */
    int n_reg_inputs;   /* 2 or 3 */

    int enabled;
};

static struct fusion_pattern patterns[MAX_PATTERNS];
static int num_patterns = 0;
static int stat_fused = 0, stat_checked = 0;

/* --------------- Minimal JSON parser --------------- */

static const char *skip_ws(const char *p)
{
    while (*p && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r'))
        p++;
    return p;
}

static const char *parse_str(const char *p, char *buf, int len)
{
    if (*p != '"') return p;
    p++;
    int i = 0;
    while (*p && *p != '"' && i < len - 1)
        buf[i++] = *p++;
    buf[i] = '\0';
    if (*p == '"') p++;
    return p;
}

static const char *parse_int_val(const char *p, long *v)
{
    *v = strtol(p, (char **)&p, 0);
    return p;
}

static const char *parse_bool(const char *p, int *v)
{
    if (!strncmp(p, "true", 4))  { *v = 1; return p + 4; }
    if (!strncmp(p, "false", 5)) { *v = 0; return p + 5; }
    *v = 0;
    return p;
}

/* Skip a JSON value (string, number, bool, array, object). */
static const char *skip_value(const char *p)
{
    p = skip_ws(p);
    if (*p == '"') {
        p++;
        while (*p && *p != '"') { if (*p == '\\') p++; p++; }
        if (*p == '"') p++;
    } else if (*p == '{') {
        int depth = 1; p++;
        while (*p && depth > 0) {
            if (*p == '{') depth++;
            else if (*p == '}') depth--;
            else if (*p == '"') { p++; while (*p && *p != '"') { if (*p == '\\') p++; p++; } }
            p++;
        }
    } else if (*p == '[') {
        int depth = 1; p++;
        while (*p && depth > 0) {
            if (*p == '[') depth++;
            else if (*p == ']') depth--;
            else if (*p == '"') { p++; while (*p && *p != '"') { if (*p == '\\') p++; p++; } }
            p++;
        }
    } else {
        /* number, bool, null */
        while (*p && *p != ',' && *p != '}' && *p != ']') p++;
    }
    return p;
}

/* Parse a single pattern from the v2 JSON format. */
static const char *
parse_pattern_v2(const char *p, struct fusion_pattern *pat)
{
    memset(pat, 0, sizeof(*pat));
    pat->enabled = 1;
    pat->opcode = 0x0b;
    pat->n_reg_inputs = 2;

    p = skip_ws(p);
    if (*p != '{') return p;
    p++;

    while (*p && *p != '}') {
        p = skip_ws(p);
        if (*p != '"') { p++; continue; }

        char key[64];
        p = parse_str(p, key, sizeof(key));
        p = skip_ws(p);
        if (*p == ':') p++;
        p = skip_ws(p);

        if (!strcmp(key, "name")) {
            p = parse_str(p, pat->name, sizeof(pat->name));
        }
        else if (!strcmp(key, "enabled")) {
            p = parse_bool(p, &pat->enabled);
        }
        else if (!strcmp(key, "gimple_codes")) {
            /* Array of strings: ["PLUS_EXPR", "MINUS_EXPR", ...] */
            if (*p == '[') {
                p++;
                pat->n_ops = 0;
                while (*p && *p != ']') {
                    p = skip_ws(p);
                    if (*p == '"') {
                        char code_name[32];
                        p = parse_str(p, code_name, sizeof(code_name));
                        if (pat->n_ops < MAX_OPS)
                            pat->gimple_codes[pat->n_ops++] =
                                resolve_gimple_code(code_name);
                    }
                    p = skip_ws(p);
                    if (*p == ',') p++;
                }
                if (*p == ']') p++;
            }
        }
        else if (!strcmp(key, "commutativity")) {
            /* Array of bools: [true, false, true] */
            if (*p == '[') {
                p++;
                int idx = 0;
                while (*p && *p != ']') {
                    p = skip_ws(p);
                    int val = 0;
                    p = parse_bool(p, &val);
                    if (idx < MAX_OPS)
                        pat->commutative[idx++] = val;
                    p = skip_ws(p);
                    if (*p == ',') p++;
                }
                if (*p == ']') p++;
            }
        }
        else if (!strcmp(key, "chain_edges")) {
            /* Array of objects: [{"producer":0,"consumer":1,"consumer_slot":0}] */
            if (*p == '[') {
                p++;
                pat->n_edges = 0;
                while (*p && *p != ']') {
                    p = skip_ws(p);
                    if (*p == '{') {
                        p++;
                        struct chain_edge *e = &pat->edges[pat->n_edges];
                        while (*p && *p != '}') {
                            p = skip_ws(p);
                            char ek[32];
                            if (*p == '"') {
                                p = parse_str(p, ek, sizeof(ek));
                                p = skip_ws(p);
                                if (*p == ':') p++;
                                p = skip_ws(p);
                                long v;
                                p = parse_int_val(p, &v);
                                if (!strcmp(ek, "producer")) e->producer = (int)v;
                                else if (!strcmp(ek, "consumer")) e->consumer = (int)v;
                                else if (!strcmp(ek, "consumer_slot")) e->consumer_slot = (int)v;
                            }
                            p = skip_ws(p);
                            if (*p == ',') p++;
                        }
                        if (*p == '}') p++;
                        if (pat->n_edges < MAX_CHAIN_EDGES)
                            pat->n_edges++;
                    }
                    p = skip_ws(p);
                    if (*p == ',') p++;
                }
                if (*p == ']') p++;
            }
        }
        else if (!strcmp(key, "chain_topology")) {
            /* Object with is_linear, has_fan_out, has_register_reuse */
            if (*p == '{') {
                p++;
                while (*p && *p != '}') {
                    p = skip_ws(p);
                    char tk[32];
                    if (*p == '"') {
                        p = parse_str(p, tk, sizeof(tk));
                        p = skip_ws(p);
                        if (*p == ':') p++;
                        p = skip_ws(p);
                        if (!strcmp(tk, "is_linear"))
                            p = parse_bool(p, &pat->is_linear);
                        else
                            p = skip_value(p);
                    }
                    p = skip_ws(p);
                    if (*p == ',') p++;
                }
                if (*p == '}') p++;
            }
        }
        else if (!strcmp(key, "immediates")) {
            /* Object with hardcoded: {"0": 16} */
            if (*p == '{') {
                p++;
                while (*p && *p != '}') {
                    p = skip_ws(p);
                    char ik[32];
                    if (*p == '"') {
                        p = parse_str(p, ik, sizeof(ik));
                        p = skip_ws(p);
                        if (*p == ':') p++;
                        p = skip_ws(p);
                        if (!strcmp(ik, "hardcoded")) {
                            if (*p == '{') {
                                p++;
                                pat->n_hc_imm = 0;
                                while (*p && *p != '}') {
                                    p = skip_ws(p);
                                    char pos_str[8];
                                    if (*p == '"') {
                                        p = parse_str(p, pos_str, sizeof(pos_str));
                                        p = skip_ws(p);
                                        if (*p == ':') p++;
                                        p = skip_ws(p);
                                        long val;
                                        p = parse_int_val(p, &val);
                                        if (pat->n_hc_imm < MAX_HC_IMM) {
                                            pat->hc_imm[pat->n_hc_imm].position = atoi(pos_str);
                                            pat->hc_imm[pat->n_hc_imm].value = val;
                                            pat->n_hc_imm++;
                                        }
                                    }
                                    p = skip_ws(p);
                                    if (*p == ',') p++;
                                }
                                if (*p == '}') p++;
                            } else {
                                p = skip_value(p);
                            }
                        } else {
                            p = skip_value(p);
                        }
                    }
                    p = skip_ws(p);
                    if (*p == ',') p++;
                }
                if (*p == '}') p++;
            }
        }
        else if (!strcmp(key, "encoding")) {
            /* Object with opcode, funct3, funct7, is_r4, n_reg_inputs */
            if (*p == '{') {
                p++;
                while (*p && *p != '}') {
                    p = skip_ws(p);
                    char ek[32];
                    if (*p == '"') {
                        p = parse_str(p, ek, sizeof(ek));
                        p = skip_ws(p);
                        if (*p == ':') p++;
                        p = skip_ws(p);
                        if (!strcmp(ek, "opcode")) {
                            long v; p = parse_int_val(p, &v);
                            pat->opcode = (unsigned)v;
                        } else if (!strcmp(ek, "funct3")) {
                            long v; p = parse_int_val(p, &v);
                            pat->funct3 = (unsigned)v;
                        } else if (!strcmp(ek, "funct7")) {
                            long v; p = parse_int_val(p, &v);
                            pat->funct7 = (unsigned)v;
                        } else if (!strcmp(ek, "is_r4")) {
                            p = parse_bool(p, &pat->is_r4);
                        } else if (!strcmp(ek, "n_reg_inputs")) {
                            long v; p = parse_int_val(p, &v);
                            pat->n_reg_inputs = (int)v;
                        } else {
                            p = skip_value(p);
                        }
                    }
                    p = skip_ws(p);
                    if (*p == ',') p++;
                }
                if (*p == '}') p++;
            }
        }
        else {
            /* Skip unknown keys */
            p = skip_value(p);
        }

        p = skip_ws(p);
        if (*p == ',') p++;
    }
    if (*p == '}') p++;
    return p;
}

static void load_config(const char *filename)
{
    FILE *f = fopen(filename, "r");
    if (!f) {
        warning(0, "fused_pass: cannot open config %qs", filename);
        return;
    }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = (char *)xmalloc(sz + 1);
    if ((long)fread(buf, 1, sz, f) != sz) {
        warning(0, "fused_pass: short read");
        free(buf);
        fclose(f);
        return;
    }
    buf[sz] = '\0';
    fclose(f);

    const char *p = strstr(buf, "\"patterns\"");
    if (!p) { free(buf); return; }
    p += strlen("\"patterns\"");
    p = skip_ws(p);
    if (*p == ':') p++;
    p = skip_ws(p);
    if (*p != '[') { free(buf); return; }
    p++;

    num_patterns = 0;
    while (*p && *p != ']' && num_patterns < MAX_PATTERNS) {
        p = skip_ws(p);
        if (*p == '{') {
            struct fusion_pattern pat;
            p = parse_pattern_v2(p, &pat);
            if (pat.enabled && pat.n_ops >= 2)
                patterns[num_patterns++] = pat;
        }
        p = skip_ws(p);
        if (*p == ',') p++;
    }
    free(buf);
}

/* --------------- SSA helpers --------------- */

static int count_uses(tree name)
{
    if (!name || TREE_CODE(name) != SSA_NAME) return 0;
    int n = 0;
    use_operand_p use_p;
    imm_use_iterator iter;
    FOR_EACH_IMM_USE_FAST(use_p, iter, name) {
        if (!is_gimple_debug(USE_STMT(use_p))) n++;
    }
    return n;
}

static bool live_out_of_bb(tree name, basic_block bb)
{
    if (!name || TREE_CODE(name) != SSA_NAME) return false;
    use_operand_p use_p;
    imm_use_iterator iter;
    FOR_EACH_IMM_USE_FAST(use_p, iter, name) {
        gimple *s = USE_STMT(use_p);
        if (!is_gimple_debug(s) && gimple_bb(s) != bb)
            return true;
    }
    return false;
}

/* --------------- Tree-walking pattern matcher --------------- */

/*
 * Match a 3-gram pattern by walking the GIMPLE SSA def-use chain
 * UPWARD from a candidate root statement.
 *
 * For a linear 3-gram pattern [op0, op1, op2]:
 *   stmt2 = op2(_, _)       ← root (what we start from)
 *   One operand of stmt2 is defined by:
 *     stmt1 = op1(_, _)     ← one level up
 *   One operand of stmt1 is defined by:
 *     stmt0 = op0(_, _)     ← two levels up
 *
 * The matcher walks UP the def chain, checking:
 *   1. Each stmt's tree_code matches the pattern's gimple_code
 *   2. The chain edges connect at the correct operand slots
 *   3. For commutative ops, both operand orderings are tried
 *   4. Intermediate defs are single-use and not live-out
 *   5. Hardcoded immediates match (if specified)
 *
 * Returns the number of external inputs found (0 = no match).
 */
static int
try_match_tree(gimple *root_stmt,
               const struct fusion_pattern *pat,
               basic_block bb,
               tree ext_inputs[], int max_ext,
               tree *result_lhs,
               gimple *matched_stmts[],
               long var_const_out[], int *n_var_const_out)
{
    int n = pat->n_ops;
    if (n < 2 || n > MAX_OPS) return 0;

    /* The root statement is the LAST op in the chain (highest index). */
    if (gimple_code(root_stmt) != GIMPLE_ASSIGN) return 0;
    if (gimple_assign_rhs_code(root_stmt) != pat->gimple_codes[n - 1])
        return 0;

    if (dump_file)
        fprintf(dump_file, "  try_match '%s': root code matches %d\n",
                pat->name, pat->gimple_codes[n - 1]);

    matched_stmts[n - 1] = root_stmt;

    /* Walk UP the chain using chain_edges to find earlier ops.
     * For each edge (producer->consumer at slot), find the defining
     * stmt of consumer's operand at that slot. */

    /* Initialize: only root is known. */
    for (int i = 0; i < n - 1; i++)
        matched_stmts[i] = NULL;

    /* Process edges in reverse consumer order (from last to first). */
    for (int e = pat->n_edges - 1; e >= 0; e--) {
        int ci = pat->edges[e].consumer;
        int pi = pat->edges[e].producer;
        int slot = pat->edges[e].consumer_slot;

        if (ci >= n || pi >= n) return 0;
        if (!matched_stmts[ci]) return 0;

        /* Get the operand at the specified slot of the consumer stmt. */
        gimple *consumer_stmt = matched_stmts[ci];
        /* In GIMPLE, gimple_op(s, 0) is LHS, gimple_op(s, 1) is rhs1,
         * gimple_op(s, 2) is rhs2. So slot 0 → op index 1, slot 1 → op index 2. */
        int op_idx = slot + 1;

        /* For commutative ops, try the other slot too. */
        tree operand = NULL;
        int try_slots[2] = {op_idx, -1};
        int n_try = 1;
        if (pat->commutative[ci] && gimple_num_ops(consumer_stmt) > 2) {
            try_slots[1] = (op_idx == 1) ? 2 : 1;
            n_try = 2;
        }

        gimple *producer_stmt = NULL;
        for (int t = 0; t < n_try && !producer_stmt; t++) {
            if (try_slots[t] < 0) continue;
            if (try_slots[t] >= (int)gimple_num_ops(consumer_stmt)) continue;

            operand = gimple_op(consumer_stmt, try_slots[t]);
            if (!operand || TREE_CODE(operand) != SSA_NAME) continue;

            /* Follow the def chain. */
            gimple *def = SSA_NAME_DEF_STMT(operand);
            if (!def || gimple_code(def) != GIMPLE_ASSIGN) continue;
            if (gimple_bb(def) != bb) continue;

            /* Check opcode matches. */
            if (gimple_assign_rhs_code(def) != pat->gimple_codes[pi])
                continue;

            /* Safety: intermediate must be single-use and not live-out. */
            if (count_uses(operand) != 1) continue;
            if (live_out_of_bb(operand, bb)) continue;

            producer_stmt = def;
        }

        if (!producer_stmt) {
            if (dump_file)
                fprintf(dump_file, "    edge %d->%d: no producer found\n",
                        pat->edges[e].producer, pat->edges[e].consumer);
            return 0;
        }
        matched_stmts[pi] = producer_stmt;
        if (dump_file)
            fprintf(dump_file, "    edge %d->%d: matched!\n",
                    pat->edges[e].producer, pat->edges[e].consumer);
    }

    /* Verify all ops were found. */
    for (int i = 0; i < n; i++) {
        if (!matched_stmts[i]) return 0;
    }

    /* Check hardcoded immediates. */
    for (int h = 0; h < pat->n_hc_imm; h++) {
        int pos = pat->hc_imm[h].position;
        long expected = pat->hc_imm[h].value;
        if (pos < 0 || pos >= n) return 0;

        gimple *s = matched_stmts[pos];
        /* Look for an INTEGER_CST operand matching the expected value. */
        bool found_imm = false;
        int nops = gimple_num_ops(s);
        for (int op = 1; op < nops; op++) {
            tree arg = gimple_op(s, op);
            if (arg && TREE_CODE(arg) == INTEGER_CST) {
                if ((long)TREE_INT_CST_LOW(arg) == expected) {
                    found_imm = true;
                    break;
                }
            }
        }
        if (!found_imm) return 0;
    }

    /* Collect external inputs and variable constants.
     * Register inputs go to ext_inputs[] with "r" constraint.
     * Variable constants (INTEGER_CST not in HC list) are encoded
     * directly as x<value> in the asm template — no register needed. */
    int n_ext = 0;
    /* Track variable constants: up to 2 (rs2 + rs3 fields) */
    int n_var_const = 0;

    tree chain_defs[MAX_OPS];
    for (int i = 0; i < n; i++)
        chain_defs[i] = gimple_assign_lhs(matched_stmts[i]);

    for (int i = 0; i < n; i++) {
        gimple *s = matched_stmts[i];
        int nops = gimple_num_ops(s);
        for (int op = 1; op < nops; op++) {
            tree arg = gimple_op(s, op);
            if (!arg) continue;

            if (TREE_CODE(arg) == INTEGER_CST) {
                /* Check if this constant is in the hardcoded list */
                long val = (long)TREE_INT_CST_LOW(arg);
                bool is_hc = false;
                for (int h = 0; h < pat->n_hc_imm; h++) {
                    if (pat->hc_imm[h].value == val) {
                        is_hc = true; break;
                    }
                }
                if (!is_hc && n_var_const < 2) {
                    /* Variable constant — encode as x<value> in asm */
                    var_const_out[n_var_const++] = val;
                }
                continue;
            }
            if (TREE_CODE(arg) != SSA_NAME) continue;

            /* Is this produced inside the chain? */
            bool is_chain = false;
            for (int d = 0; d < n; d++) {
                if (chain_defs[d] == arg) { is_chain = true; break; }
            }
            if (is_chain) continue;

            /* External input — avoid duplicates. */
            bool dup = false;
            for (int e = 0; e < n_ext; e++)
                if (ext_inputs[e] == arg) { dup = true; break; }
            if (!dup && n_ext < max_ext)
                ext_inputs[n_ext++] = arg;
        }
    }

    /* Verify we don't exceed the encoding's register capacity. */
    if (n_ext > pat->n_reg_inputs) return 0;

    *n_var_const_out = n_var_const;
    *result_lhs = gimple_assign_lhs(matched_stmts[n - 1]);
    return n_ext;
}

/* --------------- Replacement --------------- */

static void
replace_chain_v2(gimple *matched_stmts[], int n_ops,
                 const struct fusion_pattern *pat,
                 tree ext_inputs[], int n_ext, tree result_lhs,
                 basic_block bb,
                 long var_const_values[], int n_var_const)
{
    char asm_template[256];

    /* Build asm template. Variable constants are encoded as x<value>
     * directly in the rs2/rs3 fields — the decoder reads these bits
     * as immediate values via fused_imm_i, NOT as register addresses. */
    char rs2_str[16] = "zero";
    char rs3_str[16] = "zero";

    /* Fill rs2/rs3 with variable constants for unused register slots.
     * n_ext==0: rs2 + rs3 available for var_consts
     * n_ext==1: %1 is rs1, rs2 available for var_const
     * n_ext>=2: %1 is rs1, %2 is rs2, no room for var_consts in rs2 */
    if (n_var_const > 0 && n_ext <= 1) {
        snprintf(rs2_str, sizeof(rs2_str), "x%ld", var_const_values[0] & 0x1F);
        if (n_var_const > 1 && n_ext == 0)
            snprintf(rs3_str, sizeof(rs3_str), "x%ld", var_const_values[1] & 0x1F);
    }

    if (pat->is_r4) {
        unsigned funct2 = pat->funct7 & 0x03;
        if (n_ext >= 3) {
            snprintf(asm_template, sizeof(asm_template),
                     ".insn r4 0x%02x, %u, %u, %%0, %%1, %%2, %%3",
                     pat->opcode, pat->funct3, funct2);
        } else if (n_ext == 2) {
            snprintf(asm_template, sizeof(asm_template),
                     ".insn r4 0x%02x, %u, %u, %%0, %%1, %%2, %s",
                     pat->opcode, pat->funct3, funct2, rs3_str);
        } else if (n_ext == 1) {
            snprintf(asm_template, sizeof(asm_template),
                     ".insn r4 0x%02x, %u, %u, %%0, %%1, %s, %s",
                     pat->opcode, pat->funct3, funct2, rs2_str, rs3_str);
        } else {
            snprintf(asm_template, sizeof(asm_template),
                     ".insn r4 0x%02x, %u, %u, %%0, %%1, %s, %s",
                     pat->opcode, pat->funct3, funct2, rs2_str, rs3_str);
        }
    } else {
        if (n_ext >= 2) {
            snprintf(asm_template, sizeof(asm_template),
                     ".insn r 0x%02x, %u, 0x%02x, %%0, %%1, %%2",
                     pat->opcode, pat->funct3, pat->funct7);
        } else if (n_ext == 1) {
            snprintf(asm_template, sizeof(asm_template),
                     ".insn r 0x%02x, %u, 0x%02x, %%0, %%1, %s",
                     pat->opcode, pat->funct3, pat->funct7, rs2_str);
        } else {
            /* All constants — only rs1 is a register input */
            snprintf(asm_template, sizeof(asm_template),
                     ".insn r 0x%02x, %u, 0x%02x, %%0, %%1, %s",
                     pat->opcode, pat->funct3, pat->funct7, rs2_str);
        }
    }

    /* For patterns with hardcoded immediates where variable immediates
     * are encoded as register numbers (not register values), the plugin
     * emits register x<N> where N is the immediate value. The decoder
     * reads instr[24:20] directly as the immediate, NOT as a register
     * address. This avoids wasting a li instruction. */

    /* Output: "=r" constraint on result_lhs. */
    tree out_constraint = build_string(2, "=r");
    TREE_TYPE(out_constraint) = char_type_node;
    tree out_entry = tree_cons(
        tree_cons(NULL_TREE, out_constraint, NULL_TREE),
        result_lhs, NULL_TREE);

    vec<tree, va_gc> *v_outputs = NULL;
    vec_safe_push(v_outputs, out_entry);

    /* Inputs: each ext_input with "r" constraint. */
    vec<tree, va_gc> *v_inputs = NULL;
    for (int i = 0; i < n_ext; i++) {
        tree in_constraint = build_string(1, "r");
        TREE_TYPE(in_constraint) = char_type_node;
        tree in_entry = tree_cons(
            tree_cons(NULL_TREE, in_constraint, NULL_TREE),
            ext_inputs[i], NULL_TREE);
        vec_safe_push(v_inputs, in_entry);
    }

    gasm *asm_stmt = gimple_build_asm_vec(
        asm_template, v_inputs, v_outputs, NULL, NULL);
    gimple_asm_set_volatile(asm_stmt, true);

    /* Remove all intermediate stmts (all except the root/last). */
    for (int i = 0; i < n_ops - 1; i++) {
        gimple_stmt_iterator gsi = gsi_for_stmt(matched_stmts[i]);
        gsi_remove(&gsi, true);
    }

    /* Replace the root stmt with the asm. */
    gimple_stmt_iterator gsi = gsi_for_stmt(matched_stmts[n_ops - 1]);
    gsi_replace(&gsi, asm_stmt, true);

    stat_fused++;
}

/* --------------- GIMPLE pass execution --------------- */

static unsigned int execute_fused_pass(void)
{
    basic_block bb;

    if (num_patterns == 0) return 0;

    FOR_EACH_BB_FN(bb, cfun) {
        /* Scan every GIMPLE_ASSIGN in this BB as a potential root. */
        for (gimple_stmt_iterator gsi = gsi_start_bb(bb);
             !gsi_end_p(gsi); gsi_next(&gsi))
        {
            gimple *stmt = gsi_stmt(gsi);
            if (gimple_code(stmt) != GIMPLE_ASSIGN)
                continue;

            stat_checked++;
            enum tree_code root_code = gimple_assign_rhs_code(stmt);

            /* Try each pattern against this stmt as root. */
            for (int p = 0; p < num_patterns; p++) {
                /* Quick check: root opcode must match last pattern op */
                if (root_code != patterns[p].gimple_codes[patterns[p].n_ops - 1])
                    continue;
                tree ext_inputs[MAX_EXT_INPUTS] = {NULL};
                tree result_lhs = NULL;
                gimple *matched[MAX_OPS] = {NULL};

                long var_consts[2] = {0, 0};
                int n_var_const = 0;

                int n_ext = try_match_tree(
                    stmt, &patterns[p], bb,
                    ext_inputs, MAX_EXT_INPUTS,
                    &result_lhs, matched,
                    var_consts, &n_var_const);

                if (n_ext < 0 || !result_lhs)
                    continue;
                /* n_ext == 0 is valid: all-constant pattern with only HC imms */

                /* Match found — apply fusion. */
                replace_chain_v2(matched, patterns[p].n_ops,
                                 &patterns[p], ext_inputs, n_ext,
                                 result_lhs, bb,
                                 var_consts, n_var_const);

                if (dump_file)
                    fprintf(dump_file,
                            "fused_pass: applied '%s' (%d ops) in BB%d\n",
                            patterns[p].name, patterns[p].n_ops,
                            bb->index);

                /* Restart scanning this BB since stmts changed. */
                gsi = gsi_start_bb(bb);
                break;  /* Re-scan from start of BB */
            }
        }
    }

    if (dump_file)
        fprintf(dump_file,
                "fused_pass: %d fusions applied, %d candidates checked\n",
                stat_fused, stat_checked);

    return 0;
}

/* --------------- Pass definition --------------- */

namespace {

const pass_data fused_pass_data = {
    GIMPLE_PASS,
    "fused_pass",
    OPTGROUP_NONE,
    TV_NONE,
    PROP_ssa,
    0, 0, 0,
    TODO_update_ssa,
};

class fused_pass : public gimple_opt_pass
{
public:
    fused_pass(gcc::context *ctxt)
        : gimple_opt_pass(fused_pass_data, ctxt) {}

    bool gate(function *) final override
    { return num_patterns > 0; }

    unsigned int execute(function *) final override
    { return execute_fused_pass(); }
};

} /* anonymous namespace */

/* --------------- Plugin init --------------- */

static const char *config_file = NULL;

static void
plugin_parse_args(struct plugin_name_args *plugin_info)
{
    for (int i = 0; i < plugin_info->argc; i++) {
        if (!strcmp(plugin_info->argv[i].key, "config"))
            config_file = plugin_info->argv[i].value;
    }
}

int
plugin_init(struct plugin_name_args *plugin_info,
            struct plugin_gcc_version *version)
{
    if (!plugin_default_version_check(version, &gcc_version)) {
        error("fused_pass: incompatible GCC version");
        return 1;
    }

    plugin_parse_args(plugin_info);

    if (config_file)
        load_config(config_file);

    if (num_patterns > 0)
        inform(UNKNOWN_LOCATION,
               "fused_pass: loaded %d 3-gram fusion patterns from %qs",
               num_patterns, config_file);

    /* Register AFTER all SSA optimizations — GCC does its work first,
     * then we fuse 3-deep trees. This ensures GCC's constant folding,
     * CSE, loop unrolling, etc. are not disturbed by the fusion. */
    struct register_pass_info pass_info;
    pass_info.pass = new fused_pass(g);
    pass_info.reference_pass_name = "optimized";
    pass_info.ref_pass_instance_number = 1;
    pass_info.pos_op = PASS_POS_INSERT_AFTER;

    register_callback(plugin_info->base_name,
                      PLUGIN_PASS_MANAGER_SETUP,
                      NULL, &pass_info);

    return 0;
}
