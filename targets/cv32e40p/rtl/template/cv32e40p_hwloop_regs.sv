module cv32e40p_hwloop_regs #(
    parameter HW_LOOP     = 2,
    parameter CNT_WIDTH   = 32,  // ARVIS: counter width (auto-tuned from benchmark analysis)
    parameter N_REG_BITS = HW_LOOP > 1 ? $clog2(HW_LOOP) : 1
) (
    input logic clk,
    input logic rst_n,

    // from ex stage
    input logic [          31:0] hwlp_start_data_i,
    input logic [          31:0] hwlp_end_data_i,
    input logic [          31:0] hwlp_cnt_data_i,
    input logic                  hwlp_we_i,
    input logic [N_REG_BITS-1:0] hwlp_regid_i,
    input logic [           1:0] hwlp_pnult_off_i,
    input logic [           2:0] hwlp_funct3_i,

    // from controller
    input logic valid_i,

    // from hwloop controller
    input logic [HW_LOOP-1:0] hwlp_dec_cnt_i,

    // to hwloop controller
    output logic [HW_LOOP-1:0][31:0] hwlp_start_addr_o,
    output logic [HW_LOOP-1:0][31:0] hwlp_end_addr_o,
    output logic [HW_LOOP-1:0][31:0] hwlp_counter_o,
    output logic [HW_LOOP-1:0][31:0] hwlp_pnult_addr_o,
    output logic [HW_LOOP-1:0][31:0] hwlp_counter_next_o
);


  logic [HW_LOOP-1:0][31:0] hwlp_start_q;
  // ARVIS FPGA: hwlp_end_q feeds the controller's hwlp_end_eq_pc comparator,
  // which has ~10 downstream consumers. MAX_FANOUT asks Vivado to replicate
  // this register close to each consumer, reducing routing delay on the
  // per-cycle PC-match path. KEEP prevents the replicas from being merged
  // back during post-synthesis optimisation. The attributes are silently
  // ignored by ASIC synthesisers, so the change is platform-neutral.
  (* MAX_FANOUT = 30 *) (* KEEP = "true" *)
  logic [HW_LOOP-1:0][31:0] hwlp_end_q;
  logic [HW_LOOP-1:0][CNT_WIDTH-1:0] hwlp_counter_q, hwlp_counter_n;
  // hwlp_counter_shadow is only a real register when HW_LOOP > 1 (nested
  // loops); for HW_LOOP = 1 the driving generate branch below ties it to
  // zero so the synthesiser strips all FFs.
  logic [HW_LOOP-1:0][CNT_WIDTH-1:0] hwlp_counter_shadow;
  logic [HW_LOOP-1:0][1:0] hwlp_pnult_off_q;

  int unsigned i;


  always_comb
    for (int j = 0; j < HW_LOOP; j++)
      hwlp_counter_next_o[j] = {{(32-CNT_WIDTH){1'b0}},
                                 (hwlp_we_cnt && j[N_REG_BITS-1:0] == hwlp_regid_i) ? hwlp_cnt_data_i[CNT_WIDTH-1:0] :
                                 ((hwlp_we_start || hwlp_we_end) && !hwlp_we_cnt && j[N_REG_BITS-1:0] == hwlp_regid_i) ? {CNT_WIDTH{1'b0}} :
                                 hwlp_counter_q[j]};
  assign hwlp_start_addr_o = hwlp_start_q;
  assign hwlp_end_addr_o   = hwlp_end_q;
  // Zero-extend narrow counters to 32-bit output ports
  for (genvar z = 0; z < HW_LOOP; z++) begin : gen_cnt_zext
    assign hwlp_counter_o[z] = {{(32-CNT_WIDTH){1'b0}}, hwlp_counter_q[z]};
  end


  // Write enables per field based on funct3
  logic hwlp_we_start;     // write start address
  logic hwlp_we_end;       // write end address
  logic hwlp_we_start_end; // write both (BOUNDS)
  logic hwlp_we_cnt;       // write count
  // ARVIS_HWLP_BEGIN: hwlp_regs_we
  assign hwlp_we_start     = hwlp_we_i && (hwlp_funct3_i == 3'b010 || hwlp_funct3_i == 3'b100);
  assign hwlp_we_end       = hwlp_we_i && (hwlp_funct3_i == 3'b010 || hwlp_funct3_i == 3'b101);
  assign hwlp_we_start_end = hwlp_we_i && (hwlp_funct3_i == 3'b010);
  assign hwlp_we_cnt       = hwlp_we_i && (hwlp_funct3_i == 3'b011);
  // ARVIS_HWLP_END: hwlp_regs_we

  /////////////////////////////////////////////////////////////////////////////////
  // HWLOOP start-address register                                               //
  /////////////////////////////////////////////////////////////////////////////////
  always_ff @(posedge clk, negedge rst_n) begin : HWLOOP_REGS_START
    if (rst_n == 1'b0) begin
      hwlp_start_q <= '{default: 32'b0};
    end else if (hwlp_we_start) begin
      hwlp_start_q[hwlp_regid_i] <= hwlp_start_data_i;
    end
  end

  // ARVIS FPGA: register the penultimate address at write time.
  // Original code computed hwlp_pnult_addr_o combinationally every cycle as
  // hwlp_end_q - (2 or 4), placing a 32-bit subtractor + CARRY4 chain on the
  // fanout path to the controller (hwlp_end_eq_pc_plus4 comparator). Since the
  // inputs are both registers that only change on an hwloop write, we can
  // compute and latch the result once at write time. Cycle-accurate with the
  // original: both versions present the new value on the cycle following the
  // write, because hwlp_pnult_off_q and hwlp_end_q update on the same edge.
  logic [HW_LOOP-1:0][31:0] hwlp_pnult_addr_q;

  // Use the end address being written this cycle if applicable, else the
  // currently stored value.
  wire [31:0] eff_end_data =
      (hwlp_we_end || hwlp_we_start_end) ? hwlp_end_data_i
                                         : hwlp_end_q[hwlp_regid_i];
  wire [31:0] new_pnult_addr =
      eff_end_data - (hwlp_pnult_off_i[0] ? 32'd4 : 32'd2);

  always_ff @(posedge clk, negedge rst_n) begin
    if (!rst_n) begin
      hwlp_pnult_off_q  <= '0;
      hwlp_pnult_addr_q <= '{default: 32'b0};
    end else if (hwlp_we_i) begin
      hwlp_pnult_off_q[hwlp_regid_i]  <= hwlp_pnult_off_i;
      hwlp_pnult_addr_q[hwlp_regid_i] <= new_pnult_addr;
    end
  end

  assign hwlp_pnult_addr_o = hwlp_pnult_addr_q;

  /////////////////////////////////////////////////////////////////////////////////
  // HWLOOP end-address register                                                 //
  /////////////////////////////////////////////////////////////////////////////////
  always_ff @(posedge clk, negedge rst_n) begin : HWLOOP_REGS_END
    if (rst_n == 1'b0) begin
      hwlp_end_q <= '{default: 32'b0};
    end else if (hwlp_we_end) begin
      hwlp_end_q[hwlp_regid_i] <= hwlp_end_data_i;
    end
  end


  /////////////////////////////////////////////////////////////////////////////////
  // HWLOOP counter register with decrement logic                                //
  /////////////////////////////////////////////////////////////////////////////////
  genvar k;
  for (k = 0; k < HW_LOOP; k++) begin
    assign hwlp_counter_n[k] = hwlp_counter_q[k] - 1;
  end

  // ARVIS: shadow register for auto-reload.
  // Only meaningful for nested hwloops (HW_LOOP > 1). With a single level,
  // no outer loop can trigger a reload, so we gate the whole thing away
  // under a generate — the synthesiser typically prunes dead logic anyway,
  // but the guard guarantees the flip-flops vanish regardless of tool
  // version and makes the RTL intent explicit.
  if (HW_LOOP > 1) begin : gen_shadow_counter
    always_ff @(posedge clk, negedge rst_n) begin : HWLOOP_SHADOW_COUNTER
      if (rst_n == 1'b0) hwlp_counter_shadow <= '{default: {CNT_WIDTH{1'b0}}};
      else if (hwlp_we_cnt) hwlp_counter_shadow[hwlp_regid_i] <= hwlp_cnt_data_i[CNT_WIDTH-1:0];
    end
  end else begin : gen_no_shadow_counter
    assign hwlp_counter_shadow = '{default: {CNT_WIDTH{1'b0}}};
  end

  logic [HW_LOOP-1:0] hwlp_dec_pending;
  logic [HW_LOOP-1:0] hwlp_dec_effective;

  // ARVIS FPGA: flatten the 4-deep else-if chain into a parallel
  // clear/set pair. The original logic is preserved (clear has priority
  // over set), but the dependency path is shortened by one LUT level.
  wire hwlp_we_any = hwlp_we_cnt || hwlp_we_start || hwlp_we_end;
  logic [HW_LOOP-1:0] hwlp_dec_pending_clr;
  logic [HW_LOOP-1:0] hwlp_dec_pending_set;
  for (genvar j = 0; j < HW_LOOP; j++) begin : gen_dec_pending_en
    assign hwlp_dec_pending_clr[j] =
        (hwlp_we_any && (j[N_REG_BITS-1:0] == hwlp_regid_i)) ||
        (hwlp_dec_cnt_i[j] && valid_i) ||
         hwlp_dec_pending[j];
    assign hwlp_dec_pending_set[j] =
         hwlp_dec_cnt_i[j] && !valid_i && !hwlp_dec_pending[j];
  end
  always_ff @(posedge clk, negedge rst_n) begin
    if (!rst_n) hwlp_dec_pending <= '0;
    else for (int j = 0; j < HW_LOOP; j++) begin
      hwlp_dec_pending[j] <= hwlp_dec_pending_clr[j] ? 1'b0 : hwlp_dec_pending_set[j];
    end
  end
  for (genvar j = 0; j < HW_LOOP; j++)
    assign hwlp_dec_effective[j] = hwlp_dec_cnt_i[j];

  // ARVIS FPGA: pre-compute per-level "any outer counter decrements this cycle"
  // as an OR-reduction. The previous nested for loop synthesised as a priority
  // chain of length i-1, which scales badly for HW_LOOP>=3. Functionally
  // identical: reloads when an outer loop decremented and this loop did not.
  logic [HW_LOOP-1:0] hwlp_any_outer_dec;
  for (genvar i = 0; i < HW_LOOP; i++) begin : gen_any_outer_dec
    if (i == 0)      assign hwlp_any_outer_dec[i] = 1'b0;
    else             assign hwlp_any_outer_dec[i] = |hwlp_dec_effective[i-1:0];
  end

  always_ff @(posedge clk, negedge rst_n) begin : HWLOOP_REGS_COUNTER
    if (rst_n == 1'b0) begin
      hwlp_counter_q <= '{default: {CNT_WIDTH{1'b0}}};
    end else begin
      for (i = 0; i < HW_LOOP; i++) begin
        if (hwlp_we_cnt && (i[N_REG_BITS-1:0] == hwlp_regid_i)) begin
          hwlp_counter_q[i] <= hwlp_cnt_data_i[CNT_WIDTH-1:0];
        end else if ((hwlp_we_start || hwlp_we_end) && !hwlp_we_cnt && (i[N_REG_BITS-1:0] == hwlp_regid_i)) begin
          // Bounds-only write: clear counter to prevent stale value interference
          hwlp_counter_q[i] <= '0;
        end else begin
          if (hwlp_dec_effective[i]) begin
            hwlp_counter_q[i] <= hwlp_counter_n[i];
          end else if (hwlp_any_outer_dec[i]) begin
            // Auto-reload: outer loop decremented, this loop did not.
            hwlp_counter_q[i] <= hwlp_counter_shadow[i];
          end
        end
      end
    end
  end
  //----------------------------------------------------------------------------
  // Assertions
  //----------------------------------------------------------------------------
`ifdef CV32E40P_ASSERT_ON
  // do not decrement more than one counter at once
  assert property (@(posedge clk) (valid_i) |-> ($countones(hwlp_dec_cnt_i) <= 1));
`endif


endmodule
