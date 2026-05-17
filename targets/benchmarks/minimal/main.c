/* Minimal benchmark — uses absolute minimum instructions
 * Just a counter loop + memory write for verification
 */
#include <stdint.h>

static volatile uint32_t data[256];

int main(void) {
    uint32_t sum = 0;

    /* Write known pattern */
    for (int i = 0; i < 256; i++) {
        data[i] = i;
    }

    /* Read and accumulate */
    for (int i = 0; i < 256; i++) {
        sum += data[i];
    }

    /* Expected: sum of 0..255 = 32640 = 0x7F80 */
    /* Return 0 for success, nonzero for failure.
     * The crt0 handles platform-specific exit signaling. */
    return (sum == 32640) ? 0 : 1;
}