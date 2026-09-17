**Introducing `describe_graph`**

`describe_graph` is HarborRAG graph's static "schema lookup" tool: it takes no arguments, requires no tenant selection, and never executes a query or touches actual data. It returns the graph's contract — schema versions (structural/semantic/ontology), node kinds (Document, Chunk, Entity, ...), projected relation types (PARENT_OF, CONTAINS, RELATES, ...), and the property catalog for each node kind. Its purpose is to let the MCP client (e.g. Claude Desktop) learn the graph structure first, then call the right query tools such as `graph_triplet_search`, `graph_path_search`, `graph_subgraph_search` — separating schema discovery from execution keeps both sides easier to test and predict.

1. Show the tool list in the MCP client (Claude Desktop)
"I'm not familiar with the current schema of the knowledge graph (node labels, relationship types, and properties). Could you clarify the graph structure — specifically what labels and relationships like PARENT_OF and CONTAINS represent, and how they connect?"


2. Ingested data: 3 tenants corresponding to 3 connections: Local, Jira, Confluence. Show on both Vector DB and Graph DB

3. Run 3 queries and show the expected output:
a. "In the Local tenant, What is the purpose of PaymentsAPI runbook in Local tenant?"
Expected output:
- Found in PaymentsAPI Runbook.md, under the Purpose section

b. Combined (a query that requires calling both the vector search tool and the graph tool)
"In Jira tenant, what is PaymentsAPI rate limiting bug talk about? What space that ticket belongs to? Give me all ticket belongs to this space?"
-> result

4. Try a query with no matching data or the wrong tenant -> prove it doesn't call tools blindly
"In MCP_E2E testing jira space, what is METJ-10?"

5. Conclusion

This demo confirms the MCP tools (vector search + graph tool) are successfully plugged into the MCP client (Claude Desktop):

- The client correctly discovers all tools and their schemas, even asking for clarification when the graph schema is unclear.
- It calls the right tool(s) for each case — single vector search, or combined vector + graph traversal — scoped correctly to the right tenant across both Vector DB and Graph DB.
- It avoids calling tools blindly: for out-of-scope or wrong-tenant queries, it correctly reports no data found instead of hallucinating.

=> The full MCP integration lifecycle — tool discovery, context-aware tool calling, result handling, and knowing when call a tool — works end-to-end, confirming HarborRAG's MCP server is fully compatible with the MCP client.
