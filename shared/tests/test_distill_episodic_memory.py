"""The hand-run CLI is a thin shell over shared.episodic_memory.

The logic itself moved to shared/episodic_memory.py (and is tested in
test_episodic_memory.py) because no deployed image contains repo-root
scripts/ -- which is why the nightly distillation this script describes had
never actually run. What is left worth pinning here is the seam: the names
this module used to define stay importable from it, so a developer's muscle
memory and anything already importing them keeps working.
"""

import scripts.distill_episodic_memory as cli
from shared import episodic_memory


def test_the_cli_reexports_the_shared_implementations():
    assert cli.anchors_to_refresh is episodic_memory.anchors_to_refresh
    assert cli.build_distillation_prompt is episodic_memory.build_distillation_prompt
    assert cli.distill_anchor_type is episodic_memory.distill_anchor_type


def test_the_cli_reexports_the_shared_tuning_constants():
    assert cli.LOOKBACK_DAYS == episodic_memory.LOOKBACK_DAYS
    assert cli.MAX_MESSAGES == episodic_memory.MAX_MESSAGES
    assert cli.TARGET_WORDS == episodic_memory.TARGET_WORDS
