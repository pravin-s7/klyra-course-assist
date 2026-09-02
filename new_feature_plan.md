# Klyra: Massive-Scale Agentic SaaS System Design

To evolve Klyra into a production-ready, highly scalable multi-tenant SaaS platform capable of serving millions of users across **any industry**, we need a robust distributed architecture.

## User Review Required

> [!IMPORTANT]
> I have updated the plan to address your specific requirements for **Universal Customization (Any Field)** and **Tiered API Key Rate Limiting**. Please review Sections 2 and 5 carefully. 

---

## 1. Multi-Tenant Architecture & Data Hierarchy

To support teams and individual users safely, we introduce a strict hierarchy:

- **Organization / Tenant**: The billing entity.
- **Workspace**: A specific domain or project (e.g., "Marketing Q3", "Finance Audits").
- **Users**: Individuals with roles (Admin, Member, Viewer).
- **Knowledge Base (Shared)**: Documents uploaded to the workspace.
- **Personal Memory (Unique)**: The AI's memory of past interactions, scoped by `user_id`.

## 2. Universal Customization (Marketing, Finance, Law)

Yes, it is absolutely possible to make this work for **any field**. Instead of Klyra being an "Educational AI", Klyra becomes an **Agent Creation Engine**.

### How to Achieve Universal Customization
To support arbitrary domains, we must completely decouple the code from the domain logic.

1. **Custom Tools via UI (API Connectors)**: 
   Users won't just select from tools you built. They will be able to define their own tools in the UI by providing a REST API endpoint, headers, and a JSON schema. 
   - *Example (Finance):* A user creates a "Fetch Stock Price" tool pointing to the Yahoo Finance API.
   - *Example (Marketing):* A user creates a "Post to Twitter" tool pointing to the X API.
2. **Dynamic Agent Builder**:
   Users define their own Agents in the UI. The database stores:
   - `Agent Name`: (e.g., "Senior Financial Analyst")
   - `Role`: (e.g., "Analyze quarterly earnings reports")
   - `Backstory`: (e.g., "You have 20 years of experience on Wall Street...")
   - `Assigned Tools`: (They select the custom tools they created in step 1).
3. **Dynamic Prompt Assembly**:
   When a user chats, the backend dynamically assembles the `SYSTEM_INSTRUCTION` by querying the DB for the workspace's active agents and tools, regardless of the industry.

## 3. Storage Architecture

Local folders (`docs/`) will not scale. We will migrate to a cloud-native storage triad:

1. **Relational Database (PostgreSQL / SQLAlchemy)**: Stores Users, Workspaces, Custom Agent Definitions, Custom Tool Definitions, API Keys, and Usage Logs.
2. **Object Storage (AWS S3 / MinIO)**: Stores all uploaded files and generated deliverables, organized by custom folder structures defined by the user in the UI.
3. **Vector Database (Qdrant or Pinecone)**:
   - **Shared Knowledge**: When a file is uploaded to S3, a background worker chunks and embeds it, storing the vectors tagged with `workspace_id`.
   - **Personal Memory**: Agentic memory is stored here, strictly filtered by `user_id` so the agent remembers personal preferences without leaking them to the team.

## 4. Compute & Execution Layer (Background Workers)

CrewAI execution is extremely slow and blocking. It **cannot** run in the web server loop.

- **Web Tier (FastAPI)**: Handles HTTP routes, WebSocket connections, and API Key validation.
- **Message Broker (Redis / RabbitMQ)**: Queues generation tasks.
- **Worker Tier (Celery + Redis)**: Scalable worker nodes that consume tasks. When a user clicks "Generate", a Celery worker pulls the dynamic Crew config from Postgres, downloads context from S3, executes the workflow, and streams logs back via WebSockets.

## 5. Monetization, API Keys & Tiered Rate Limiting

To build a true SaaS model where you can issue API keys, enforce limits, and toggle features (like Voice) based on subscription plans, you need an **API Gateway / Middleware pattern**.

### How to Implement Tiered Rate Limiting

1. **Plan Definitions (PostgreSQL)**:
   Create a `SubscriptionPlans` table defining the tiers:
   - `Free Plan`: 50 requests/day, Voice = `False`, Max Model = `gemini-1.5-flash`.
   - `Pro Plan`: 1000 requests/day, Voice = `True`, Max Model = `gemini-1.5-pro`.

2. **API Key Generation**:
   Users generate API keys in their dashboard. The key is hashed and stored in the DB, linked to their Organization's active `SubscriptionPlan`.

3. **FastAPI Middleware (The Gatekeeper)**:
   Every incoming request hits a custom FastAPI Middleware *before* it reaches the endpoints.
   - **Authentication**: Validates the `Authorization: Bearer <API_KEY>` header.
   - **Feature Toggling**: If the user hits the `/api/voice/connect` WebSocket endpoint, the middleware checks the database: `if not user.plan.voice_enabled: return 403 Forbidden`.
   - **Model Routing**: If the user requests a complex task, the middleware checks `user.plan.max_model`. If they request a Pro model on a Free plan, it rejects the request or silently downgrades it to Flash.

4. **Redis-Based Rate Limiting**:
   We use **Redis** (via libraries like `slowapi` or custom Lua scripts) to track usage with ultra-low latency.
   - Every time an API key makes a request, Redis increments a counter: `INCR rate_limit:{api_key}:YYYY-MM-DD`.
   - If the counter exceeds the limit defined in their plan, the middleware returns `429 Too Many Requests`.
   - You can also track **Token Usage** by hooking into Gemini's token response, updating a `tokens_used:{api_key}:YYYY-MM` counter in Redis, and syncing it to Postgres hourly for billing.
