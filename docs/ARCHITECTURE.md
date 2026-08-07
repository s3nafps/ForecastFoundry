# ForecastFoundry runtime boundaries

```text
Polymarket/public providers -> scheduler -> ApplicationServices -> persistence
                                   |              |                |
                                   |              +-> paper pipeline |-> REST/dashboard
                                   |              +-> executor      |-> MCP/CLI
                                   +-> provider-health cache
```

Only the dedicated executor may be configured with an unlocked keystore. MCP
and the web process can inspect evidence, predictions, controls, and paper
state; they cannot sign or submit an order. All state-changing controls pass
through the durable control row and produce an audit event.
