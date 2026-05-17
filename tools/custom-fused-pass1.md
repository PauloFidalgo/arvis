;; === Pass 1: RR+RR 2-grams batch 1 (no variable immediates) ===
;; Total: 64 patterns (R4=64)
;; add+add RR+RR p1 s=0
(define_insn "fused_add_add_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (plus:SI (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 0, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+sub RR+RR p1 s=1
(define_insn "fused_add_sub_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 0, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+sub RR+RR rev p1 s=2
(define_insn "fused_add_sub_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (match_operand:SI 3 "register_operand" "r") (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 0, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+and RR+RR p1 s=3
(define_insn "fused_add_and_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (and:SI (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 0, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+or RR+RR p1 s=4
(define_insn "fused_add_or_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ior:SI (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 1, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+xor RR+RR p1 s=5
(define_insn "fused_add_xor_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (xor:SI (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 1, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+sll RR+RR p1 s=6
(define_insn "fused_add_sll_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 1, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+sll RR+RR rev p1 s=7
(define_insn "fused_add_sll_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (match_operand:SI 3 "register_operand" "r") (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 1, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+srl RR+RR p1 s=8
(define_insn "fused_add_srl_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 2, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+srl RR+RR rev p1 s=9
(define_insn "fused_add_srl_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (match_operand:SI 3 "register_operand" "r") (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 2, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+sra RR+RR p1 s=10
(define_insn "fused_add_sra_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 2, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+sra RR+RR rev p1 s=11
(define_insn "fused_add_sra_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (match_operand:SI 3 "register_operand" "r") (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 2, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+slt RR+RR p1 s=12
(define_insn "fused_add_slt_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 3, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+slt RR+RR rev p1 s=13
(define_insn "fused_add_slt_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (match_operand:SI 3 "register_operand" "r") (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 3, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+sltu RR+RR p1 s=14
(define_insn "fused_add_sltu_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 3, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+sltu RR+RR rev p1 s=15
(define_insn "fused_add_sltu_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (match_operand:SI 3 "register_operand" "r") (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 3, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; add+mul RR+RR p1 s=16
(define_insn "fused_add_mul_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (mult:SI (plus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 0, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+add RR+RR p1 s=17
(define_insn "fused_sub_add_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (plus:SI (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 0, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+sub RR+RR p1 s=18
(define_insn "fused_sub_sub_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 0, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+sub RR+RR rev p1 s=19
(define_insn "fused_sub_sub_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (match_operand:SI 3 "register_operand" "r") (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 0, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+and RR+RR p1 s=20
(define_insn "fused_sub_and_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (and:SI (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 1, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+or RR+RR p1 s=21
(define_insn "fused_sub_or_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ior:SI (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 1, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+xor RR+RR p1 s=22
(define_insn "fused_sub_xor_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (xor:SI (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 1, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+sll RR+RR p1 s=23
(define_insn "fused_sub_sll_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 1, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+sll RR+RR rev p1 s=24
(define_insn "fused_sub_sll_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (match_operand:SI 3 "register_operand" "r") (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 2, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+srl RR+RR p1 s=25
(define_insn "fused_sub_srl_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 2, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+srl RR+RR rev p1 s=26
(define_insn "fused_sub_srl_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (match_operand:SI 3 "register_operand" "r") (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 2, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+sra RR+RR p1 s=27
(define_insn "fused_sub_sra_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 2, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+sra RR+RR rev p1 s=28
(define_insn "fused_sub_sra_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (match_operand:SI 3 "register_operand" "r") (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 3, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+slt RR+RR p1 s=29
(define_insn "fused_sub_slt_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 3, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+slt RR+RR rev p1 s=30
(define_insn "fused_sub_slt_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (match_operand:SI 3 "register_operand" "r") (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 3, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+sltu RR+RR p1 s=31
(define_insn "fused_sub_sltu_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 3, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+sltu RR+RR rev p1 s=32
(define_insn "fused_sub_sltu_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (match_operand:SI 3 "register_operand" "r") (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 0, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sub+mul RR+RR p1 s=33
(define_insn "fused_sub_mul_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (mult:SI (minus:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 0, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+add RR+RR p1 s=34
(define_insn "fused_and_add_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (plus:SI (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 0, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+sub RR+RR p1 s=35
(define_insn "fused_and_sub_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 0, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+sub RR+RR rev p1 s=36
(define_insn "fused_and_sub_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (match_operand:SI 3 "register_operand" "r") (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 1, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+and RR+RR p1 s=37
(define_insn "fused_and_and_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (and:SI (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 1, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+or RR+RR p1 s=38
(define_insn "fused_and_or_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ior:SI (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 1, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+xor RR+RR p1 s=39
(define_insn "fused_and_xor_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (xor:SI (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 1, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+sll RR+RR p1 s=40
(define_insn "fused_and_sll_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 2, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+sll RR+RR rev p1 s=41
(define_insn "fused_and_sll_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (match_operand:SI 3 "register_operand" "r") (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 2, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+srl RR+RR p1 s=42
(define_insn "fused_and_srl_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 2, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+srl RR+RR rev p1 s=43
(define_insn "fused_and_srl_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (match_operand:SI 3 "register_operand" "r") (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 2, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+sra RR+RR p1 s=44
(define_insn "fused_and_sra_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 3, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+sra RR+RR rev p1 s=45
(define_insn "fused_and_sra_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (match_operand:SI 3 "register_operand" "r") (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 3, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+slt RR+RR p1 s=46
(define_insn "fused_and_slt_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 3, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+slt RR+RR rev p1 s=47
(define_insn "fused_and_slt_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (match_operand:SI 3 "register_operand" "r") (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 3, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+sltu RR+RR p1 s=48
(define_insn "fused_and_sltu_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 0, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+sltu RR+RR rev p1 s=49
(define_insn "fused_and_sltu_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (match_operand:SI 3 "register_operand" "r") (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 0, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; and+mul RR+RR p1 s=50
(define_insn "fused_and_mul_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (mult:SI (and:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 0, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+add RR+RR p1 s=51
(define_insn "fused_or_add_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (plus:SI (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 0, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+sub RR+RR p1 s=52
(define_insn "fused_or_sub_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 1, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+sub RR+RR rev p1 s=53
(define_insn "fused_or_sub_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (match_operand:SI 3 "register_operand" "r") (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 1, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+and RR+RR p1 s=54
(define_insn "fused_or_and_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (and:SI (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 1, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+or RR+RR p1 s=55
(define_insn "fused_or_or_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ior:SI (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 1, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+xor RR+RR p1 s=56
(define_insn "fused_or_xor_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (xor:SI (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 2, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+sll RR+RR p1 s=57
(define_insn "fused_or_sll_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 2, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+sll RR+RR rev p1 s=58
(define_insn "fused_or_sll_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (match_operand:SI 3 "register_operand" "r") (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 2, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+srl RR+RR p1 s=59
(define_insn "fused_or_srl_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 2, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+srl RR+RR rev p1 s=60
(define_insn "fused_or_srl_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (match_operand:SI 3 "register_operand" "r") (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 3, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+sra RR+RR p1 s=61
(define_insn "fused_or_sra_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 3, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+sra RR+RR rev p1 s=62
(define_insn "fused_or_sra_rr_rev_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (match_operand:SI 3 "register_operand" "r") (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 3, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+slt RR+RR p1 s=63
(define_insn "fused_or_slt_rr_p1"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 3, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

