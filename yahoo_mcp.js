const { Server } = require("@modelcontextprotocol/sdk/server/index.js");
const { StdioServerTransport } = require("@modelcontextprotocol/sdk/server/stdio.js");
const { CallToolRequestSchema, ListToolsRequestSchema } = require("@modelcontextprotocol/sdk/types.js");
const { execSync } = require("child_process");

const server = new Server(
  { name: "yahoo-mcp", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "yahoo_quote",
      description: "Get quote for a stock from Yahoo Finance (15 min delay)",
      inputSchema: {
        type: "object",
        properties: {
          symbol: { type: "string", description: "e.g. RELIANCE-EQ or NIFTY50-INDEX" }
        },
        required: ["symbol"]
      }
    },
    {
      name: "yahoo_history",
      description: "Get long-term historical OHLC from Yahoo Finance — up to 5 years",
      inputSchema: {
        type: "object",
        properties: {
          symbol: { type: "string", description: "e.g. RELIANCE-EQ or NIFTY50-INDEX" },
          days: { type: "integer", description: "How many days back (up to 1825 for 5 years)" },
          resolution: { type: "string", description: "D=daily, W=weekly, 5=5min, 15=15min, 60=hourly" }
        },
        required: ["symbol", "days"]
      }
    }
  ]
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    let cmd;
    if (name === "yahoo_quote") {
      cmd = `python "C:\\Users\\Administrator\\Desktop\\quant_project\\yahoo_quote.py" "${args.symbol}"`;
    } else if (name === "yahoo_history") {
      const res = args.resolution || "D";
      cmd = `python "C:\\Users\\Administrator\\Desktop\\quant_project\\yahoo_hist.py" "${args.symbol}" ${args.days} ${res}`;
    } else {
      throw new Error(`Unknown tool: ${name}`);
    }

    const output = execSync(cmd, { timeout: 30000 }).toString();
    return { content: [{ type: "text", text: output }] };

  } catch (err) {
    return { content: [{ type: "text", text: JSON.stringify({ error: err.message }) }] };
  }
});

const transport = new StdioServerTransport();
server.connect(transport);