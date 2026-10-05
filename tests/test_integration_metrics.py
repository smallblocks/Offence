"""Counter parsing must fail closed instead of reporting missing data as idle."""
import importlib.util
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("resilience_integration", SCRIPTS / "resilience_integration.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
sys.path.remove(str(SCRIPTS))


def metrics(model='selected', generated='12', running='0', waiting='0'):
    return '\n'.join(f'vllm:{key}{{model_name="{model}",engine="0"}} {value}' for key, value in (
        ('generation_tokens_total', generated), ('num_requests_running', running),
        ('num_requests_waiting', waiting)))


def test_metrics_select_model_and_sum_engine_series():
    parsed = runner.parse_metrics(metrics() + '\n' + metrics(generated='3') + '\n' + metrics('other', running='8'), 'selected')
    assert parsed == {'generation_tokens_total': 15, 'num_requests_running': 0, 'num_requests_waiting': 0}


@pytest.mark.parametrize('body', ['', metrics('other'), metrics().split('\n')[0],
    metrics(generated='NaN'), metrics(generated='Inf'), metrics(running='-1')])
def test_metrics_reject_missing_or_invalid_counters(body):
    with pytest.raises(RuntimeError):
        runner.parse_metrics(body, 'selected')
