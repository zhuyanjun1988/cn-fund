.PHONY: check-python test verify-evidence policy

PYTHON ?= $(shell command -v python3.12 2>/dev/null || command -v python3.11 2>/dev/null || command -v python3)

check-python:
	@$(PYTHON) -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required"'

test: check-python
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

verify-evidence: check-python
	PYTHONPATH=src $(PYTHON) -m cn_fund_strategy.interfaces.public_cli verify-evidence \
		--ledger evidence/g6-standard-1/daily_accounting_ledger.json \
		--summary evidence/g6-standard-1/public_result_summary.json

policy: check-python
	PYTHONPATH=src $(PYTHON) -m cn_fund_strategy.interfaces.public_cli policy
