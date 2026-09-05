# Runs end-to-end from a clean clone with only .env filled in (or even empty: fixtures + templates).
# recursively expanded (=) not simply expanded (:=): re-evaluated at each use, so `make setup demo`
# in one invocation picks up the .venv that `setup` just created instead of resolving before it exists.
PY = $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

.PHONY: setup demo chaos simulate eval insights test clean

setup:            ## create venv, install pinned deps, copy .env.example -> .env if missing
	python3 -m venv .venv
	.venv/bin/pip install --quiet --upgrade pip
	.venv/bin/pip install --quiet -r requirements.txt
	@[ -f .env ] || cp .env.example .env
	@echo "ok: .venv ready. Fill .env (optional) then: make demo"

demo:             ## seed 22 failure events -> classify -> decide -> execute -> poll outcomes -> show
	$(PY) -m app.main demo

chaos:            ## inject all nine faults, print the path taken for each, write docs/failure_report.md
	$(PY) scripts/chaos.py --all --write docs/failure_report.md

simulate:         ## 500 synthetic events: naive retry-everything baseline vs the policy table
	$(PY) scripts/simulate.py --write docs/simulation_report.md

serve: ; $(PY) -m app.web --port 8000  # read-only operator view on http://127.0.0.1:8000 (no auth, loopback only, Ctrl-C to stop)
test:
	$(PY) -m pytest -q

clean:
	rm -f recovery.db recovery.db-journal

insights:         ## recovery rates by cause and delay (Wilson intervals), LLM usage, money by merchant, proposals
	$(PY) -m app.main insights

eval:             ## score the rule classifier over the hand-labelled set, write docs/classifier_eval.md
	$(PY) scripts/eval_classify.py --write docs/classifier_eval.md

# ---- running it as a product (docs/ops.md) ---------------------------------------------------
.PHONY: install init doctor run digest
install:          ## pip install -e . (gives you the `pra` command)
	$(PY) -m pip install --quiet -e .
init:             ## guided setup: keys, webhook secret, .env, dashboard steps, then doctor
	$(PY) -m app.main init
doctor:           ## PASS/WARN/FAIL checks; exit 1 on any FAIL
	$(PY) -m app.main doctor
run:              ## one process: webhook receiver :8080 + scheduler + operator view :8000
	$(PY) -m app.main serve
digest:           ## last 24h in plain text (add --post to send it to ALERT_WEBHOOK_URL)
	$(PY) -m app.main digest
