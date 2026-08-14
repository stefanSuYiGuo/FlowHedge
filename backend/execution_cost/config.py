"""Centralized Step 8A fee assumptions.

No institutional fee tier has been supplied, so the production singleton is
deliberately empty and every candidate reports fee_status=UNCONFIGURED.
"""

from .models import ExecutionFeeConfig


execution_fee_config = ExecutionFeeConfig()
