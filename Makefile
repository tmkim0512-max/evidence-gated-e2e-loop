# Demo state lives in .demo-state/ and is recreated on every run.
DEMO = LOOP_HOME=.demo-state

.PHONY: demo test

demo:
	rm -rf .demo-state
	$(DEMO) loop run --workers 3
	$(DEMO) BUG=cart_total loop replay
	$(DEMO) loop replay

test:
	python -m pytest -q
	python scripts/red_check.py
