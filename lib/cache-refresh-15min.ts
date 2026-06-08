// /lib/cache-refresh-15min.ts
// Ultra-lightweight: 15-min refresh via API call
// No cron needed — trigger from endpoint request if cache is stale

import { Pool } from "pg";
import scorrQuery from "./scorr-api-wrapper";

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

const CACHE_REFRESH_INTERVAL_MS = 15 * 60 * 1000; // 15 minutes

/**
 * Check if cache needs refresh
 * Returns: { needsRefresh: boolean, lastRefresh: Date | null }
 */
export async function getCacheStatus() {
  try {
    const result = await pool.query(
      "SELECT last_sync FROM cache_metadata WHERE key = 'gvm_cache'"
    );

    if (result.rows.length === 0) {
      return { needsRefresh: true, lastRefresh: null };
    }

    const lastRefresh = new Date(result.rows[0].last_sync);
    const age = Date.now() - lastRefresh.getTime();
    const needsRefresh = age > CACHE_REFRESH_INTERVAL_MS;

    return { needsRefresh, lastRefresh, ageMs: age };
  } catch (error) {
    console.error("Cache status check error:", error);
    return { needsRefresh: true, lastRefresh: null };
  }
}

/**
 * Minimal refresh: Pull top 100 stocks from Scorr, update cache
 * Called automatically when cache is stale (every 15 min)
 * Cost: ~50 tokens per refresh ≈ $0.001
 */
export async function refreshCache(topN: number = 100) {
  console.log(`🔄 Cache refresh (top ${topN} stocks)...`);
  const startTime = Date.now();

  try {
    // Get top N stocks from Scorr MCP
    const result = await scorrQuery({
      type: "gvm",
      stocks: ["TOP_100"], // Your MCP should support this
      threshold: 0,
    });

    const scores = parseGVMScores(result.data, topN);
    console.log(`📊 Got ${scores.length} scores from API`);

    if (scores.length === 0) {
      console.warn("⚠️  No scores returned from API");
      return { success: false, updated: 0 };
    }

    // Bulk upsert into cache
    const client = await pool.connect();
    try {
      await client.query("BEGIN");

      for (const score of scores) {
        await client.query(
          `INSERT INTO gvm_cache (symbol, gvm_score, growth, value, momentum, segment, last_updated)
           VALUES ($1, $2, $3, $4, $5, $6, NOW())
           ON CONFLICT (symbol) DO UPDATE SET
           gvm_score = $2, growth = $3, value = $4, momentum = $5, segment = $6, last_updated = NOW()`,
          [
            score.symbol,
            score.gvm_score,
            score.growth,
            score.value,
            score.momentum,
            score.segment,
          ]
        );
      }

      // Update metadata
      await client.query(
        `UPDATE cache_metadata 
         SET last_sync = NOW(), stock_count = $1, status = 'ready'
         WHERE key = 'gvm_cache'`,
        [scores.length]
      );

      await client.query("COMMIT");

      const duration = Date.now() - startTime;
      console.log(
        `✅ Cache refreshed: ${scores.length} stocks in ${duration}ms | Cost: $${result.tokensUsed.cost.toFixed(4)}`
      );

      return {
        success: true,
        updated: scores.length,
        durationMs: duration,
        costUsd: result.tokensUsed.cost,
      };
    } finally {
      client.release();
    }
  } catch (error) {
    console.error("❌ Cache refresh failed:", error);
    return { success: false, error: error.message };
  }
}

/**
 * Parse Scorr response
 */
function parseGVMScores(data: any, limit: number) {
  if (Array.isArray(data.scores)) {
    return data.scores.slice(0, limit);
  }
  return [];
}

export default { getCacheStatus, refreshCache };
