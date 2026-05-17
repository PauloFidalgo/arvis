
;; Hardware loop support
(define_expand "doloop_end"
  [(parallel [(set (pc)
                   (if_then_else
                    (ne (match_operand:SI 0 "register_operand")
                        (const_int 1))
                    (label_ref (match_operand 1 ""))
                    (pc)))
              (set (match_dup 0)
                   (plus:SI (match_dup 0) (const_int -1)))])]
  "TARGET_HWLOOP"
{
  if (GET_MODE (operands[0]) != SImode)
    FAIL;
})

(define_insn "*doloop_end_si"
  [(set (pc)
        (if_then_else (ne (match_operand:SI 0 "register_operand" "+r")
                          (const_int 1))
                      (label_ref (match_operand 1 "" ""))
                      (pc)))
   (set (match_dup 0) (plus:SI (match_dup 0) (const_int -1)))]
  "TARGET_HWLOOP"
  "addi\t%0,%0,-1\;bnez\t%0,%l1"
  [(set_attr "type" "branch")
   (set_attr "length" "8")])
