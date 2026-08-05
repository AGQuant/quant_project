# Scorr — Spec Registry Index

Generated 05-Aug-2026 from the live `session_log` table (333 doctrine entries across
`spec_locked`, `canonical_spec`, `decision`, `framework`, `trading_learnings`).

**How to use this.** This is an INDEX, not the specs themselves. The full text of every
entry lives in the Railway DB and is the authority. To read one:

```sql
SELECT id, title, details FROM session_log WHERE id = <id>;
```

To find one by topic:

```sql
SELECT id, category, title FROM session_log
WHERE title ILIKE '%<keyword>%'
  AND category NOT IN ('archived_superseded')
ORDER BY id DESC;
```

`archived_superseded` (530 more entries) is deliberately excluded here — those are
retired and must not be treated as current. If a spec below says "supersedes id=X",
X is archived.

---

## 1 · Operating rules & protocol — read these first

| id | Title |
|---|---|
| 15150 | PRODUCTION_MODE_STANDING_V1 — CC never holds a push (founder-locked 04-Aug) |
| 13829 | ENGINE_LIVENESS_RULE_V1 — built-and-registered is NOT live; badge follows the data |
| 3041 | MAINTENANCE_LOCK_RULE_V1 — REINDEX/VACUUM FULL/CLUSTER/ALTER TABLE = console, weekends, propose-first |
| 2987 | CC_RULE_NAV_COMPLETE_SHIPPING_V1 — a page isn't done until deployed AND in the NAV array |
| 2667 | DEV_MODE_PUSH_RULE_V2 |
| 8122 | NIGHT_BATCH_WINDOW_V1 — 00:00–06:00 IST protected + no-push (amends 2667) |
| 1713 | DEV_STAGE_DEPLOY_POLICY_V1 — RULE_7 deploy window suspended while in dev |
| 2349 | DEPLOY_POLICY_ADDENDUM_09JUL |
| 2160 | DONE_WHAT_NEXT_PUSH_SIGNAL_V1 — the push handshake |
| 2164 | CC_PUSH_SIGNAL_V1 |
| 1586 | VERIFICATION_SCOPE_RULE_full_chain |
| 967 | CC_TASK_PROTOCOL_V1 |
| 638 | CC_TASK_CLAIMING_PROTOCOL_V1 |
| 6063 | CC_BATCH_QUEUE_MODEL_V1 |
| 428 | CC_OPERATING_FRAMEWORK_V1 |
| 1473 | CC_MODEL_ROUTING_RULE_V1 |
| 1316 | CC_PUSH_RELAY_FALLBACK_PROTOCOL |
| 8807 | MAX_SUBSCRIPTION_ONLY_RULE_V1 — CC is the model, no API billing |
| 8850 | DETECT_REPAIR_RULE_V1 |
| 12625 | AUTO_FILE_GRANT_31JUL |
| 239 | github_push_discipline |
| 100 | main_py_architecture_rule — main.py is wiring only |
| 101 | session_log_write_protocol_v2 |
| 5848 | MEMORY_HYGIENE_POLICY_V1 |
| 1175 | MEMORY_TAXONOMY_V1 |
| 156 | RAILWAY_MEMORY_RULES |
| 150 | CANONICAL_SPEC_REGISTRY |
| 265 | SPEC_PROPAGATION_MAP |
| 13295 | MEMORY_SWEEP_FULL_SINGLE_SESSION_V2 |
| 7581 | CRITICAL_PATH_FREEZE_V1 |
| 5700 | SCHEDULER_MASTER_RULE — enumeration must be registry-derived |
| 7129 / 7130 | ENGINE_WATCHDOG_V1 / ENGINE_WATCHDOG_STANDING_RULE |
| 10093 | BACKLOG_STATUS_CHECK_V1 |
| 1306 | AUTO_CLEAN_SESSION_CHECK_RULE |
| 235 | chat_rule_trigger_agenda_suggest |
| 196 | CANONICAL_ARCHITECTURE_v2_MaxCIO_vs_ClaudeAI |

## 2 · V8 engine, baskets & paper execution

| id | Title |
|---|---|
| 15366 | SELL_MOMENTUM_V4_N5I — weekly RSI REMOVED, mom_2d restored (04-Aug) **← current** |
| 4514 | SELL_MOMENTUM_SPEC_V4_N5I_LOCKED_16JUL |
| 5626 | SELL_REVERSAL_SPEC_V6_R1FALL_LOCKED_17JUL |
| 7828 | BUY_REVERSAL_V6_SPEC — 7 strict-AND conditions |
| 7842 | BUY_REVERSAL_V6_FRESH_BOOK — V6 performance counts only V6-stamped trades |
| 5647 | BUY_REVERSAL_SPEC_V5_SPRING_LOCKED_17JUL |
| 5650 | BUY_MOMENTUM_SPEC_V3_LOCKED_17JUL |
| 5642 | SELL_OVERBOUGHT_KILLED_17JUL |
| 5646 | BUY_S1_BOUNCE_KILLED_17JUL |
| 1407 | V8_LIVE_METRICS_TRUTH_v2.4.0 |
| 1651 / 1652 | V8_EOD_NO_REQUALIFICATION_V1 (+ addendum) |
| 1510 | V8_PRICING_PRINCIPLE_V1 |
| 1403 | V8_PAPER_EXECUTION_SPEC_V2 |
| 379 | SLOT_ARCHITECTURE_V2.4.0 |
| 1493 | V8_BACKTEST_OWNERSHIP_RULE |
| 1515 | V8_PARITY_HARNESS_SPEC_V1 |
| 6055 | V8_INDEX_INTEL_UNIFIED_SPEC_V1_LOCKED |
| 179 | v8_entry_rule_zone_gap50 |
| 174 | v8_gvm_gate_relax_7_to_6 |
| 175 | buy_reversal_gate_adaptive_qualification |
| 336 / 337 | buy_reversal filter optimisation / dynamic nifty filter |
| 1916 | PCR_TREND_MOOD_GATE_V1 |
| 1919 | MOOD_GATE_INLINE_LAST_VALUE_DAYCHANGE_V1 |
| 165 | adr_live_intraday_spec |
| 226 | day_change_to_mom_2d_COMPLETE |
| 1625 | FUNNEL_AUDIT_6baskets_06Jul |
| 3075 | FILTER_LOGIC_MASTER_INDEX_V1 |
| 5680 | BASKET_EDIT_AUTHORITY_RULE |
| 244 | trade_check_v8_context_isolation |
| 214 | V8_vs_TradeCheck_separation |

## 3 · V9 / V10 / V14 / V15 engines

| id | Title |
|---|---|
| 8169 | V9_SECTOR_PAIRS_SPEC_V3_BRAHMASTRA |
| 3060 | V14_INTRADAY_ENGINE_SPEC_V1_LOCKED |
| 3062 | V14_GATES_FINAL_V2_LOCKED |
| 3063 | V14_SETUPS_FINAL_V2_LOCKED |
| 3064 | V14_EXITS_AND_PP_FINAL_LOCKED — square-off 15:15 |
| 374 | INTRADAY_PAPER_ENGINE_SPEC_V1 |
| 399 / 400 | INTRADAY_SCANNER_SPEC_V2 / SELL_SPEC_V1 |
| 3079 | V15_MF_INTELLIGENCE_ARCHITECTURE_V1 |
| 3080 | V15_MF_BUILD_FRAMEWORK |
| 4734 | V15_MF_DATA_PIPELINE_SPEC_V1_FINAL |
| 4334 | V15_EQUITY_UNIVERSE_V1 |
| 3365 / 3364 | V15_PAGE_DESIGN_LOCKED_R2 / V15_DESIGN_REF_R2_LOCKED |
| 1662 | V11_ENDGAME_SEED_strategy_builder_platform |
| 3069 | FABLE_V13_THEME_BRIDGE_STANDING_RULE |

## 4 · Trade Check / Invest Check

| id | Title |
|---|---|
| 959 | TC_CANONICAL_SPEC_V4 |
| 2926 | TRADE_CHECK_SPEC_V4_DUAL_STYLE |
| 3005 | TRADE_CHECK_V4_FINAL_UNIFIED_SURFACE |
| 6625 / 6640 | TC_V4_CEILING_FINAL_V1 (+ LIVE cc#586) |
| 9035 | TC_GATE_SIMPLIFICATION_V1 |
| 9973 | TC_INTRADAY_TRUTH_V2 |
| 9946 | TC_R1_MOOD_V2 |
| 3010 | TC_V4_2_SELL_RULEBOOK_FINAL |
| 3019 | TC_V4_R16_FIB_RULE |
| 6621 | TC_V4_R17_VALUATION_RULE |
| 6624 | TC_R18_MOMENTUM_V1 |
| 6622 | TC_R19_RELATIVE_STRENGTH_V1 |
| 6623 | TC_R20_DELTA_GVM_180D_V1 |
| 5682 | TC_V4_3_V8_ALIGNMENT_PRINCIPLE |
| 12289 | TC_SCANNER_V41_V8_ALIGNMENT_SPEC |
| 10237 | TC_VERSION_PROPAGATION_RULE_V1 |
| 11047 | TC_OUTCOME_SIM_SPEC_V1 |
| 6632 | INVEST_CHECK_V3_0_GATE_CONVICTION_FINAL |
| 243 | trade_check_priority_order |
| 948 | TRADE_ANALYSIS_MANDATORY_3CHECK |

## 5 · Quant Baskets (QB) & model baskets

| id | Title |
|---|---|
| 6089 | QUANT_BASKET_FRAMEWORK_MASTER |
| 2970 | V12_QUANT_BASKET_BUILDER_MASTER_SPEC_V1 |
| 6085 / 6086 | ALPHA_MULTICAP_SPEC_V2_FINAL_LOCKED / DGVM |
| 6094 / 6097 / 6098 | SMALL_CAP_V2 / LARGE_CAP_V2 / MID_CAP_V2 LOCKED |
| 6103 | BREAKOUT_52W_BASKET_V1_LOCKED |
| 6104 | CONTRA_VALUE_BASKET_V1_LOCKED |
| 6109 | QB_SIX_BASKET_PAGE_DESIGN_LOCKED |
| 125 / 124 | qb_rebalance_schedule / qb_exit_rules |
| 80 | basket_rebalancing_modus_operandi |
| 5019–5093 | SCORR_BASKET_* (DivYield, Multibagger, HiddenValue, MarketLeaders, FutureGiants) |

## 6 · Data, feed & scheduler

| id | Title |
|---|---|
| 15596 | **CLOSING_PRICE_METHODOLOGY_BREAK_03AUG2026** — SEBI CAS, VWAP→auction close (cc#855) |
| 14406 | GLOBAL_HEATSTRIP_V2 |
| 166 | fyers_worker_ops |
| 167 | five_min_system_architecture |
| 1525 | FEED_PROVENANCE_CONSOLIDATION_intraday_equity |
| 1536 | INCIDENT_06JUL_feed_outage_0915_1005_root_cause |
| 1918 | PCR_INTRADAY_STALE_OI_GUARD_V1 |
| 1917 | GLOBAL_INDICES_PREVCLOSE_BUG_AND_247_REFRESH_V1 |
| 79 | intraday_gap_fill_rule |
| 1924 | RETENTION_POLICY_LOCKED_V2 |
| 1060 | DATA_SOURCE_RECONCILIATION_SPEC_30JUN2026 |
| 1781 | SCHEDULER_DIAGNOSIS_V2_full_coverage |
| 9116 | OPS_CONTROL_PLANE_V1_MASTERS |
| 6616 | SYSTEM_SPEED_CHECK_SPEC_V1 |
| 5422 | RAILWAY_PRO_UPGRADE_DISK_DOCTRINE_V2 |
| 208 | SCHEMA_REGISTRY_SNAPSHOT |
| 1173 | STOCK_OPTIONS_CHAIN_SPEC_V1 |
| 6339 | STOCK_OPTIONS_OFF_WS_ONDEMAND_CHAIN_V1 |
| 1770 | EARNINGS_CALENDAR_V2_ACCUMULATE |

## 7 · Fundamentals, GVM & ops metrics

| id | Title |
|---|---|
| 269 | gvm_canonical_pillar_map |
| 5678 | GVM_RATING_METHODOLOGY_UNIFICATION_V1 |
| 10056 | MQS WEIGHT CHANGE Q50/R25/C15/S10 |
| 5698 | OPS_METRICS_FRAMEWORK_V1 |
| 5684 | OPS_METRICS_SEGMENT_KPI_REGISTRY_V1 |
| 5685 | OPS_METRICS_SEGMENT_EXTENSIONS_V1_1 |
| 5709 | OPS_METRICS_PHASE_SPLIT_V1 + storage guard |
| 7117 | OPS_METRICS_PEER_BENCHMARK_SPEC_V1_1 |
| 7132 | OPS_METRICS_PEERSET_AND_WINDOW_LOCK_V1 |
| 7118 | SECTOR_KPI_LOCKED_FROM_DATA_V1 |
| 9088 | MONTHLY_OPS_METRICS_CLEANUP_RITUAL_V1 |
| 13348 | OPS_METRICS_INTERNAL_ONLY |
| 304 / 305 | SECTOR_OPS_METRICS_SPEC_v1 / COMPANY_LIST_v1 |
| 7121 | SHAREHOLDING_SCRAPE_CADENCE_V1 |
| 9178 | SCRAPE_UNIVERSE_TOP500_NSE_V1 |
| 10260 | SCRAPE_UNIVERSE_HARD_BOUNDARY_V1 |
| 13546 | SCRAPE_UNIVERSE_TOP500 (supersedes 10260) |
| 13517 | SEASON_PASS_SCRAPE_POLICY |
| 5634 | MC_SCRAPER_FRAMEWORK_CC500_17JUL |
| 5712 | SCREENER_UPLOAD_PROTOCOL_V1 |
| 87 / 90 | input_raw refresh schedule / content structure |
| 9108 | RESULT_SEASON_DATA_FRESHNESS_V1 |
| 85 | cap_category_definition |

## 8 · News, polish & editorial

| id | Title |
|---|---|
| 965 | **daily_digest_format_v2** — the digest content contract (V3 renders this) |
| 634 / 635 / 636 | NEWS_POLISH_SPEC / FRAMEWORK / DISPLAY_SPEC |
| 733 | POLISH_NEWS_SPEC_V1 |
| 1293 | POLISH_BATCH_EDITORIAL_SPEC_V2 |
| 8188 | POLISH_LANDING_RULE_V1 |
| 874 | POLISH_PRE_CHECK_RULE_V1 |
| 13354 | POLISH_RESULTS_FRAMEWORK_V1 |
| 13616 | L2_POLISH_PROTOCOL_V1 — CC-on-command, no API |
| 10086 | POLISH_RESULT_UPDATE_FRAMEWORK_V1 |
| 445 | SCORR_INTELLIGENCE_POLISH_PROTOCOL_V3 |
| 1923 | CONTENT_LEVEL_NEWS_DEDUP_V1 |
| 4284 | NEWS_BRAND_SCRUB_RULE |
| 402 | NEWS_FETCH_SPEC_V1 |
| 1660 | POSITION_NEWS_PIPELINE_V1 *(feature retired cc#847)* |
| 13456 | STOCK_VIEWS_FRAMEWORK_V2 |
| 8372 | STOCK_VIEW_CATEGORY_SPEC_V2 |
| 413 | RESULT_ANALYSIS_FORMAT_V1 |
| 13353 | RESULT_ANALYSIS_V2_SAMPLE |
| 7135 | RESULT_CORNER_SPEC_V1 |
| 8157 | RESULT_CORNER_V2_DESIGN_R1_LOCKED |
| 6438 | RESULTS_R_BUTTON_UNIVERSAL_CARD_V1 |
| 6500 | VOLUME_V_BUTTON_UNIVERSAL_CARD_V1 |
| 2492 | AI_EDITORIAL_STYLE_ADDENDUM_09JUL |
| 986 | KNOWLEDGE_HUB_POLISH_SPEC_V1 |
| 110 | key_takeaway_update_rule |

## 9 · Portfolio, SmartGain & clients

| id | Title |
|---|---|
| 1170 | SMARTGAIN_ORDERBOOK_LINEAR_V1 (+ addenda 1272, 2066) |
| 1008 | SMARTGAIN_WEEKLY_PNL_FRAMEWORK_V1 (+ addendum 2057) |
| 4685 | SMARTGAIN_WEEK_PNL_DISPLAY_RULE_V1 |
| 1393 | SMARTGAIN_DAILY_M2M_SPEC_V1 |
| 828 | SMARTGAIN_REVIEW_SPEC_V1 |
| 4705 | SMARTGAIN_MHK40_CLEAN_RESET_16JUL |
| 6505 | SMARTGAIN_STALE_CARRIED_POSITIONS_MARK_CLOSED |
| 6399 | PRICING_RULE_FUTURES_ONLY_LOCKED_20JUL |
| 2102 | CLIENT_POSITIONS_TRACKING_SPEC_V1 |
| 11983 | CLIENT_INDEX_CREDENTIALS_DECISION_V1 |
| 11510 | CLIENT_INDEX_V1_GOOGLE_SHEET_RETIREMENT |
| 2169 | ACCOUNT_TYPE_QTY_RULE_V1 |
| 1049 | TRADE_DIRECTION_SOURCE_OF_TRUTH_V1 |
| 1058 | INFINN_M2M_TRACKING_SPEC_V1 |
| 2994 | PORTFOLIO_HEALTH_REPORT_SPEC_V1 |
| 8857 / 8858 | HEALTH_REPORT_CLIENT_FORMAT_V1 (+ V1.1 compact result table) |
| 566 | MULTI_BOOK_FUND_MANAGER_FRAMEWORK_V1 |
| 721 | TRADE_JOURNAL_FRAMEWORK_V1 |
| 143 | trade_review_framework_v3_final |
| 145 | journal_triggers_v1 |

## 10 · Product, design & nav

| id | Title |
|---|---|
| 375 | CANONICAL_NAV_SPEC_V1 |
| 637 | NAV_UX_SPEC_V1 |
| 9016 | UI_PATTERN_MASTER_INDEX_V1 |
| 2460 | SCORR_FULL_APP_REDESIGN_MAP_V1 |
| 432 | SCORR_PRODUCT_ARCHITECTURE_V2.1 |
| 396 / 397 / 398 | SCORR_APP_LAYER1 product / LAYER2 tech / LAYER3 design |
| 6372 | DERIVATIVE_COCKPIT_REDESIGN_V2 |
| 13551 | BASIC_POLISH_L1_CARD_DESIGN_V1 |
| 6133 | FIB_LEVELS_ZONE_COLOR_FIX |
| 45 | trade_card_format |
| 146 | virtual_dashboard_format |
| 219 | surface_names_locked |
| 222 | native_grammar_v1 |
| 376 | DEPLOY_REFLECTS_SCORR_IN_V1 |
| 2498 | PRICE_ACTION_PRIMACY_COCKPIT_ROLE |

## 11 · Max (AI CIO) & subscription

| id | Title |
|---|---|
| 13823 | MAX_FRAMEWORK_INDEX_V1 — **master index, read first for anything Max** |
| 13816 | MAX_IVR_ARCHITECTURE_V1 |
| 13822 | MAX_IVR_TREE_V2 (supersedes V1 id=13820) |
| 851 | AI_CIO_MAX_FINAL_SPEC_V1 |
| 852 | AI_CIO_MAX_POSITIONING_V1 |
| 849 | AI_CIO_MAX_PRICING_V1 |
| 848 | AI_CIO_MAX_MODEL_SWITCH_V1 |
| 844 | AI_CIO_MAX_PLAN_FEATURES_V1 |
| 850 | AI_CIO_MAX_SESSION_BAR_V1 |
| 202 | SCORR_MAX_MODEL_CONFIG_v2 |
| 195 | Max CIO Memory Protocol v1 |
| 843 | SUBSCRIPTION_TIER_UPDATE_26JUN2026 |
| 221 | max_pricing_model_locked |
| 6062 | AI_AUTH_ARCHITECTURE_V1_LOCKED |
| 6615 | AI_INFRA_BILLING_ANTHROPIC_API_RETIRED_V1 |

## 12 · Business, strategy & roadmap

| id | Title |
|---|---|
| 2505 | NORTH_STAR_OCT2026_PROFIT_PRIMACY |
| 5846 | SCORR_ROADMAP_MASTER |
| 5845 | SCORR_STRATEGY_FRAMEWORK_MASTER |
| 534 | SCORR_VISION_AND_ROADMAP_V1 |
| 341 | SCORR_PRODUCT_VISION_V1 |
| 527 | SCORR_BUSINESS_PLAN_V1 |
| 434 | SCORR_PHASED_RELEASE_STRATEGY_V1 |
| 405 | SCORR_3NODE_OPERATING_SYSTEM_V1 |
| 681 | GOLIVE_PREREQUISITES_V1 |
| 233 | scorr_positioning_2year_roadmap_locked |
| 278 | Scorr Positioning Framework v1.0 |
| 286 | SCORR_2030_COMMITMENT |
| 280 | Scorr Launch Calendar 2026-2027 |
| 528 | LINKEDIN_CONTENT_PLAN_V1 |
| 737 / 2172 | LINKEDIN_POST_SPEC_V1 / POST_FORMAT_V1 |

## 13 · Backtesting & learnings

| id | Title |
|---|---|
| 7990 | BACKTESTING_FRAMEWORK_V1 |
| 4708 | V7_BACKTEST_ARCHITECTURE_V1 |
| 4711 | PIPELINE_LOCKED_17JUL_QUEUE_THEN_BACKTEST |
| 13286 | WEEK_5_TRADING_LEARNINGS (27–31 Jul) — net +Rs 15,670 |
| 1476 | TRADING_LEARNINGS_WEEK1_29JUN_03JUL |
| 207 | DEBUG_LEARNINGS_CANONICAL |
| 743 | JULY_STABILISATION_AUDIT |

---

## Open items as of 05-Aug-2026

- **cc#852** — pending: global tape coverage (DAX/FTSE live, add Hang Seng)
- **cc#849** — build shipped `9cd462c`; Global Indices tab retirement GATED on a live
  check of the digest global section
- **cc#850** — rename shipped `1d6890c`; one `TAB_ORDER` edit left, gated on cc#849
- **Pre-open restructure lands 07-Sep-2026** (same SEBI circular as cc#855) — affects the
  cc#843 `WS_FIRST_CONNECT = 09:16` logic. Needs its own card before that date.
- **Proposed, not run:** `ALTER TABLE v8_metrics ALTER COLUMN computed_at SET DEFAULT
  (NOW() AT TIME ZONE 'Asia/Kolkata')` — weekend console window per rule 10.
