"""
ARVIS CLI pretty printing with colors and structured output.
"""

import os

_NO_COLOR = os.environ.get("NO_COLOR", "") != ""


class Colors:
    PURPLE = "" if _NO_COLOR else "\033[95m"
    BLUE = "" if _NO_COLOR else "\033[94m"
    CYAN = "" if _NO_COLOR else "\033[96m"
    GREEN = "" if _NO_COLOR else "\033[92m"
    YELLOW = "" if _NO_COLOR else "\033[93m"
    RED = "" if _NO_COLOR else "\033[91m"
    BOLD = "" if _NO_COLOR else "\033[1m"
    UNDERLINE = "" if _NO_COLOR else "\033[4m"
    DIM = "" if _NO_COLOR else "\033[2m"
    END = "" if _NO_COLOR else "\033[0m"


def print_banner():
    """Print ARVIS ASCII banner."""
    banner = f"""{Colors.PURPLE}{Colors.BOLD}
    ╔═══════════════════════════════════════════════════╗
    ║                                                   ║
    ║         █████╗ ██████╗ ██╗   ██╗██╗███████╗       ║
    ║        ██╔══██╗██╔══██╗██║   ██║██║██╔════╝       ║
    ║        ███████║██████╔╝██║   ██║██║███████╗       ║
    ║        ██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║       ║
    ║        ██║  ██║██║  ██║ ╚████╔╝ ██║███████║       ║
    ║        ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝       ║
    ║                                                   ║
    ║     Automated RISC-V Intelligent Specialisation   ║
    ║                                                   ║
    ╚═══════════════════════════════════════════════════╝
{Colors.END}"""
    print(banner, flush=True)


def print_step(step: str, message: str):
    """Print a step with icon."""
    print(f"{Colors.CYAN}{Colors.BOLD}[{step}]{Colors.END} {message}", flush=True)


def print_success(message: str):
    """Print success message."""
    print(f"  {Colors.GREEN}✓{Colors.END} {message}", flush=True)


def print_error(message: str):
    """Print error message."""
    print(f"  {Colors.RED}✗{Colors.END} {message}", flush=True)


def print_warning(message: str):
    """Print warning message."""
    print(f"  {Colors.YELLOW}⚠{Colors.END} {message}", flush=True)


def print_info(message: str):
    """Print info message."""
    print(f"  {Colors.BLUE}ℹ{Colors.END} {message}", flush=True)


def print_section(title: str):
    """Print section header."""
    print(f"\n{Colors.BOLD}{Colors.PURPLE}{'═' * 60}{Colors.END}", flush=True)
    print(f"{Colors.BOLD}{Colors.PURPLE}  {title}{Colors.END}", flush=True)
    print(f"{Colors.BOLD}{Colors.PURPLE}{'═' * 60}{Colors.END}\n", flush=True)


def print_subsection(title: str):
    """Print subsection header."""
    print(
        f"\n{Colors.BOLD}{Colors.CYAN}── {title} ──{Colors.END}",
        flush=True,
    )


def print_metric(label: str, value: str, unit: str = ""):
    """Print a metric."""
    full_value = f"{value} {unit}".strip()
    print(
        f"  {Colors.CYAN}{label:.<40}{Colors.END} {Colors.BOLD}{full_value}{Colors.END}",
        flush=True,
    )
