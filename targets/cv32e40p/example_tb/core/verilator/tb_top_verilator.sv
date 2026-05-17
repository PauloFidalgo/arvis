// CV32E40P top-level testbench for Verilator simulation
// This wraps the subsystem and handles firmware loading
module tb_top #(
    parameter INSTR_RDATA_WIDTH = 32,
    parameter RAM_ADDR_WIDTH = 22,
    parameter BOOT_ADDR = 'h180,
    parameter COREV_PULP = 0,
    parameter COREV_CLUSTER = 0,
    parameter FPU = 0,
    parameter ZFINX = 0,
    parameter NUM_MHPMCOUNTERS = 29,
    parameter DM_HALTADDRESS = 32'h1A110800,
    parameter HW_LOOP = 0
)(
    input logic clk_i,
    input logic rst_ni,
    output logic tests_passed_o,
    output logic tests_failed_o,
    output logic exit_valid_o,
    output logic [31:0] exit_value_o
);

  logic fetch_enable;
  assign fetch_enable = 1'b1;

  // Load firmware at simulation start
  initial begin : load_prog
    automatic string firmware;
    if ($value$plusargs("firmware=%s", firmware)) begin
      $display("[TB] Loading firmware: %s", firmware);
      $readmemh(firmware, wrapper_i.ram_i.dp_ram_i.mem);
    end else begin
      $display("[TB] ERROR: No firmware specified. Use +firmware=<file.hex>");
      $finish;
    end
  end

  // wrapper for riscv, the memory system and stdout peripheral
  cv32e40p_tb_subsystem #(
      .INSTR_RDATA_WIDTH(INSTR_RDATA_WIDTH),
      .RAM_ADDR_WIDTH   (RAM_ADDR_WIDTH),
      .BOOT_ADDR        (BOOT_ADDR),
      .COREV_PULP       (COREV_PULP),
      .COREV_CLUSTER    (COREV_CLUSTER),
      .FPU              (FPU),
      .ZFINX            (ZFINX),
      .NUM_MHPMCOUNTERS (NUM_MHPMCOUNTERS),
      .DM_HALTADDRESS   (DM_HALTADDRESS),
      .HW_LOOP          (HW_LOOP)
  ) wrapper_i (
      .clk_i         (clk_i),
      .rst_ni        (rst_ni),
      .fetch_enable_i(fetch_enable),
      .tests_passed_o(tests_passed_o),
      .tests_failed_o(tests_failed_o),
      .exit_valid_o  (exit_valid_o),
      .exit_value_o  (exit_value_o)
  );

endmodule
