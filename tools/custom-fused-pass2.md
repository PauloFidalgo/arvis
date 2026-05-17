;; === Pass 2: RR+RR 2-grams batch 2 (no variable immediates) ===
;; Total: 64 patterns (R4=64)
;; or+slt RR+RR rev p2 s=0
(define_insn "fused_or_slt_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (match_operand:SI 3 "register_operand" "r") (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 0, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+sltu RR+RR p2 s=1
(define_insn "fused_or_sltu_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 0, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+sltu RR+RR rev p2 s=2
(define_insn "fused_or_sltu_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (match_operand:SI 3 "register_operand" "r") (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 0, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; or+mul RR+RR p2 s=3
(define_insn "fused_or_mul_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (mult:SI (ior:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 0, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+add RR+RR p2 s=4
(define_insn "fused_xor_add_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (plus:SI (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 1, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+sub RR+RR p2 s=5
(define_insn "fused_xor_sub_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 1, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+sub RR+RR rev p2 s=6
(define_insn "fused_xor_sub_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (match_operand:SI 3 "register_operand" "r") (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 1, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+and RR+RR p2 s=7
(define_insn "fused_xor_and_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (and:SI (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 1, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+or RR+RR p2 s=8
(define_insn "fused_xor_or_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ior:SI (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 2, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+xor RR+RR p2 s=9
(define_insn "fused_xor_xor_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (xor:SI (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 2, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+sll RR+RR p2 s=10
(define_insn "fused_xor_sll_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 2, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+sll RR+RR rev p2 s=11
(define_insn "fused_xor_sll_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (match_operand:SI 3 "register_operand" "r") (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 2, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+srl RR+RR p2 s=12
(define_insn "fused_xor_srl_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 3, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+srl RR+RR rev p2 s=13
(define_insn "fused_xor_srl_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (match_operand:SI 3 "register_operand" "r") (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 3, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+sra RR+RR p2 s=14
(define_insn "fused_xor_sra_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 3, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+sra RR+RR rev p2 s=15
(define_insn "fused_xor_sra_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (match_operand:SI 3 "register_operand" "r") (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x0b, 3, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+slt RR+RR p2 s=16
(define_insn "fused_xor_slt_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 0, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+slt RR+RR rev p2 s=17
(define_insn "fused_xor_slt_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (match_operand:SI 3 "register_operand" "r") (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 0, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+sltu RR+RR p2 s=18
(define_insn "fused_xor_sltu_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 0, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+sltu RR+RR rev p2 s=19
(define_insn "fused_xor_sltu_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (match_operand:SI 3 "register_operand" "r") (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 0, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; xor+mul RR+RR p2 s=20
(define_insn "fused_xor_mul_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (mult:SI (xor:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 1, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+add RR+RR p2 s=21
(define_insn "fused_sll_add_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (plus:SI (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 1, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+sub RR+RR p2 s=22
(define_insn "fused_sll_sub_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 1, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+sub RR+RR rev p2 s=23
(define_insn "fused_sll_sub_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (match_operand:SI 3 "register_operand" "r") (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 1, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+and RR+RR p2 s=24
(define_insn "fused_sll_and_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (and:SI (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 2, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+or RR+RR p2 s=25
(define_insn "fused_sll_or_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ior:SI (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 2, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+xor RR+RR p2 s=26
(define_insn "fused_sll_xor_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (xor:SI (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 2, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+sll RR+RR p2 s=27
(define_insn "fused_sll_sll_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 2, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+sll RR+RR rev p2 s=28
(define_insn "fused_sll_sll_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (match_operand:SI 3 "register_operand" "r") (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 3, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+srl RR+RR p2 s=29
(define_insn "fused_sll_srl_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 3, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+srl RR+RR rev p2 s=30
(define_insn "fused_sll_srl_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (match_operand:SI 3 "register_operand" "r") (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 3, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+sra RR+RR p2 s=31
(define_insn "fused_sll_sra_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x2b, 3, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+sra RR+RR rev p2 s=32
(define_insn "fused_sll_sra_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (match_operand:SI 3 "register_operand" "r") (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 0, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+slt RR+RR p2 s=33
(define_insn "fused_sll_slt_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 0, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+slt RR+RR rev p2 s=34
(define_insn "fused_sll_slt_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (match_operand:SI 3 "register_operand" "r") (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 0, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+sltu RR+RR p2 s=35
(define_insn "fused_sll_sltu_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 0, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+sltu RR+RR rev p2 s=36
(define_insn "fused_sll_sltu_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (match_operand:SI 3 "register_operand" "r") (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 1, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sll+mul RR+RR p2 s=37
(define_insn "fused_sll_mul_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (mult:SI (ashift:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 1, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+add RR+RR p2 s=38
(define_insn "fused_srl_add_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (plus:SI (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 1, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+sub RR+RR p2 s=39
(define_insn "fused_srl_sub_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 1, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+sub RR+RR rev p2 s=40
(define_insn "fused_srl_sub_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (match_operand:SI 3 "register_operand" "r") (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 2, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+and RR+RR p2 s=41
(define_insn "fused_srl_and_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (and:SI (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 2, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+or RR+RR p2 s=42
(define_insn "fused_srl_or_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ior:SI (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 2, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+xor RR+RR p2 s=43
(define_insn "fused_srl_xor_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (xor:SI (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 2, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+sll RR+RR p2 s=44
(define_insn "fused_srl_sll_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 3, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+sll RR+RR rev p2 s=45
(define_insn "fused_srl_sll_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (match_operand:SI 3 "register_operand" "r") (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 3, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+srl RR+RR p2 s=46
(define_insn "fused_srl_srl_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 3, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+srl RR+RR rev p2 s=47
(define_insn "fused_srl_srl_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (match_operand:SI 3 "register_operand" "r") (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x5b, 3, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+sra RR+RR p2 s=48
(define_insn "fused_srl_sra_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 0, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+sra RR+RR rev p2 s=49
(define_insn "fused_srl_sra_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashiftrt:SI (match_operand:SI 3 "register_operand" "r") (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 0, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+slt RR+RR p2 s=50
(define_insn "fused_srl_slt_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 0, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+slt RR+RR rev p2 s=51
(define_insn "fused_srl_slt_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lt:SI (match_operand:SI 3 "register_operand" "r") (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 0, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+sltu RR+RR p2 s=52
(define_insn "fused_srl_sltu_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 1, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+sltu RR+RR rev p2 s=53
(define_insn "fused_srl_sltu_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ltu:SI (match_operand:SI 3 "register_operand" "r") (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 1, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; srl+mul RR+RR p2 s=54
(define_insn "fused_srl_mul_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (mult:SI (lshiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 1, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+add RR+RR p2 s=55
(define_insn "fused_sra_add_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (plus:SI (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 1, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+sub RR+RR p2 s=56
(define_insn "fused_sra_sub_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 2, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+sub RR+RR rev p2 s=57
(define_insn "fused_sra_sub_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (minus:SI (match_operand:SI 3 "register_operand" "r") (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 2, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+and RR+RR p2 s=58
(define_insn "fused_sra_and_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (and:SI (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 2, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+or RR+RR p2 s=59
(define_insn "fused_sra_or_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ior:SI (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 2, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+xor RR+RR p2 s=60
(define_insn "fused_sra_xor_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (xor:SI (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 3, 0, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+sll RR+RR p2 s=61
(define_insn "fused_sra_sll_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 3, 1, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+sll RR+RR rev p2 s=62
(define_insn "fused_sra_sll_rr_rev_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (ashift:SI (match_operand:SI 3 "register_operand" "r") (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r"))))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 3, 2, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

;; sra+srl RR+RR p2 s=63
(define_insn "fused_sra_srl_rr_p2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (lshiftrt:SI (ashiftrt:SI (match_operand:SI 1 "register_operand" "r") (match_operand:SI 2 "register_operand" "r")) (match_operand:SI 3 "register_operand" "r")))]
  "TARGET_CUSTOM_FUSED"
  ".insn r4 0x7b, 3, 3, %0, %1, %2, %3"
  [(set_attr "type" "arith")])

