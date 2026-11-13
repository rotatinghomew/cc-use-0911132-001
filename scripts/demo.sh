set -eu
workspace=$(mktemp -d)
trap 'rm -rf "$workspace"' EXIT
export PYTHONDONTWRITEBYTECODE=1
python3 -m unittest discover -s tests/reorder_sagas -p 'test_*.py' -q
python3 -m unittest discover -s tests -p 'test_analysis_inputs.py' -q
python3 -m unittest discover -s tests -p 'test_basket_insights.py' -q
PYTHONPYCACHEPREFIX="$workspace/cache" python3 -m compileall -q b2b_reorder_network tests
python3 -m b2b_reorder_network.dispatch.cli fixtures/demo.json --database "$workspace/operations.sqlite3" > "$workspace/operations.json"
python3 -m json.tool "$workspace/operations.json"
