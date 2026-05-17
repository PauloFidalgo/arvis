// Instruction Scratchpad Memory for CV32E40P

module cv32e40p_ispm #(
    parameter ISPM_SIZE = 1024,
    parameter ISPM_ADDR_BASE = 32'h80000000,
    parameter ISPM_ADDR_END  = ISPM_ADDR_BASE + ISPM_SIZE
) (
    input  logic        clk,
    input  logic        rst_n,
    
    input  logic        req_i,
    input  logic [31:0] addr_i,
    
    output logic        hit_o,
    output logic [31:0] rdata_o
);

    localparam NUM_WORDS = ISPM_SIZE / 4;
    
    logic [31:0] mem [0:NUM_WORDS-1];
    
    wire addr_hit = (addr_i >= ISPM_ADDR_BASE) && (addr_i < ISPM_ADDR_END);
    assign hit_o = req_i && addr_hit;
    
    wire [$clog2(ISPM_SIZE)-3:0] local_addr = (addr_i - ISPM_ADDR_BASE) >> 2;
    
    // 1-cycle read
    always_ff @(posedge clk) begin
        if (req_i && addr_hit)
            rdata_o <= mem[local_addr];
    end

    // Initialize from firmware file (same as main RAM)
    // This task is called from testbench after loading firmware
    task automatic init_from_ram(input logic [31:0] ram_data[]);
        for (int i = 0; i < NUM_WORDS; i++) begin
            automatic int ram_addr = (ISPM_ADDR_BASE >> 2) + i;
            if (ram_addr < $size(ram_data))
                mem[i] = ram_data[ram_addr];
        end
        $display("[TB] I-SPM initialized: %0d bytes from 0x%08x", ISPM_SIZE, ISPM_ADDR_BASE);
    endtask

endmodule
