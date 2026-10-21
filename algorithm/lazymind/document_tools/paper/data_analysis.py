"""Run model-generated analysis and figures from the current F4 input contract."""
from .analysis.pipeline import run as generated_analysis


def run(inputs: dict) -> dict:
    """Generate, execute and independently verify the requested analysis."""
    return generated_analysis(inputs)
