;; === Pass 3: RR+RR 2-grams batch 3 — remaining (no variable immediates) ===
;; Total: 58 patterns (R4=58)
;; sra+srl RR+RR rev p3 s=0
(define_insn "fused_sra_srl_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (match_operand:SI 3 "register_operand" "r") (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 0, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+sra RR+RR p3 s=1
(define_insn "fused_sra_sra_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 0, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+sra RR+RR rev p3 s=2
(define_insn "fused_sra_sra_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (match_operand:SI 3 "register_operand" "r") (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 0, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+slt RR+RR p3 s=3
(define_insn "fused_sra_slt_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 0, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+slt RR+RR rev p3 s=4
(define_insn "fused_sra_slt_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (match_operand:SI 3 "register_operand" "r") (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 1, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+sltu RR+RR p3 s=5
(define_insn "fused_sra_sltu_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 1, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+sltu RR+RR rev p3 s=6
(define_insn "fused_sra_sltu_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (match_operand:SI 3 "register_operand" "r") (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 1, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+mul RR+RR p3 s=7
(define_insn "fused_sra_mul_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (mult:SI (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 1, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+add RR+RR p3 s=8
(define_insn "fused_slt_add_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (plus:SI (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 2, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+sub RR+RR p3 s=9
(define_insn "fused_slt_sub_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 2, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+sub RR+RR rev p3 s=10
(define_insn "fused_slt_sub_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (match_operand:SI 3 "register_operand" "r") (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 2, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+and RR+RR p3 s=11
(define_insn "fused_slt_and_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (and:SI (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 2, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+or RR+RR p3 s=12
(define_insn "fused_slt_or_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ior:SI (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 3, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+xor RR+RR p3 s=13
(define_insn "fused_slt_xor_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (xor:SI (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 3, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+sll RR+RR p3 s=14
(define_insn "fused_slt_sll_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 3, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+sll RR+RR rev p3 s=15
(define_insn "fused_slt_sll_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (match_operand:SI 3 "register_operand" "r") (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 3, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+srl RR+RR p3 s=16
(define_insn "fused_slt_srl_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 0, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+srl RR+RR rev p3 s=17
(define_insn "fused_slt_srl_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (match_operand:SI 3 "register_operand" "r") (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 0, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+sra RR+RR p3 s=18
(define_insn "fused_slt_sra_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 0, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+sra RR+RR rev p3 s=19
(define_insn "fused_slt_sra_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (match_operand:SI 3 "register_operand" "r") (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 0, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+slt RR+RR p3 s=20
(define_insn "fused_slt_slt_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 1, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+slt RR+RR rev p3 s=21
(define_insn "fused_slt_slt_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (match_operand:SI 3 "register_operand" "r") (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 1, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+sltu RR+RR p3 s=22
(define_insn "fused_slt_sltu_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 1, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+sltu RR+RR rev p3 s=23
(define_insn "fused_slt_sltu_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (match_operand:SI 3 "register_operand" "r") (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 1, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; slt+mul RR+RR p3 s=24
(define_insn "fused_slt_mul_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (mult:SI (lt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 2, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+add RR+RR p3 s=25
(define_insn "fused_sltu_add_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (plus:SI (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 2, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+sub RR+RR p3 s=26
(define_insn "fused_sltu_sub_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 2, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+sub RR+RR rev p3 s=27
(define_insn "fused_sltu_sub_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (match_operand:SI 3 "register_operand" "r") (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 2, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+and RR+RR p3 s=28
(define_insn "fused_sltu_and_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (and:SI (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 3, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+or RR+RR p3 s=29
(define_insn "fused_sltu_or_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ior:SI (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 3, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+xor RR+RR p3 s=30
(define_insn "fused_sltu_xor_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (xor:SI (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 3, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+sll RR+RR p3 s=31
(define_insn "fused_sltu_sll_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 3, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+sll RR+RR rev p3 s=32
(define_insn "fused_sltu_sll_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (match_operand:SI 3 "register_operand" "r") (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 0, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+srl RR+RR p3 s=33
(define_insn "fused_sltu_srl_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 0, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+srl RR+RR rev p3 s=34
(define_insn "fused_sltu_srl_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (match_operand:SI 3 "register_operand" "r") (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 0, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+sra RR+RR p3 s=35
(define_insn "fused_sltu_sra_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 0, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+sra RR+RR rev p3 s=36
(define_insn "fused_sltu_sra_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (match_operand:SI 3 "register_operand" "r") (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 1, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+slt RR+RR p3 s=37
(define_insn "fused_sltu_slt_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 1, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+slt RR+RR rev p3 s=38
(define_insn "fused_sltu_slt_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (match_operand:SI 3 "register_operand" "r") (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 1, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+sltu RR+RR p3 s=39
(define_insn "fused_sltu_sltu_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 1, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+sltu RR+RR rev p3 s=40
(define_insn "fused_sltu_sltu_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (match_operand:SI 3 "register_operand" "r") (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 2, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sltu+mul RR+RR p3 s=41
(define_insn "fused_sltu_mul_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (mult:SI (ltu:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 2, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+add RR+RR p3 s=42
(define_insn "fused_mul_add_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (plus:SI (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 2, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+sub RR+RR p3 s=43
(define_insn "fused_mul_sub_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 2, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+sub RR+RR rev p3 s=44
(define_insn "fused_mul_sub_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (match_operand:SI 3 "register_operand" "r") (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 3, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+and RR+RR p3 s=45
(define_insn "fused_mul_and_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (and:SI (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 3, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+or RR+RR p3 s=46
(define_insn "fused_mul_or_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ior:SI (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 3, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+xor RR+RR p3 s=47
(define_insn "fused_mul_xor_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (xor:SI (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 3, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+sll RR+RR p3 s=48
(define_insn "fused_mul_sll_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 0, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+sll RR+RR rev p3 s=49
(define_insn "fused_mul_sll_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (match_operand:SI 3 "register_operand" "r") (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 0, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+srl RR+RR p3 s=50
(define_insn "fused_mul_srl_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 0, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+srl RR+RR rev p3 s=51
(define_insn "fused_mul_srl_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (match_operand:SI 3 "register_operand" "r") (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 0, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+sra RR+RR p3 s=52
(define_insn "fused_mul_sra_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 1, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+sra RR+RR rev p3 s=53
(define_insn "fused_mul_sra_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (match_operand:SI 3 "register_operand" "r") (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 1, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+slt RR+RR p3 s=54
(define_insn "fused_mul_slt_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 1, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+slt RR+RR rev p3 s=55
(define_insn "fused_mul_slt_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (match_operand:SI 3 "register_operand" "r") (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 1, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+sltu RR+RR p3 s=56
(define_insn "fused_mul_sltu_rr_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 2, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; mul+sltu RR+RR rev p3 s=57
(define_insn "fused_mul_sltu_rr_rev_p3"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (match_operand:SI 3 "register_operand" "r") (mult:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 2, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

