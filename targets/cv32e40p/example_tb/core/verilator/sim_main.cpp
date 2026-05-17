#include <verilated.h>
#include "Vtb_top.h"
#include <iostream>
#include <cstdlib>

vluint64_t main_time = 0;
double sc_time_stamp() { return main_time; }

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);

    vluint64_t max_cycles = 100000000;
    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], "+maxcycles=", 11) == 0) {
            max_cycles = atoll(argv[i] + 11);
        }
    }

    Vtb_top* top = new Vtb_top;

    // Reset
    top->clk_i = 0;
    top->rst_ni = 0;
    for (int i = 0; i < 10; i++) {
        top->clk_i = !top->clk_i;
        top->eval();
        main_time++;
    }
    top->rst_ni = 1;

    vluint64_t cycle = 0;
    int exit_code = 0;
    
    std::cout << "Starting simulation..." << std::endl;
    
    while (!Verilated::gotFinish() && cycle < max_cycles) {
        top->clk_i = !top->clk_i;
        top->eval();
        main_time++;

        if (top->clk_i) {
            cycle++;
            
            if (top->exit_valid_o) {
                std::cout << "EXIT " << (top->exit_value_o == 0 ? "SUCCESS" : "FAILURE") 
                          << " after " << cycle << " cycles" << std::endl;
                exit_code = (top->exit_value_o != 0);
                break;
            }
            if (top->tests_passed_o) {
                std::cout << "TESTS PASSED after " << cycle << " cycles" << std::endl;
                break;
            }
            if (top->tests_failed_o) {
                std::cout << "TESTS FAILED after " << cycle << " cycles" << std::endl;
                exit_code = 1;
                break;
            }
            
            if (cycle % 10000000 == 0) {
                std::cout << "Cycle: " << cycle << std::endl;
            }
        }
    }

    if (cycle >= max_cycles) {
        std::cout << "Timeout after " << max_cycles << " cycles" << std::endl;
        exit_code = 2;
    }

    delete top;
    return exit_code;
}
