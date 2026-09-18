# Complete Environment Reference

Comprehensive list of all environment variables available in Open Notebook.

---

## API Configuration

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `API_URL` | No | Auto-detected | URL where frontend reaches API (e.g., http://localhost:5055) |
| `INTERNAL_API_URL` | No | http://localhost:5055 | Internal API URL for Next.js server-side proxying |
| `API_CLIENT_TIMEOUT` | No | 300 | Client timeout in seconds (how long to wait for API response) |
| `OPEN_NOTEBOOK_PASSWORD` | No | None | Password to protect Open Notebook instance |
| `OPEN_NOTEBOOK_ENCRYPTION_KEY` | **Yes** | None | Secret string to encrypt credentials stored in database (any string works). **Required** for the credential system. Supports Docker secrets via `_FILE` suffix. |
| `FRONTEND_BIND_HOST` | No | `0.0.0.0` (in Docker) | Network interface for Next.js to bind to. Default `0.0.0.0` ensures accessibility from reverse proxies. (Replaces `HOSTNAME`, which container runtimes such as Podman override with the container/pod hostname, causing Next.js to bind to the wrong address) |
| `API_HOST` | No | `0.0.0.0` (in Docker) | Network interface for the API (uvicorn) to bind to. Set to `::` for IPv6 dual-stack environments (listens on IPv6 and, on Linux defaults, IPv4 too) |
| `OPEN_NOTEBOOK_MAX_UPLOAD_SIZE_MB` | No | 100 | Maximum request body size (in MB) the API will accept, enforced before auth/routing. Raise this if you need to upload larger audio/video files. A fronting reverse proxy's own limit (e.g. nginx `client_max_body_size`) still applies and should be raised to match. |

> **Important**: `OPEN_NOTEBOOK_ENCRYPTION_KEY` is required for storing AI provider credentials via the Settings UI. Without it, you cannot save credentials. If you change or lose this key, all stored credentials become unreadable.

---

## Database: SurrealDB

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `SURREAL_URL` | Yes | ws://surrealdb:8000/rpc | SurrealDB WebSocket connection URL |
| `SURREAL_USER` | Yes | root | SurrealDB username |
| `SURREAL_PASSWORD` | Yes | root | SurrealDB password |
| `SURREAL_NAMESPACE` | Yes | open_notebook | SurrealDB namespace |
| `SURREAL_DATABASE` | Yes | open_notebook | SurrealDB database name |

---

## Job Queue (Celery + Redis)

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `REDIS_URL` | Yes | redis://localhost:6379/0 | Celery broker and result backend. **Background jobs queue forever with no error if this is unreachable.** `docker-compose.yml` points it at the internal `redis` service. |
| `FLOWER_BASIC_AUTH` | No | *(none)* | `user:password` for the Flower monitoring UI. Flower ships with **no authentication** and can read task arguments and revoke jobs, so it is published on `127.0.0.1:5555` only — set this before exposing it on a network. |
| `FLOWER_UNAUTHENTICATED_API` | No | false | Allow Flower's JSON API without credentials. Leave off unless you are scripting against it. |

> **Retry policy is not configurable by environment.** Each task declares its own
> budget in code (`commands/*.py`) because the right policy differs per task:
> `process_source` retries up to 15 times to outlast SurrealDB transaction
> conflicts, while `generate_podcast` never retries because a retry would produce
> a duplicate episode. Validation and configuration errors are never retried.
> See [ADR-009](../7-DEVELOPMENT/decisions/ADR-009-celery-queue.md).

---

## Worker: Concurrency

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `OPEN_NOTEBOOK_WORKER_MAX_TASKS` | No | 5 | Maximum number of background tasks (source processing, embeddings, podcasts) the worker runs concurrently. Passed to Celery as `--concurrency` at launch. Set to `1` for **sequential processing** on single-GPU or local-LLM setups, where parallel requests overload the model and trigger rate limits. |
| `OPEN_NOTEBOOK_JOB_STALE_MINUTES` | No | 30 | How long a queued/running job may go without any update before the API reports it as `unknown` instead of in-progress. A worker killed mid-task (container restart, OOM) never writes its terminal status, so without this its job row counts as in-progress forever. Nothing is written to the database — a job that was merely quiet reappears as active as soon as it reports again — so raise this only if you have tasks that legitimately go silent for longer. |

> The worker runs Celery's **threads** pool: one process running N tasks at once,
> which keeps memory flat as you raise this number. Switching to `--pool=prefork`
> gives real parallelism and lets you hard-kill a running task, at the cost of N
> full copies of the import footprint.

> **Read at worker launch, from the process environment.** In Docker this comes from the container environment — set it under `environment:` in `docker-compose.yml` (or your orchestrator). For local `make worker-start` / `dev-init.sh`, export it in your shell (e.g. `export OPEN_NOTEBOOK_WORKER_MAX_TASKS=1`) — it is consumed by the shell before the app loads `.env`, so a value placed only in `.env` will not apply to these local launch paths.

---

## LLM Timeouts

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `ESPERANTO_LLM_TIMEOUT` | No | 60 | LLM inference timeout in seconds |
| `ESPERANTO_SSL_VERIFY` | No | true | Verify SSL certificates (false = development only) |
| `ESPERANTO_SSL_CA_BUNDLE` | No | None | Path to custom CA certificate bundle |

---

## Embeddings

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `OPEN_NOTEBOOK_EMBEDDING_BATCH_SIZE` | No | 50 | Number of texts sent per embedding batch. Lower this for CPU-only or stricter OpenAI-compatible embedding providers. |
| `OPEN_NOTEBOOK_MIN_CHUNK_SIZE` | No | 5 | Minimum chunk size in tokens. Chunks below this threshold are dropped before embedding to avoid degenerate single-character fragments that some providers (e.g. llama.cpp) return null embeddings for. Set to `0` to disable filtering. |

---

## API / CORS

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `CORS_ORIGINS` | No | `*` | Comma-separated list of origins allowed to call the API (e.g. `https://app.example.com,https://www.example.com`). Default `*` accepts any origin; **for production, set this explicitly to your frontend origin(s)**. Changes require an API restart. The API logs a warning on startup when unset. |

**When to change this**:
- You access the UI at a custom domain (reverse proxy, HTTPS, public deployment).
- The frontend runs on a different port than `3000`.
- You serve the frontend from a different host than the API (e.g. CDN).

Example for a production deployment behind a reverse proxy:

```bash
CORS_ORIGINS=https://notebook.example.com
```

---

## Text-to-Speech (TTS)

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `TTS_BATCH_SIZE` | No | 5 | Concurrent TTS requests (1-5, depends on provider) |
| `ESPERANTO_TTS_TIMEOUT` | No | 300 | Text-to-speech request timeout in seconds (passed through to Esperanto). Increase it for slow or self-hosted TTS providers that take longer than 5 minutes to synthesize a segment, otherwise long podcast segments can fail with a timeout. |
| `PODCAST_RETRY_MAX_ATTEMPTS` | No | 3 | Attempts per outline/transcript/TTS call. A model that returns malformed JSON is re-sampled; raise this if your provider is flaky. |
| `PODCAST_RETRY_WAIT_MULTIPLIER` | No | 2 | Exponential backoff multiplier in seconds between those attempts (capped at 30s). |

---

## Content Extraction

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `FIRECRAWL_API_KEY` | No | None | Firecrawl API key for advanced web scraping |
| `FIRECRAWL_API_URL` | No | None | Base URL of a self-hosted Firecrawl instance (use instead of the hosted service) |
| `CCORE_FIRECRAWL_PROXY` | No | `auto` | Firecrawl proxy mode to bypass anti-bot protection: `basic`, `stealth`, or `auto` |
| `CCORE_FIRECRAWL_WAIT_FOR` | No | `3000` | Milliseconds Firecrawl waits for JavaScript to render before capturing the page |
| `JINA_API_KEY` | No | None | Jina AI API key for web extraction |
| `CRAWL4AI_API_URL` | No | None | Base URL of a remote Crawl4AI server. Set this to use Crawl4AI without a local install |
| `CRAWL4AI_API_TOKEN` | No | None | Bearer token sent as `Authorization: Bearer …` to the remote Crawl4AI server. Required by Crawl4AI Docker ≥ 0.9.0, which rejects unauthenticated external connections by default. Ignored when unset |

### Optional heavy runtimes (installed on first startup)

These are **off by default** to keep the image lean. Setting one to `true` makes the container install that runtime the first time it starts (downloads are cached on the `/app/data` volume, so only the first boot is slow). See [Content Processing Engines → Optional engines](../3-USER-GUIDE/content-processing-engines.md#optional-engines-docling--crawl4ai).

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `OPEN_NOTEBOOK_ENABLE_DOCLING` | No | `false` | Install Docling on first startup: unlocks the `docling` document engine, the OCR toggle and image sources. Pulls a large ML stack. |
| `OPEN_NOTEBOOK_ENABLE_CRAWL4AI` | No | `false` | Install the local Crawl4AI runtime + a Chromium browser on first startup: unlocks the `crawl4ai` URL engine. Not needed if `CRAWL4AI_API_URL` is set. |

**Setup:**
- Firecrawl: https://firecrawl.dev/
- Jina: https://jina.ai/
- Crawl4AI: https://github.com/unclecode/crawl4ai

The `CCORE_FIRECRAWL_*` variables are passed straight through to the content-core library (its settings are prefixed with `CCORE_`); Open Notebook itself doesn't read them. See [Content Processing Engines](../3-USER-GUIDE/content-processing-engines.md) for how these engines are selected in the UI.

---

## Network / Proxy

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `HTTP_PROXY` | No | None | HTTP proxy URL for outbound HTTP requests |
| `HTTPS_PROXY` | No | None | HTTPS proxy URL for outbound HTTPS requests |
| `NO_PROXY` | No | None | Comma-separated list of hosts to bypass proxy (must include the internal DB hosts — see below) |

Route all outbound HTTP requests through a proxy server. Useful for corporate/firewalled environments.

> **Important:** `NO_PROXY` must list the internal SurrealDB hosts — `host.docker.internal` (Docker) and `surrealdb` (the compose service name). The SurrealDB SDK connects over a websocket, and `websockets` 15.0+ tunnels even `ws://` connections through a configured proxy, which then rejects the internal host with **HTTP 403** and prevents the API and worker from starting. Open Notebook injects `host.docker.internal,surrealdb,localhost,127.0.0.1` into `NO_PROXY` automatically at startup as a safety net, but you should still set them explicitly.

The underlying libraries (esperanto, content-core) automatically detect proxy settings from these standard environment variables.

**Affects:**
- AI provider API calls (OpenAI, Anthropic, Google, Groq, etc.)
- Content extraction from URLs (web scraping, YouTube transcripts)
- Podcast generation (LLM and TTS provider calls)

**Format:** `http://[user:pass@]host:port` or `https://[user:pass@]host:port`

**Examples:**
```bash
# Basic proxy
HTTP_PROXY=http://proxy.corp.com:8080
HTTPS_PROXY=http://proxy.corp.com:8080

# Authenticated proxy
HTTP_PROXY=http://user:password@proxy.corp.com:8080
HTTPS_PROXY=http://user:password@proxy.corp.com:8080

# Bypass proxy for local hosts (include the internal DB hosts!)
NO_PROXY=localhost,127.0.0.1,host.docker.internal,surrealdb,redis,.local
```

---

## Debugging & Monitoring

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `LANGCHAIN_TRACING_V2` | No | false | Enable LangSmith tracing |
| `LANGCHAIN_ENDPOINT` | No | https://api.smith.langchain.com | LangSmith endpoint |
| `LANGCHAIN_API_KEY` | No | None | LangSmith API key |
| `LANGCHAIN_PROJECT` | No | Open Notebook | LangSmith project name |

**Setup:** https://smith.langchain.com/

---

## Environment Variables by Use Case

### Minimal Setup (New Installation)
```
OPEN_NOTEBOOK_ENCRYPTION_KEY=my-secret-key
SURREAL_URL=ws://surrealdb:8000/rpc
SURREAL_USER=root
SURREAL_PASSWORD=password
SURREAL_NAMESPACE=open_notebook
SURREAL_DATABASE=open_notebook
```
Then configure AI providers via **Manage → Models** in the browser.

### Production Deployment
```
OPEN_NOTEBOOK_ENCRYPTION_KEY=your-strong-secret-key
OPEN_NOTEBOOK_PASSWORD=your-secure-password
API_URL=https://mynotebook.example.com
SURREAL_USER=production_user
SURREAL_PASSWORD=secure_password
```

### Self-Hosted Behind Reverse Proxy
```
OPEN_NOTEBOOK_ENCRYPTION_KEY=your-secret-key
API_URL=https://mynotebook.example.com
```

### Corporate Environment (Behind Proxy)
```
OPEN_NOTEBOOK_ENCRYPTION_KEY=your-secret-key
HTTP_PROXY=http://proxy.corp.com:8080
HTTPS_PROXY=http://proxy.corp.com:8080
NO_PROXY=localhost,127.0.0.1,host.docker.internal,surrealdb,redis,.local
```

### High-Performance Deployment
```
OPEN_NOTEBOOK_ENCRYPTION_KEY=your-secret-key
OPEN_NOTEBOOK_WORKER_MAX_TASKS=10
TTS_BATCH_SIZE=5
API_CLIENT_TIMEOUT=600
```

### Debugging
```
OPEN_NOTEBOOK_ENCRYPTION_KEY=your-secret-key
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=your-key
```

---

## Validation

Check if a variable is set:

```bash
# Check single variable
echo $OPEN_NOTEBOOK_ENCRYPTION_KEY

# Check multiple
env | grep -E "OPEN_NOTEBOOK|API_URL"

# Print all config
env | grep -E "^[A-Z_]+=" | sort
```

---

## Notes

- **Case-sensitive:** `OPEN_NOTEBOOK_ENCRYPTION_KEY` ≠ `open_notebook_encryption_key`
- **No spaces:** `OPEN_NOTEBOOK_ENCRYPTION_KEY=my-key` not `OPEN_NOTEBOOK_ENCRYPTION_KEY = my-key`
- **Quote values:** Use quotes for values with spaces: `API_URL="http://my server:5055"`
- **Restart required:** Changes take effect after restarting services
- **Secrets:** Don't commit encryption keys or passwords to git
- **AI Providers:** Configure via **Manage → Models** in the browser. The provider env vars listed under [Legacy](#legacy-ai-provider-environment-variables-deprecated) are a deprecated fallback
- **Migration:** Use Settings UI to migrate existing env vars to the credential system. See [API Configuration](../3-USER-GUIDE/api-configuration.md#migrating-from-environment-variables)

---

## Quick Setup Checklist

- [ ] Set `OPEN_NOTEBOOK_ENCRYPTION_KEY` in docker-compose.yml
- [ ] Set database credentials (`SURREAL_*`)
- [ ] Start services
- [ ] Open browser → Go to **Manage → Models**
- [ ] **Add Credential** for your AI provider
- [ ] **Test Connection** to verify
- [ ] **Discover & Register Models**
- [ ] Set `API_URL` if behind reverse proxy
- [ ] Change `SURREAL_PASSWORD` in production
- [ ] Try a test chat

Done!

---

## Legacy: AI Provider Environment Variables (Deprecated)

> **Deprecated**: The following AI provider environment variables are a deprecated fallback. They still work today (the runtime reads the database first and falls back to the environment), but there is no guarantee they keep working in future releases, and new automation should not be built on them. Configure providers via **Manage → Models** instead.

Why the UI is the source of truth: credentials in the database are encrypted at rest (via `OPEN_NOTEBOOK_ENCRYPTION_KEY`), can be added or rotated at runtime without restarts, and support multiple credentials per provider — none of which a single env var can express.

**Headless, CI/CD and Docker deployments:** a declarative provisioning contract over the credentials API (a file describing providers and credentials, with `${VAR}`-style references resolved from the environment) is being designed in [Discussion #765](https://github.com/lfnovo/open-notebook/discussions/765). Until it ships, the env fallback is the only unattended path — use it knowing it is deprecated.

If you have these variables configured from a previous installation, click the **Migrate to Database** button in **Manage → Models** to import them into the credential system, then remove them from your configuration.

| Variable | Provider | Replacement |
|----------|----------|-------------|
| `OPENAI_API_KEY` | OpenAI | Manage → Models → Add OpenAI Credential |
| `ANTHROPIC_API_KEY` | Anthropic | Manage → Models → Add Anthropic Credential |
| `GOOGLE_API_KEY` | Google Gemini | Manage → Models → Add Google Credential |
| `GEMINI_API_BASE_URL` | Google Gemini | Configure in Google Gemini credential |
| `VERTEX_PROJECT` | Vertex AI | Manage → Models → Add Vertex AI Credential |
| `VERTEX_LOCATION` | Vertex AI | Configure in Vertex AI credential |
| `GOOGLE_APPLICATION_CREDENTIALS` | Vertex AI | Configure in Vertex AI credential |
| `GROQ_API_KEY` | Groq | Manage → Models → Add Groq Credential |
| `MISTRAL_API_KEY` | Mistral | Manage → Models → Add Mistral Credential |
| `DEEPSEEK_API_KEY` | DeepSeek | Manage → Models → Add DeepSeek Credential |
| `XAI_API_KEY` | xAI | Manage → Models → Add xAI Credential |
| `OLLAMA_API_BASE` | Ollama | Manage → Models → Add Ollama Credential |
| `OMLX_API_BASE` | oMLX | Manage → Models → Add oMLX Credential |
| `OMLX_API_KEY` | oMLX | Optional; only if oMLX was started with `--api-key` |
| `OPENROUTER_API_KEY` | OpenRouter | Manage → Models → Add OpenRouter Credential |
| `OPENROUTER_BASE_URL` | OpenRouter | Configure in OpenRouter credential |
| `VOYAGE_API_KEY` | Voyage AI | Manage → Models → Add Voyage AI Credential |
| `ELEVENLABS_API_KEY` | ElevenLabs | Manage → Models → Add ElevenLabs Credential |
| `OPENAI_COMPATIBLE_BASE_URL` | OpenAI-Compatible | Manage → Models → Add OpenAI-Compatible Credential |
| `OPENAI_COMPATIBLE_API_KEY` | OpenAI-Compatible | Configure in OpenAI-Compatible credential |
| `OPENAI_COMPATIBLE_BASE_URL_LLM` | OpenAI-Compatible | Configure per-service URL in credential |
| `OPENAI_COMPATIBLE_API_KEY_LLM` | OpenAI-Compatible | Configure per-service key in credential |
| `OPENAI_COMPATIBLE_BASE_URL_EMBEDDING` | OpenAI-Compatible | Configure per-service URL in credential |
| `OPENAI_COMPATIBLE_API_KEY_EMBEDDING` | OpenAI-Compatible | Configure per-service key in credential |
| `OPENAI_COMPATIBLE_BASE_URL_STT` | OpenAI-Compatible | Configure per-service URL in credential |
| `OPENAI_COMPATIBLE_API_KEY_STT` | OpenAI-Compatible | Configure per-service key in credential |
| `OPENAI_COMPATIBLE_BASE_URL_TTS` | OpenAI-Compatible | Configure per-service URL in credential |
| `OPENAI_COMPATIBLE_API_KEY_TTS` | OpenAI-Compatible | Configure per-service key in credential |
| `DASHSCOPE_API_KEY` | DashScope (Qwen) | Manage → Models → Add DashScope Credential |
| `MINIMAX_API_KEY` | MiniMax | Manage → Models → Add MiniMax Credential |
| `NOVITA_API_KEY` | Novita | Manage → Models → Add Novita Credential |
| `PPQ_API_KEY` | PayPerQ (PPQ) | Manage → Models → Add PayPerQ Credential |
| `COHERE_API_KEY` | Cohere | Manage → Models → Add Cohere Credential |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI | Manage → Models → Add Azure OpenAI Credential |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI | Configure in Azure OpenAI credential |
| `AZURE_OPENAI_API_VERSION` | Azure OpenAI | Configure in Azure OpenAI credential |
| `AZURE_OPENAI_API_KEY_LLM` | Azure OpenAI | Configure per-service in credential |
| `AZURE_OPENAI_ENDPOINT_LLM` | Azure OpenAI | Configure per-service in credential |
| `AZURE_OPENAI_API_VERSION_LLM` | Azure OpenAI | Configure per-service in credential |
| `AZURE_OPENAI_API_KEY_EMBEDDING` | Azure OpenAI | Configure per-service in credential |
| `AZURE_OPENAI_ENDPOINT_EMBEDDING` | Azure OpenAI | Configure per-service in credential |
| `AZURE_OPENAI_API_VERSION_EMBEDDING` | Azure OpenAI | Configure per-service in credential |
