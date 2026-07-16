# Live provider smoke tests

These tests call real chat, embedding, and reranking providers through the public Harbor clients. They do not use mocked LiteLLM functions.

## Setup

Copy the example file and add real credentials and model identifiers:

```bash
cp .env.llm.example .env
```

At minimum, configure the variables for each smoke test you intend to run. The default command runs all three clients, so all three groups must be configured.

## Run

```bash
pytest tests/smoke/models --run-smoke --no-cov
```

`--run-smoke` prevents accidental paid or external requests during the normal test suite. `--no-cov` disables the repository-wide unit coverage gate for this intentionally small live suite.

To use another dotenv file:

```bash
HARBOR_SMOKE_ENV_FILE=/secure/path/provider-smoke.env \
  pytest tests/smoke/models --run-smoke --no-cov
```

The smoke suite skips providers whose required model/provider variables or `.env` file are missing. API keys and authorization headers are never printed by the tests.
