# Orin Federation Network Plan

Documentation review: October 22, 2026. This is a design plan with a partial
HTTP implementation. The implementation status below takes precedence over
proposed endpoints, commands, transport protocols and phase descriptions.

## Implementation status

| Area | Current source status |
| --- | --- |
| Local network registry and public listing | Implemented; lists networks hosted by a known Federation service |
| Token-gated membership | Implemented with member public keys |
| Identity and signed requests | Ed25519, signed work/heartbeats/capability publication, nonce and expiry checks |
| Persistence | Network, membership, token and work records through Memory; local identity and nonce/cache files |
| Work exchange | Explicit HTTP submission through CortexBridge and result polling |
| Execution boundary | Current Cortex direct WorkPacket ingress accepts LLM work only |
| Result sanitization | Implemented for backend identity, exact models and runtime metadata according to policy |
| Global/decentralized discovery | Proposed; local listing is not peer-to-peer network discovery |
| Automatic federated routing | Proposed; remote descriptors are not normal Planner candidates yet |
| Independent remote-agent delegation | Future work; an inference call is not agent delegation |
| Administrative authorization | Incomplete for network/token creation and capability refresh |
| Polling authorization | Policy checks exist; the current GET route does not authenticate signed members |
| Quotas/concurrency | Policy fields are not proof of enforced limits; hardening remains |

The supplied June 5, 2026 checkpoint records a completed signed submit/poll test
with sanitized inference output. It is dated historical evidence, not a current
regression result. Re-run against the selected commit, image digests,
configuration and models before claiming current end-to-end success.

Implemented command handlers include `/network_list`, `/network_show`,
`/network_create`, `/network_token_create` and
`/network_capabilities_refresh`. Join/member/leave commands shown later are
proposals. Inspect `/commands` for the deployed command inventory.

The current path is FederationClient -> HTTP Federation -> membership/signature/
nonce/policy checks -> packet sanitization -> CortexBridge -> Cortex LLM work
ingress -> RouterService/backend -> polling and sanitized result.
It does not expose Cortex goals, Memory, arbitrary slash commands or host control.

See [current architecture](../orin-cluster-current-architecture-and-operations.md)
and [security boundaries](../SECURITY.md).

## Purpose

The goal is to build a public Orin overlay network where anyone who installs Orin can discover available FederationNetworks, join them with the correct access token, and exchange work with other members through the Federation boundary.

Federation is the cross-environment capability exchange boundary. Explicitly
configured external inference providers are a separate outbound path governed by
Cortex privacy policy. Cortex, LLM, memory, tool services, Kubernetes resources, deployment settings, slash commands, node details, and internal service metadata remain private.

The network should be free as in no mandatory paid hosting. The cost of discovery, relay, metadata replication, and work execution is distributed across participating Orin nodes.

## Core Idea

Every Orin install can become a Federation node.

Federation handles:

- public network discovery
- token-gated joining
- member identity
- signed work exchange
- policy enforcement
- capability advertisement
- result exchange

Cortex handles:

- local reasoning
- routing
- internal backend selection
- local WorkPacket execution

## Public and Private Boundary

### Publicly visible

- FederationNetwork slug
- network name
- description
- join mode
- owner/admission public identity
- sanitized capability summaries
- protocol version
- optional member count

### Never publicly exposed

- Cortex URL
- LLM service URL
- Memory service URL
- Kubernetes services
- pod names
- node IPs
- SSH details
- deployment configuration
- model file paths
- memory contents
- slash commands
- backend/load/unload operations

Federation is the membrane between the outside world and the internal Orin cluster.

## FederationNetwork Concept

Externally, avoid the term Kubernetes namespace. The public concept should be:

```text
FederationNetwork
```

A FederationNetwork is a public or private network that Orin clusters can discover and join.

Suggested model:

```python
class FederationNetwork(BaseModel):
    network_id: str
    slug: str
    name: str
    description: str | None = None

    visibility: Literal["public", "unlisted", "private"] = "public"
    join_mode: Literal["token", "approval", "open", "closed"] = "token"

    owner_cluster_id: str
    policy: NetworkPolicy

    advertised_capabilities: list[CapabilitySummary] = []
    member_count: int = 0

    created_at: datetime
    updated_at: datetime
```

## Join-Token Model

Access tokens are bootstrap credentials only. They should not be reused forever.

```python
class JoinToken(BaseModel):
    token_id: str
    network_id: str

    token_hash: str
    scopes: list[str]

    max_uses: int | None = 1
    used_count: int = 0

    expires_at: datetime | None = None
    created_by_cluster_id: str

    status: Literal["active", "revoked", "expired"] = "active"
```

After a successful join, the new cluster becomes a member with its own identity.

```python
class NetworkMember(BaseModel):
    network_id: str
    cluster_id: str
    public_key: str

    role: Literal["owner", "admin", "member", "guest"] = "member"
    status: Literal["active", "disabled", "revoked", "pending"] = "active"

    allowed_work_types: list[str] = ["llm", "tool"]
    allowed_operations: list[str] = ["chat", "summarize", "analyze"]

    joined_at: datetime
    last_seen_at: datetime | None = None
```

## Intended Join Flow

```text
1. User installs Orin.
2. Orin starts its Federation node.
3. Federation discovers public FederationNetworks.
4. User lists available networks.
5. User chooses a network.
6. User receives an access token from the network owner/admin.
7. User runs network join with the token.
8. Local Federation contacts an admission peer.
9. Admission peer validates the token.
10. The new cluster public key is registered as a member.
11. Future member mutations and work submissions use signed requests.
    Result polling still needs member authentication in the current HTTP MVP.
```

The access token is only a bootstrap credential. The long-term credential is the cluster keypair.

## Federated Work Format

External peers should not send raw internal WorkPackets directly. They should send signed envelopes.

```python
class FederatedWorkEnvelope(BaseModel):
    federation_version: str = "v1"

    network_id: str
    origin_cluster_id: str
    target_cluster_id: str | None = None

    request_id: str
    issued_at: datetime
    expires_at: datetime
    nonce: str

    packet: WorkPacket
    signature: str
```

On receive, Federation must:

```text
1. identify the origin cluster
2. verify network membership
3. verify signature
4. verify nonce and expiry
5. check network policy
6. check member policy
7. enforce quotas and payload limits
8. strip forbidden fields
9. add federation origin metadata
10. submit allowed work through CortexBridge to Cortex's LLM-only WorkPacket ingress
```

## Network Policy

Default policy should be restrictive.

### Allowed by default

- llm.chat
- llm.summarize
- llm.classify
- llm.extract
- llm.analyze
- tool.search, later

### Blocked by default

- memory.*
- deployment.*
- backend/load/unload
- slash commands
- node management
- SSH operations
- k3s/Kubernetes operations
- direct Cortex command execution

Suggested policy model:

```python
class NetworkPolicy(BaseModel):
    allow_remote_work_submission: bool = True
    allow_remote_result_polling: bool = True
    allow_member_capability_publish: bool = True

    allowed_work_types: list[str] = ["llm", "tool"]
    denied_work_types: list[str] = ["memory", "deployment"]

    allowed_operations: list[str] = [
        "chat",
        "summarize",
        "classify",
        "extract",
        "analyze",
        "search",
    ]

    max_payload_bytes: int = 1_000_000
    max_context_tokens: int = 4096
    max_result_tokens: int = 1024
    max_concurrent_jobs_per_member: int = 1

    expose_member_list: bool = False
    expose_exact_models: bool = False
    expose_runtime_metadata: bool = False
```

## Routing Integration

Proposed routing integration: joined remote members would appear to Cortex as
federated backend candidates. This is not implemented in normal Cortex routing.

```python
BackendDescriptor(
    name="fed:<network>:<cluster>:llm",
    role="llm",
    kind="federated",
    visibility="external",
    capabilities=["chat", "summarize"],
    modalities=["text"],
    url="federation://<network>/<cluster>",
)
```

Routing logic:

```text
1. Prefer local backend when available.
2. If local capacity is unavailable or policy allows remote routing:
   - check joined FederationNetworks
   - select a remote member with matching capabilities
   - wrap WorkPacket in FederatedWorkEnvelope
   - send through FederationClient
3. Remote Federation verifies and executes through its own Cortex.
4. Result is returned through Federation.
```

Federated backends should not use the normal BackendClient. They need a dedicated FederationClient or federated backend adapter.

## Network Architecture

Each Orin install should include:

- Cortex
- LLM / Memory / Tool backends
- Federation app

Federation app responsibilities:

- local federation API
- network registry
- member registry
- join-token handling
- capability publishing
- policy enforcement
- signed envelope verification
- external transport
- Cortex RouterService bridge

Global shape:

```text
Orin node A
  Federation
    signed envelope
      |
      v
Orin node B
  Federation
    verify membership/policy
      |
      v
  Cortex RouterService
      |
      v
  Local backend execution
```

## Bootstrap and Relay Model

To avoid mandatory paid infrastructure, the network should use volunteer/community bootstrap and relay nodes.

Bootstrap nodes help new installs discover the network. They are not trusted with work contents or private data.

```text
ship Orin with a default bootstrap peer list
allow users to add more bootstrap peers
allow stable Orin nodes to volunteer as bootstrap nodes
allow LAN discovery for local clusters
allow relay fallback when direct connectivity fails
```

Relays should be fallback infrastructure, not the main execution path.

```text
direct connection preferred
hole punching attempted when needed
relay fallback for difficult NAT/firewall cases
large transfers should avoid relays where possible
```

## Proposed source additions (not a current file inventory)

```text
src/federation/
  app.py
  p2p_node.py
  network_registry.py
  manifests.py
  join_tokens.py
  members.py
  envelopes.py
  policy.py
  federation_client.py
```

## Proposed endpoint contract (see implementation status)

```text
GET  /federation/networks
GET  /federation/networks/{slug}
POST /federation/networks/{slug}/join
POST /federation/networks/{network_id}/capabilities
POST /federation/networks/{network_id}/work
GET  /federation/networks/{network_id}/work/{work_id}
```

## Proposed peer protocol operations (not implemented)

```text
/orin/federation/hello/1.0.0
/orin/federation/network-manifest/1.0.0
/orin/federation/join/1.0.0
/orin/federation/capabilities/1.0.0
/orin/federation/work/1.0.0
/orin/federation/result/1.0.0
```

## CLI design examples (includes unimplemented commands)

```text
/cd Federation
/network_list
/network_create public-text-inference --public --join token
/network_token_create public-text-inference --uses 10 --expires 7d
/network_join public-text-inference --token <token>
/network_members public-text-inference
/network_leave public-text-inference
```

## Design phases and remaining work

### Phase 1: Local Federation skeleton (implemented foundation)

```text
Create federation app
Create network/member/token models
Create local registry
Create signed envelope model
Create policy checker
Expose local endpoints
Historical skeleton milestone; explicit HTTP LLM work now exists
```

### Phase 2: Discovery and joining (local HTTP implemented; global discovery pending)

```text
Start Federation node identity
Discover public FederationNetworks
Implement token redemption
Register member public keys
Persist memberships
Publish sanitized capabilities
```

### Phase 3: Federated WorkPacket exchange (HTTP LLM path implemented)

```text
Add FederationClient
Send signed FederatedWorkEnvelope
Verify remote envelope
Apply policy
Submit allowed packet through CortexBridge to Cortex LLM work ingress
Return WorkResult
```

### Phase 4: Routing integration (pending)

```text
Represent joined remote members as federated backend descriptors
Let Cortex route to local or federated backends
Existing: explicit HTTP result polling
Pending: routed result integration and streaming
Add quota and timeout enforcement
```

### Phase 5: Volunteer bootstrap/relay hardening (pending)

```text
Default bootstrap peer list
Community relay support
Rate limits
Abuse protection
Revocation lists
Network health reporting
Protocol version negotiation
```

## Final Design Principle

The network should be:

- publicly discoverable
- free to join for Orin installs
- token-gated per network
- signed after membership
- policy-controlled
- federation-only externally
- private internally
- distributed across participating Orin nodes

In one sentence:

> Orin Federation should be a public, no-pay overlay network where every Orin install can discover FederationNetworks, join authorized networks with tokens, and exchange signed policy-limited WorkPackets through Federation without exposing any internal cluster structure.
