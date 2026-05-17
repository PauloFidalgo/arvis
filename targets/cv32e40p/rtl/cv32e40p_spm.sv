// Scratchpad Memory for CV32E40P
// Shadows main memory - initialized from same hex file

module cv32e40p_spm #(
    parameter SPM_SIZE = 10240,
    parameter SPM_ADDR_BASE = 32'h8001167c,
    parameter SPM_ADDR_END  = SPM_ADDR_BASE + SPM_SIZE
) (
    input  logic        clk,
    input  logic        rst_n,
    
    // Request interface
    input  logic        req_i,
    input  logic [31:0] addr_i,
    input  logic        we_i,
    input  logic [3:0]  be_i,
    input  logic [31:0] wdata_i,
    
    // Response interface  
    output logic        hit_o,
    output logic [31:0] rdata_o
);

    localparam ADDR_WIDTH = $clog2(SPM_SIZE);
    localparam NUM_WORDS = SPM_SIZE / 4;
    
    // SPM memory array
    logic [31:0] mem [0:NUM_WORDS-1];
    
    // Address hit detection
    wire addr_hit = (addr_i >= SPM_ADDR_BASE) && (addr_i < SPM_ADDR_END);
    assign hit_o = req_i && addr_hit;
    
    // Local address (word index)
    wire [ADDR_WIDTH-3:0] local_addr = (addr_i - SPM_ADDR_BASE) >> 2;
    
    // Read (1-cycle latency)
    always_ff @(posedge clk) begin
        if (req_i && addr_hit && !we_i) begin
            rdata_o <= mem[local_addr];
        end
    end
    
    // Write with byte enables
    always_ff @(posedge clk) begin
        if (req_i && addr_hit && we_i) begin
            if (be_i[0]) mem[local_addr][7:0]   <= wdata_i[7:0];
            if (be_i[1]) mem[local_addr][15:8]  <= wdata_i[15:8];
            if (be_i[2]) mem[local_addr][23:16] <= wdata_i[23:16];
            if (be_i[3]) mem[local_addr][31:24] <= wdata_i[31:24];
        end
    end

endmodule
