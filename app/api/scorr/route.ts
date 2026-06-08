// /app/api/scorr/route.ts
// Smart routing: Native → Cache → API (in that order)
// Most queries return in <100ms with 0 tokens

import { NextRequest, NextResponse } from "next/server";
import {
  getTopStocksNative,
  filterByThresholdNative,
  getPeerComparison,
  getScoreNative,
} from "@/lib/native-gvm-calc";
import { getCacheStatus, refreshCache } from "@/lib/cache-refresh-15min";
import scorrQuery from "@/lib/scorr-api-wrapper";

type QueryType = "top_stocks" | "filter" | "peer_compare" | "recommendation";

interface QueryRequest {
  type: QueryType;
  segment?: string;
  threshold?: number;
  limit?: number;
  stocks?: string[];
  include_explanation?: boolean;
}

export async function POST(req: NextRequest): Promise<NextResponse<any>> {
  const startTime = Date.now();
  let tokensUsed = 0;
  let apiCallCount = 0;

  try {
    const query: QueryRequest = await req.json();

    // Auto-refresh cache if stale (15-min interval)
    const cacheStatus = await getCacheStatus();
    if (cacheStatus.needsRefresh) {
      console.log("📅 Cache is stale, refreshing...");
      await refreshCache(100);
    }

    let result: any;

    // Route to appropriate native handler
    switch (query.type) {
      case "top_stocks":
        // NATIVE: No API call
        result = await getTopStocksNative(query.limit || 20, query.segment);
        break;

      case "filter":
        // NATIVE: No API call
        if (!query.segment || query.threshold === undefined) {
          return NextResponse.json(
            { error: "segment and threshold required" },
            { status: 400 }
          );
        }
        result = await filterByThresholdNative(query.segment, query.threshold);
        break;

      case "peer_compare":
        // NATIVE: No API call
        if (!query.stocks || !query.segment) {
          return NextResponse.json(
            { error: "stocks and segment required" },
            { status: 400 }
          );
        }
        const comparisons = await Promise.all(
          query.stocks.map((s) => getPeerComparison(s, query.segment!))
        );
        result = comparisons;
        break;

      case "recommendation":
        // HYBRID: Native first, API for explanation
        if (!query.stocks) {
          return NextResponse.json(
            { error: "stocks required" },
            { status: 400 }
          );
        }

        // Get scores natively
        const scores = await Promise.all(
          query.stocks.map((s) =>
            getScoreNative(s).catch(() => ({
              symbol: s,
              scores: { gvm: 0, growth: 0, value: 0, momentum: 0 },
            }))
          )
        );

        // If explanation needed, call API once for all stocks
        if (query.include_explanation) {
          const apiResult = await scorrQuery({
            type: "recommendation",
            stocks: query.stocks,
          });
          result = {
            scores,
            explanation: apiResult.data,
          };
          tokensUsed = apiResult.tokensUsed.input + apiResult.tokensUsed.output;
          apiCallCount = 1;
        } else {
          result = scores;
        }
        break;

      default:
        return NextResponse.json({ error: "Unknown query type" }, { status: 400 });
    }

    const duration = Date.now() - startTime;

    return NextResponse.json({
      type: query.type,
      result,
      meta: {
        api_calls: apiCallCount,
        tokens_used: tokensUsed,
        cost_usd: (tokensUsed * 0.00000001).toFixed(8),
        duration_ms: duration,
        cache_status: cacheStatus.lastRefresh ? "fresh" : "needs_refresh",
      },
      timestamp: new Date().toISOString(),
    });
  } catch (error: any) {
    console.error("Query endpoint error:", error);
    return NextResponse.json(
      { error: error.message || "Internal server error" },
      { status: 500 }
    );
  }
}
