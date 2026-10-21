"""Business API: no workflow context, paths, pool or publication in algorithms."""
from . import contracts
from . import research_topic, literature_review, experiment, data_analysis
from . import write_document, peer_review, reading_reflection

ALGORITHMS = {
    'research_topic': research_topic, 'literature_review': literature_review,
    'experiment': experiment, 'data_analysis': data_analysis,
    'write_document': write_document, 'peer_review': peer_review,
    'reading_reflection': reading_reflection,
}


def run_algorithm(step: str, inputs: dict) -> dict:
    """Validate business input, call only the requested module, validate output."""
    module = contracts.ALIASES.get(step, step)
    if module not in ALGORITHMS:
        raise ValueError(f'未知 Workflow 步骤: {step}')
    normalized = contracts.validate_input(step, inputs)
    result = ALGORITHMS[module].run(normalized)
    bound = normalized if module == 'data_analysis' and 'constraints' in normalized else None
    return contracts.validate_output(module, result, bound)
