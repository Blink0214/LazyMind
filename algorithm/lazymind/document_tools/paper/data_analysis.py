"""Run model-generated analysis and figures from the current F4 input contract."""
import re

from .analysis.pipeline import run as generated_analysis


def run(inputs: dict) -> dict:
    """Normalize F4 input once, then execute and publish the requested analysis."""
    from .contracts import validate_input
    from .analysis import trace as diagnostics
    with diagnostics.stage('input_validation'):
        normalized = validate_input('data_analysis', inputs)
    return generated_analysis(normalized)


def retry_request(params, request):
    """F4 retry commands resume the original requirements instead of replacing them."""
    def requirement(value):
        text = str(value or '').strip()
        if text.rstrip('。.!！').lower() in {'重试', '重新执行', 'retry', 're-run', 'rerun', 'try again'}:
            return ''
        if re.fullmatch(r'(?:请重新执行步骤|Please re-run step)\s+[A-Za-z0-9_.:-]+', text, re.I):
            return ''
        return text
    return next((text for value in (request, params.get('user_input'), params.get('launch_user_input'))
                 if (text := requirement(value))), '')
