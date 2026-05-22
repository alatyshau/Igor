import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { initializeDeps, registerAllTools } from "./tools.js";

const server = new McpServer({
  name: "igor-cockpit",
  version: "0.1.0",
});

const deps = await initializeDeps();
registerAllTools(server, deps);

await server.connect(new StdioServerTransport());
